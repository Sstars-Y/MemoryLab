from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import os

from .lifecycle import remove_managed_tree
from .paths import guarded
from .storage import connect
from .types import MemoryFault, Scope, identifier, reject_secrets, uid


def file_hash(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def recover_restore(layout):
    """Finish a validated, durable restore intent after interruption, before opening SQLite."""
    intent = layout.path("restore-intents", layout.persona_id+".json")
    if not intent.exists():
        return
    if not layout.ledger.is_file():
        raise MemoryFault("恢复中断且生命周期清单缺失，拒绝打开", 409)
    information = json.loads(intent.read_text(encoding="utf-8"))
    staging = layout.path("restore-staging", identifier(information["staging_id"]))
    if information["persona_id"] != layout.persona_id:
        raise MemoryFault("恢复意图身份不匹配", 409)
    for relative, expected in information["files"].items():
        source = guarded(staging, relative)
        if file_hash(source) != expected["sha256"]:
            raise MemoryFault("中断恢复的暂存文件校验失败", 409)
    # Staging remains intact until lifecycle replay succeeds, so this is idempotent.
    for suffix in ("-wal", "-shm"):
        Path(str(layout.database)+suffix).unlink(missing_ok=True)
    replacement = layout.persona_path("data", "memory", "restore-install.db")
    shutil.copyfile(guarded(staging, "memory", "metadata.db"), replacement)
    replacement.replace(layout.database)
    for relative in information["files"]:
        if relative.startswith("media/"):
            destination = layout.persona_path("data", relative)
        elif relative.startswith("persona/"):
            destination = layout.persona_path(relative.removeprefix("persona/"))
        else:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(guarded(staging, relative), destination)
    return information


class ProductsMixin:
    def finish_restore_intent(self):
        intent = self.layout.path("restore-intents", self.persona_id+".json")
        if intent.exists():
            information = json.loads(intent.read_text(encoding="utf-8"))
            for product in information.get("products", []):
                self.db.execute("INSERT OR IGNORE INTO ltm_products VALUES(?,?,?,?,?,?)", product)
            # Replay has already run, but newly carried product registrations also
            # need invalidation by operations newer than the snapshot.
            selected = set()
            purged = set()
            for operation in self.ledger.execute("SELECT kind,target_ids FROM operations WHERE seq>?", (information.get("lifecycle_seq", 0),)):
                mids = set(json.loads(operation["target_ids"]))
                selected.update(mids)
                if operation["kind"] == "purge":
                    purged.update(mids)
            self._invalidate(selected)
            self._invalidate(purged, purge=True)
            self._clean_invalid_products()
            intent.unlink()
            staging = self.layout.path("restore-staging", identifier(information["staging_id"]))
            remove_managed_tree(self.layout.data, staging)

    def export(self, scope: Scope, message_ids=None, filters=None, include_media=False, include_persona=False, include_episodes=True):
        with self.lock:
            self.check(scope)
            self.barrier()
            if message_ids is not None:
                rows = self.read_messages(scope, message_ids)
            else:
                # Export is bounded and defaults to the bound current session.
                if not scope.session_id:
                    raise MemoryFault("请选择一个会话或明确的消息范围")
                self.check(scope, session=True)
                rules = {**(filters or {}), "session_id": scope.session_id}
                rows, offset = [], 0
                while True:
                    batch = self.search_messages(scope, "", rules, 200, offset)
                    rows.extend(batch)
                    if len(batch) < 200:
                        break
                    offset += 200
                    if offset > 10000:
                        raise MemoryFault("单次导出最多 10000 条，请缩小日期范围")
                rows.sort(key=lambda m: m["ordinal"])
            export_id = uid("export")
            folder = self.layout.persona_path("exports", export_id)
            folder.mkdir()
            try:
                transcript = ["# 对话导出", "", "仅文本导出不包含原始媒体。" if not include_media else "包含所选消息的原始媒体。", ""]
                structured = []
                for row in rows:
                    reject_secrets(row["body"])
                    transcript.extend([f"## {row['role']} · {row['created']} · {row['id']}", row["body"], ""])
                    entry = {key: row[key] for key in ("id", "role", "body", "created", "session_id", "turn_id", "revision", "corrections", "attachments")}
                    for correction in row["corrections"]:
                        reject_secrets(correction["correction"])
                        transcript.append(f"纠正／变化（{correction['kind']}）：{correction['correction']}")
                    for attachment in row["attachments"]:
                        transcript.append(f"【{'图片' if attachment['mime'].startswith('image/') else '语音'}】@{attachment['id']}")
                        if attachment["recognition"]:
                            reject_secrets(attachment["recognition"])
                            transcript.append(f"识别文字：{attachment['recognition']}")
                        if include_media:
                            source, _ = self.read_attachment(scope, attachment["id"])
                            dest = guarded(folder, "media", source.name)
                            dest.parent.mkdir(exist_ok=True)
                            shutil.copyfile(source, dest)
                            attachment["export_path"] = f"media/{source.name}"
                            transcript.append(f"[附件](media/{source.name})")
                    structured.append(entry)
                guarded(folder, "transcript.md").write_text("\n".join(transcript), encoding="utf-8")
                guarded(folder, "messages.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in structured), encoding="utf-8")
                if include_persona:
                    text = self.compile_persona()
                    guarded(folder, "persona.md").write_text(text, encoding="utf-8")
                source_ids = [r["id"] for r in rows]
                if include_episodes:
                    chosen = set(source_ids)
                    summaries = []
                    for episode in self.db.execute("SELECT * FROM ltm_episodes WHERE user_id=? AND status='active'", (scope.user_id,)):
                        refs = json.loads(episode["source_ids"])
                        if set(refs).issubset(chosen):
                            reject_secrets(episode["summary"])
                            summaries.append(episode["summary"]+"\n来源："+", ".join(refs))
                    guarded(folder, "episodes.md").write_text("\n\n".join(summaries), encoding="utf-8")
                manifest = {"version": 1, "persona_id": self.persona_id, "created": self.clock(), "message_count": len(rows), "include_media": include_media,
                            "files": self._file_manifest(folder)}
                guarded(folder, "export-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                import zipfile
                with zipfile.ZipFile(guarded(folder, "download.zip"), "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    for name in [*manifest["files"], "export-manifest.json"]:
                        bundle.write(guarded(folder, name), arcname=name)
                self.db.execute("INSERT INTO ltm_products VALUES(?,?,?,?,?,?)", (export_id, "export", scope.user_id, json.dumps(source_ids), "active", self.clock()))
                return {"id": export_id, "message_count": len(rows), "status": "complete", "files": list(manifest["files"])}
            except BaseException:
                remove_managed_tree(self.layout.persona, folder)
                raise

    def _file_manifest(self, folder):
        files = {}
        # folder must be an export/snapshot/restore staging directory, never persona.
        def visit(directory):
            for entry in directory.iterdir():
                safe = guarded(folder, str(entry.relative_to(folder)))
                if safe.is_dir():
                    visit(safe)
                else:
                    files[safe.relative_to(folder).as_posix()] = {"size": safe.stat().st_size, "sha256": file_hash(safe)}
        visit(folder)
        return files

    def snapshot(self, scope: Scope):
        with self.lock:
            self.check(scope)
            self.barrier()
            # Until an explicit administrator interface exists, never expose another user's DB.
            if self.db.execute("SELECT 1 FROM ltm_messages WHERE user_id!=? AND status!='purged' LIMIT 1", (scope.user_id,)).fetchone():
                raise MemoryFault("当前网页不能快照其他用户的私聊数据", 403)
            snapshot_id = uid("snapshot")
            folder = self.layout.persona_path("backups", snapshot_id)
            folder.mkdir()
            try:
                guarded(folder, "memory").mkdir()
                backup_db = sqlite3.connect(guarded(folder, "memory", "metadata.db"))
                try:
                    self.db.backup(backup_db)
                finally:
                    backup_db.close()
                sources = []
                for row in self.db.execute("SELECT id FROM ltm_messages WHERE status IN ('active','recycled')"):
                    sources.append(row[0])
                for attachment in self.db.execute("SELECT * FROM ltm_attachments WHERE status IN ('active','recycled')"):
                    source = self.layout.persona_path(attachment["relative_path"])
                    target = guarded(folder, attachment["relative_path"].removeprefix("data/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not source.is_file() or source.stat().st_size != attachment["size"] or file_hash(source) != attachment["sha256"]:
                        raise MemoryFault("媒体缺失或校验失败，快照未完成", 409)
                    shutil.copyfile(source, target)
                for name in ("manifest.json", "editor.json"):
                    target = guarded(folder, "persona", name)
                    target.parent.mkdir(exist_ok=True)
                    shutil.copyfile(self.layout.persona_path(name), target)
                lore = self.layout.persona_path("lore")
                for entry in lore.glob("*.md"):
                    source = guarded(lore, entry.name)
                    target = guarded(folder, "persona", "lore", entry.name)
                    target.parent.mkdir(exist_ok=True)
                    shutil.copyfile(source, target)
                ledger_id = self.ledger.execute("SELECT ledger_id FROM identity").fetchone()[0]
                manifest = {"version": 1, "persona_id": self.persona_id, "user_id": scope.user_id,
                            "ledger_id": ledger_id, "lifecycle_seq": self.seq, "created": self.clock(), "files": self._file_manifest(folder)}
                guarded(folder, "snapshot.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                self.db.execute("INSERT INTO ltm_products VALUES(?,?,?,?,?,?)", (snapshot_id, "snapshot", scope.user_id, json.dumps(sources), "active", self.clock()))
                return {"id": snapshot_id, "status": "complete", "files": len(manifest["files"])}
            except BaseException:
                remove_managed_tree(self.layout.persona, folder)
                raise

    def _snapshot_folder(self, snapshot_id):
        return self.layout.persona_path("backups", identifier(snapshot_id))

    def verify_snapshot(self, scope: Scope, snapshot_id: str):
        with self.lock:
            self.check(scope)
            self.barrier()
            if not self.layout.ledger.is_file():
                raise MemoryFault("缺少最新生命周期清单", 409)
            registration = self.db.execute("SELECT id FROM ltm_products WHERE id=? AND user_id=? AND kind='snapshot' AND status='active'", (snapshot_id, scope.user_id)).fetchone()
            if not registration:
                raise MemoryFault("快照不存在或已失效", 409)
            folder = self._snapshot_folder(snapshot_id)
            manifest_file = guarded(folder, "snapshot.json")
            if not manifest_file.is_file() or manifest_file.stat().st_size > 64*1024*1024:
                raise MemoryFault("快照清单缺失或过大", 409)
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                ledger_id = self.ledger.execute("SELECT ledger_id FROM identity").fetchone()[0]
                if manifest["version"] != 1 or manifest["persona_id"] != self.persona_id or manifest["user_id"] != scope.user_id or manifest["ledger_id"] != ledger_id or manifest["lifecycle_seq"] > self.seq:
                    raise MemoryFault("快照身份、版本或生命周期不匹配", 409)
                required = {"memory/metadata.db", "persona/manifest.json", "persona/editor.json"}
                files = manifest["files"]
                if not required.issubset(files):
                    raise MemoryFault("快照缺少必要文件", 409)
                for relative, expected in files.items():
                    parts = Path(relative).parts
                    allowed = relative in required or (len(parts) == 3 and parts[0:2] == ("persona", "lore") and parts[2].endswith(".md")) or (len(parts) == 3 and parts[0] == "media" and parts[1] in ("images", "audio") and parts[2].startswith("att_"))
                    if not allowed:
                        raise MemoryFault("快照包含非白名单文件", 409)
                    source = guarded(folder, relative)
                    if not source.is_file() or source.stat().st_size != expected["size"] or file_hash(source) != expected["sha256"]:
                        raise MemoryFault("快照文件缺失或校验失败", 409)
                probe = sqlite3.connect(guarded(folder, "memory", "metadata.db").as_uri()+"?mode=ro", uri=True)
                probe.row_factory = sqlite3.Row
                try:
                    probe.execute("PRAGMA trusted_schema=OFF")
                    if probe.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise MemoryFault("快照数据库损坏", 409)
                    binding = probe.execute("SELECT value FROM ltm_meta WHERE key='ledger_id'").fetchone()
                    if not binding or binding[0] != ledger_id:
                        raise MemoryFault("快照数据库生命周期身份不匹配", 409)
                    schema = {(r[0], r[1], r[2]) for r in probe.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
                    expected_schema = {(r[0], r[1], r[2]) for r in self.db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
                    if schema != expected_schema:
                        raise MemoryFault("快照数据库结构不受支持", 409)
                    if probe.execute("SELECT 1 FROM ltm_messages WHERE user_id!=? AND status!='purged' LIMIT 1", (scope.user_id,)).fetchone():
                        raise MemoryFault("快照包含范围外私聊", 403)
                    for attachment in probe.execute("SELECT * FROM ltm_attachments WHERE status IN ('active','recycled')"):
                        relative = attachment["relative_path"].removeprefix("data/")
                        if relative not in files or files[relative]["sha256"] != attachment["sha256"]:
                            raise MemoryFault("快照附件引用不完整", 409)
                finally:
                    probe.close()
                return manifest
            except (KeyError, ValueError, sqlite3.Error) as error:
                raise MemoryFault("快照格式不合法", 409) from error

    def restore_snapshot(self, scope: Scope, snapshot_id: str):
        with self.lock:
            manifest = self.verify_snapshot(scope, snapshot_id)
            folder = self._snapshot_folder(snapshot_id)
            # Staging is within this project's own data tree. It is not a backup.
            staging = self.layout.path("restore-staging", uid("restore"))
            staging.mkdir(parents=True)
            self.epoch += 1
            try:
                for relative in manifest["files"]:
                    source = guarded(folder, relative)
                    target = guarded(staging, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                # Keep current products registry, so post-snapshot exports are still tracked.
                products = [tuple(r) for r in self.db.execute("SELECT * FROM ltm_products")]
                journal = {"staging_id": staging.name, "persona_id": self.persona_id, "files": manifest["files"], "products": products, "lifecycle_seq": manifest["lifecycle_seq"]}
                intent = self.layout.path("restore-intents", self.persona_id+".json")
                intent.parent.mkdir(parents=True, exist_ok=True)
                temp_intent = self.layout.path("restore-intents", self.persona_id+".tmp")
                with temp_intent.open("w", encoding="utf-8") as stream:
                    json.dump(journal, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp_intent.replace(intent)
                self.db.close()
                recover_restore(self.layout)
                self.db = connect(self.layout.database)
                for row in products:
                    self.db.execute("INSERT OR REPLACE INTO ltm_products VALUES(?,?,?,?,?,?)", row)
                self._replay()
                self.expire()
                # Invalidate derivatives and jobs bound to the previous running context.
                self.db.execute("UPDATE ltm_chunks SET status='invalid'")
                self.db.execute("UPDATE ltm_jobs SET status='pending',run_id=? WHERE status IN ('done','running')", (self.run_id,))
                self.db.execute("UPDATE ltm_requests SET status='interrupted' WHERE status IN ('running','pending')")
                vectors = self.layout.persona_path("data", "indexes", "vectors")
                for child in vectors.iterdir():
                    remove_managed_tree(vectors, child)
                if self.vector_index:
                    self.vector_index.clear_memory()
                self.compile_persona()
                self.clean_orphans()
                self.finish_restore_intent()
                self.ensure_search_index()
                return {"id": snapshot_id, "status": "restored", "lifecycle_seq": self.seq}
            finally:
                # An interrupted restore must retain its staging for next-start replay.
                if not self.layout.path("restore-intents", self.persona_id+".json").exists():
                    remove_managed_tree(self.layout.data, staging)

    def products(self, scope: Scope):
        with self.lock:
            self.check(scope)
            self.barrier()
            return [dict(r) for r in self.db.execute("SELECT id,kind,status,created FROM ltm_products WHERE user_id=? ORDER BY created DESC LIMIT 200", (scope.user_id,))]

    def product_file(self, scope: Scope, product_id: str, relative: str):
        with self.lock:
            self.check(scope)
            self.barrier()
            row = self.db.execute("SELECT * FROM ltm_products WHERE id=? AND user_id=? AND status='active' AND kind='export'", (identifier(product_id), scope.user_id)).fetchone()
            if not row:
                raise MemoryFault("导出不存在或已失效", 404)
            folder = self.layout.persona_path("exports", product_id)
            manifest = json.loads(guarded(folder, "export-manifest.json").read_text(encoding="utf-8"))
            if relative != "export-manifest.json" and relative not in manifest["files"]:
                raise MemoryFault("文件未列入导出清单", 403)
            return guarded(folder, relative)

    def export_archive(self, scope: Scope, export_id: str):
        with self.lock:
            manifest_path = self.product_file(scope, export_id, "export-manifest.json")
            archive = guarded(manifest_path.parent, "download.zip")
            if not archive.is_file():
                raise MemoryFault("此旧导出没有打包文件，请重新生成导出", 404)
            return archive
