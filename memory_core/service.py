from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
import threading

from .paths import Layout, guarded
from .storage import SCHEMA, LEDGER_SCHEMA, connect, terms, match_query
from .types import MemoryFault, Scope, identifier, now, reject_secrets, uid
from .lifecycle import LifecycleMixin
from .products import ProductsMixin, recover_restore


class MemoryService(LifecycleMixin, ProductsMixin):
    """One persona; the host binds user/session scope on every operation."""

    def __init__(self, project: Path, data_root: Path, persona_id: str, clock=now):
        self.layout = Layout(project, data_root, persona_id)
        self.persona_id = persona_id
        self.clock = clock
        self.lock = threading.RLock()
        self.run_id = uid("run")
        self.epoch = 0
        self.recording = True
        self.vector_index = None
        recover_restore(self.layout)
        existing = self.layout.database.exists()
        if existing and not self.layout.ledger.exists():
            raise MemoryFault("缺少最新生命周期清单，拒绝打开已有记忆库", 409)
        self.layout.initialize_dirs()
        self.db = connect(self.layout.database)
        self.ledger = connect(self.layout.ledger)
        self.db.executescript(SCHEMA)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(ltm_requests)")}
        if "payload_hash" not in columns:
            self.db.execute("ALTER TABLE ltm_requests ADD COLUMN payload_hash TEXT NOT NULL DEFAULT ''")
        self.ledger.executescript(LEDGER_SCHEMA)
        identity = self.ledger.execute("SELECT * FROM identity").fetchone()
        if not identity:
            if existing:
                raise MemoryFault("生命周期清单身份缺失", 409)
            self.ledger.execute("INSERT INTO identity VALUES(?,?)", (persona_id, uid("ledger")))
            identity = self.ledger.execute("SELECT * FROM identity").fetchone()
        if identity["persona_id"] != persona_id:
            raise MemoryFault("生命周期清单不属于当前人格", 409)
        binding = self.db.execute("SELECT value FROM ltm_meta WHERE key='ledger_id'").fetchone()
        if existing and (not binding or binding[0] != identity["ledger_id"]):
            raise MemoryFault("生命周期清单与记忆库不匹配", 409)
        self.db.execute("INSERT OR IGNORE INTO ltm_meta VALUES('ledger_id',?)", (identity["ledger_id"],))
        self.db.execute("INSERT OR IGNORE INTO ltm_meta VALUES('schema_version','1')")
        version = self.db.execute("SELECT value FROM ltm_meta WHERE key='schema_version'").fetchone()[0]
        if version != "1":
            raise MemoryFault("不支持的数据版本", 409)
        self._initialize_persona()
        with self.lock:
            self._replay()
            self.expire()
            self.db.execute("UPDATE ltm_requests SET status=CASE WHEN EXISTS(SELECT 1 FROM ltm_messages m WHERE m.idem=ltm_requests.id AND m.status='active') THEN 'completed' ELSE 'interrupted' END WHERE status IN ('pending','running')")
            self.db.execute("UPDATE ltm_jobs SET status='pending',run_id=? WHERE status='running'", (self.run_id,))
            self.finish_restore_intent()
            self.clean_orphans()
            self.ensure_search_index()

    def ensure_search_index(self):
        if not self.db.execute("SELECT 1 FROM ltm_meta WHERE key='search_media_v2'").fetchone():
            with self.transaction():
                for row in self.db.execute("SELECT DISTINCT m.id FROM ltm_messages m JOIN ltm_attachments a ON a.message_id=m.id WHERE a.recognition IS NOT NULL").fetchall():
                    self._refresh_search_index(row["id"])
                self.db.execute("INSERT INTO ltm_meta VALUES('search_media_v2','1')")

    def clean_orphans(self):
        referenced = {r[0] for r in self.db.execute("SELECT relative_path FROM ltm_attachments")}
        for kind in ("images", "audio"):
            directory = self.layout.persona_path("data", "media", kind)
            for entry in directory.iterdir():
                safe = guarded(directory, entry.name)
                if safe.is_file() and f"data/media/{kind}/{entry.name}" not in referenced:
                    safe.unlink()
        from .lifecycle import remove_managed_tree
        registered = {r[0] for r in self.db.execute("SELECT id FROM ltm_products")}
        for kind in ("exports", "backups"):
            directory = self.layout.persona_path(kind)
            for entry in directory.iterdir():
                if entry.name not in registered:
                    remove_managed_tree(directory, entry)

    def _initialize_persona(self):
        manifest = self.layout.persona_path("manifest.json")
        if not manifest.exists():
            manifest.write_text(json.dumps({"persona_id": self.persona_id, "name": "记忆伙伴"}, ensure_ascii=False), encoding="utf-8")
        if json.loads(manifest.read_text(encoding="utf-8"))["persona_id"] != self.persona_id:
            raise MemoryFault("人格编辑源身份不匹配", 409)
        editor = self.layout.persona_path("editor.json")
        if not editor.exists():
            editor.write_text(json.dumps({"instructions": "认真交流，依据来源回忆；不确定时坦诚说明。"}, ensure_ascii=False), encoding="utf-8")
        self.compile_persona()

    def compile_persona(self):
        manifest = json.loads(self.layout.persona_path("manifest.json").read_text(encoding="utf-8"))
        editor = json.loads(self.layout.persona_path("editor.json").read_text(encoding="utf-8"))
        instructions = str(editor.get("instructions", ""))
        reject_secrets(instructions)
        lore = []
        # Explicitly scoped to lore/; never enumerate the persona root.
        folder = self.layout.persona_path("lore")
        for p in sorted(folder.glob("*.md")):
            safe = guarded(folder, p.name)
            if safe.stat().st_size > 256_000:
                raise MemoryFault("单个人格扩展文件不能超过 256 KB")
            lore.append(safe.read_text(encoding="utf-8"))
        system = f"你是{manifest.get('name', '记忆伙伴')}。\n{instructions}\n" + "\n".join(lore)
        reject_secrets(system)
        digest = hashlib.sha256(system.encode()).hexdigest()
        self.layout.persona_path("system.md").write_text(system, encoding="utf-8")
        self.layout.persona_path("generated", "persona-version.json").write_text(json.dumps({"sha256": digest}), encoding="utf-8")
        return system

    def set_persona(self, name: str, instructions: str):
        reject_secrets(name + instructions)
        if not name.strip() or len(name) > 80 or len(instructions) > 16000:
            raise MemoryFault("人格名称或设定长度不合法")
        with self.lock:
            self.layout.persona_path("manifest.json").write_text(json.dumps({"persona_id": self.persona_id, "name": name}, ensure_ascii=False), encoding="utf-8")
            self.layout.persona_path("editor.json").write_text(json.dumps({"instructions": instructions}, ensure_ascii=False), encoding="utf-8")
            self.compile_persona()
            self.epoch += 1

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @property
    def seq(self):
        return self.ledger.execute("SELECT COALESCE(MAX(seq),0) FROM operations").fetchone()[0]

    def check(self, scope: Scope, session=False):
        if scope.persona_id != self.persona_id:
            raise MemoryFault("不允许跨人格访问", 403)
        identifier(scope.user_id)
        if scope.run_id and scope.run_id != self.run_id:
            raise MemoryFault("该操作所属运行实例已失效", 409)
        if session:
            row = self.db.execute("SELECT * FROM ltm_sessions WHERE id=? AND user_id=?", (scope.session_id, scope.user_id)).fetchone()
            if not row:
                raise MemoryFault("会话不存在或无权访问", 404)

    def barrier(self):
        self._replay()
        self.expire()

    def new_session(self, scope: Scope, title="新对话"):
        with self.lock:
            self.check(scope)
            identifier_value = uid("session")
            self.db.execute("INSERT INTO ltm_sessions VALUES(?,?,?,?)", (identifier_value, scope.user_id, title[:80], self.clock()))
            return identifier_value

    def sessions(self, scope: Scope):
        with self.lock:
            self.check(scope)
            return [dict(r) for r in self.db.execute("SELECT * FROM ltm_sessions WHERE user_id=? ORDER BY created DESC LIMIT 200", (scope.user_id,))]

    def _message(self, row):
        result = dict(row)
        result.pop("idem", None)
        result["corrections"] = [dict(r) for r in self.db.execute("SELECT * FROM ltm_revisions WHERE target_id=? ORDER BY created", (row["id"],))]
        result["attachments"] = [dict(r) for r in self.db.execute("SELECT id,mime,size,duration,status,recognition,recognition_version FROM ltm_attachments WHERE message_id=?", (row["id"],))]
        job = self.db.execute("SELECT status,error FROM ltm_jobs WHERE message_id=? AND revision=?", (row["id"], row["revision"])).fetchone()
        result["processing"] = dict(job) if job else {"status": "not_queued", "error": None}
        return result

    def _refresh_search_index(self, message_id):
        row = self.db.execute("SELECT pk,body FROM ltm_messages WHERE id=?", (message_id,)).fetchone()
        corrections = [r[0] for r in self.db.execute("SELECT correction FROM ltm_revisions WHERE target_id=?", (message_id,))]
        recognized = [r[0] for r in self.db.execute("SELECT recognition FROM ltm_attachments WHERE message_id=? AND recognition IS NOT NULL", (message_id,))]
        self.db.execute("INSERT OR REPLACE INTO ltm_messages_fts(rowid,terms) VALUES(?,?)", (row["pk"], terms(" ".join([row["body"], *corrections, *recognized]))))

    def record_message(self, scope: Scope, body: str, idempotency_key: str, role="user", turn_id=None, attachments=()):
        if role not in ("user", "assistant") or len(body) > 100_000 or (not body.strip() and not attachments):
            raise MemoryFault("消息为空或过长")
        identifier(idempotency_key)
        reject_secrets(body)
        with self.lock:
            self.check(scope, session=True)
            self.barrier()
            if not self.recording:
                raise MemoryFault("当前已暂停保存", 409)
            prior = self.db.execute("SELECT * FROM ltm_messages WHERE user_id=? AND idem=?", (scope.user_id, idempotency_key)).fetchone()
            if prior:
                if prior["status"] != "active" or prior["session_id"] != scope.session_id or prior["body"] != body or prior["role"] != role:
                    raise MemoryFault("幂等键已使用且请求不同或记录已失效", 409)
                return self._message(prior)
            message_id = uid("msg")
            turn_id = turn_id or message_id
            ordinal = self.db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM ltm_messages WHERE session_id=?", (scope.session_id,)).fetchone()[0]
            installed = []
            try:
                with self.transaction():
                    self.db.execute("INSERT INTO ltm_messages(id,user_id,session_id,ordinal,role,body,created,idem,turn_id) VALUES(?,?,?,?,?,?,?,?,?)", (message_id, scope.user_id, scope.session_id, ordinal, role, body, self.clock(), idempotency_key, turn_id))
                    pk = self.db.execute("SELECT pk FROM ltm_messages WHERE id=?", (message_id,)).fetchone()[0]
                    self.db.execute("INSERT INTO ltm_messages_fts(rowid,terms) VALUES(?,?)", (pk, terms(body)))
                    for attachment in attachments:
                        if attachment["user_id"] != scope.user_id or attachment["session_id"] != scope.session_id:
                            raise MemoryFault("附件不属于当前会话", 403)
                        aid = identifier(attachment["id"])
                        relative = f"data/media/{'images' if attachment['mime'].startswith('image/') else 'audio'}/{aid}{attachment['suffix']}"
                        destination = self.layout.persona_path(relative)
                        source = guarded(self.layout.project, str(Path(attachment["path"]).relative_to(self.layout.project)))
                        if destination.exists():
                            raise MemoryFault("附件编号已使用", 409)
                        if hashlib.sha256(source.read_bytes()).hexdigest() != attachment["sha256"]:
                            raise MemoryFault("附件校验失败")
                        source.replace(destination)
                        installed.append(destination)
                        self.db.execute("INSERT INTO ltm_attachments(id,message_id,user_id,relative_path,mime,size,duration,sha256) VALUES(?,?,?,?,?,?,?,?)", (aid, message_id, scope.user_id, relative, attachment["mime"], attachment["size"], attachment.get("duration"), attachment["sha256"]))
                    self.db.execute("INSERT INTO ltm_jobs(id,message_id,revision,run_id,seq,status) VALUES(?,?,?,?,?,'pending')", (uid("job"), message_id, 1, self.run_id, self.seq))
            except BaseException:
                for file in installed:
                    file.unlink(missing_ok=True)
                raise
            return self._message(self.db.execute("SELECT * FROM ltm_messages WHERE id=?", (message_id,)).fetchone())

    def history(self, scope: Scope, before: int | None = None, limit=80):
        with self.lock:
            self.check(scope, session=True)
            self.barrier()
            rows = self.db.execute("SELECT * FROM ltm_messages WHERE user_id=? AND session_id=? AND status='active' AND ordinal<? ORDER BY ordinal DESC LIMIT ?", (scope.user_id, scope.session_id, before or 2**63-1, min(max(limit, 1), 200))).fetchall()
            return [self._message(row) for row in reversed(rows)]

    def read_messages(self, scope: Scope, message_ids: list[str], before=0, after=0):
        if len(message_ids) > 100 or not (0 <= before <= 20 and 0 <= after <= 20):
            raise MemoryFault("展开范围过大")
        with self.lock:
            self.check(scope)
            self.barrier()
            found = {}
            for mid in message_ids:
                row = self.db.execute("SELECT * FROM ltm_messages WHERE id=? AND user_id=? AND status='active'", (mid, scope.user_id)).fetchone()
                if not row:
                    raise MemoryFault("消息不存在或无权访问", 404)
                for r in self.db.execute("SELECT * FROM ltm_messages WHERE user_id=? AND session_id=? AND status='active' AND ordinal BETWEEN ? AND ? ORDER BY ordinal", (scope.user_id, row["session_id"], row["ordinal"]-before, row["ordinal"]+after)):
                    found[r["id"]] = self._message(r)
            return sorted(found.values(), key=lambda m: (m["session_id"], m["ordinal"]))

    def _relaxed_match_query(self, query):
        # Broad OR expansion over common CJK singles ranks nearly the whole DB.
        # Use the existing index vocabulary to select a bounded set of useful
        # terms. This TEMP view neither changes the saved schema nor snapshots.
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.ltm_search_vocab USING fts5vocab(main,ltm_messages_fts,'row')")
        tokens=terms(query).split()[:64]
        if not tokens:
            return ""
        rows=self.db.execute("SELECT term,doc FROM temp.ltm_search_vocab WHERE term IN ("+",".join("?" for _ in tokens)+") ORDER BY doc,term",tokens).fetchall()
        preferred=[r["term"] for r in rows if r["term"].startswith("b") or bytes.fromhex(r["term"][1:]).isascii()]
        return " OR ".join((preferred or [r["term"] for r in rows])[:4])

    def search_messages(self, scope: Scope, query="", filters=None, limit=30, cursor=0, relaxed=False, *, newest=False):
        if len(query) > 500 or cursor < 0 or cursor > 10000:
            raise MemoryFault("查询过长或分页超出限制")
        filters = filters or {}
        limit = min(max(limit, 1), 200)
        with self.lock:
            self.check(scope)
            self.barrier()
            where, args = ["m.user_id=?", "m.status='active'"], [scope.user_id]
            for key, column in [("session_id", "session_id"), ("role", "role")]:
                if filters.get(key):
                    where.append(f"m.{column}=?")
                    args.append(filters[key])
            for key, operator in [("since", ">="), ("until", "<=")]:
                if filters.get(key) is not None:
                    where.append(f"m.created{operator}?")
                    args.append(float(filters[key]))
            join, rank = "", "m.created DESC,m.pk DESC"
            if query:
                if query.startswith("msg_"):
                    where.append("m.id=?")
                    args.append(query)
                else:
                    match = self._relaxed_match_query(query) if relaxed else match_query(query)
                    if not match:
                        return []
                    if newest:
                        # Materialize the matching row IDs, then walk the scoped
                        # time index. Avoid sorting all full message rows to find
                        # recent evidence when relevance ranks tie.
                        join = " INDEXED BY ltm_messages_time"
                        where.append("m.pk IN (SELECT rowid FROM ltm_messages_fts WHERE ltm_messages_fts MATCH ?)")
                    else:
                        join = " JOIN ltm_messages_fts f ON f.rowid=m.pk"
                        where.append("ltm_messages_fts MATCH ?")
                    args.append(match)
                    # FTS can stream its built-in BM25 rank and stop at LIMIT.
                    # Adding a time tie-break forces sorting every match. Date
                    # bounds remain filters; unqueried history stays newest-first.
                    if not newest:
                        rank = "f.rank"
                    if not relaxed:
                        where.append("(instr(lower(m.body),lower(?))>0 OR EXISTS(SELECT 1 FROM ltm_revisions r WHERE r.target_id=m.id AND instr(lower(r.correction),lower(?))>0) OR EXISTS(SELECT 1 FROM ltm_attachments a WHERE a.message_id=m.id AND instr(lower(a.recognition),lower(?))>0))")
                        args.extend([query, query, query])
            sql = f"SELECT m.* FROM ltm_messages m{join} WHERE {' AND '.join(where)} ORDER BY {rank} LIMIT ? OFFSET ?"
            return [self._message(row) for row in self.db.execute(sql, [*args, limit, cursor])]

    def corrections_for(self, message_ids):
        return {mid: [dict(r) for r in self.db.execute("SELECT * FROM ltm_revisions WHERE target_id=? ORDER BY created", (mid,))] for mid in message_ids}

    def correct_memory(self, scope: Scope, target: str, correction: str, kind="error", event_time=None):
        if kind not in ("error", "change", "transcription") or not correction.strip() or len(correction) > 16000:
            raise MemoryFault("纠正内容或类型无效")
        reject_secrets(correction)
        with self.lock:
            self.check(scope)
            self.barrier()
            row = self.read_messages(scope, [target])[0]
            if event_time:
                from datetime import datetime
                try:
                    datetime.fromisoformat(event_time)
                except ValueError:
                    raise MemoryFault("事件时间须为 ISO 日期或时间")
            correction_id = uid("revision")
            with self.transaction():
                self.db.execute("INSERT INTO ltm_revisions VALUES(?,?,?,?,?,?,?)", (correction_id, target, scope.user_id, correction, kind, event_time, self.clock()))
                self.db.execute("UPDATE ltm_messages SET revision=revision+1 WHERE id=?", (target,))
                self._refresh_search_index(target)
                self._invalidate([target])
                self.db.execute("INSERT INTO ltm_jobs(id,message_id,revision,run_id,seq,status) VALUES(?,?,?,?,?,'pending')", (uid("job"), target, row["revision"]+1, self.run_id, self.seq))
            self.epoch += 1
            self._clean_invalid_products()
            return correction_id

    def set_recording(self, enabled: bool):
        with self.lock:
            if self.recording != enabled:
                self.recording = enabled
                self.epoch += 1
            return {"recording": self.recording, "epoch": self.epoch}

    def read_attachment(self, scope: Scope, attachment_id: str, recycle=False):
        with self.lock:
            self.check(scope)
            self.barrier()
            row = self.db.execute("SELECT a.*,m.status AS message_status FROM ltm_attachments a JOIN ltm_messages m ON m.id=a.message_id WHERE a.id=? AND a.user_id=?", (identifier(attachment_id), scope.user_id)).fetchone()
            if not row or row["status"] != ("recycled" if recycle else "active") or row["message_status"] != row["status"]:
                raise MemoryFault("附件不存在或无权访问", 404)
            return self.layout.persona_path(row["relative_path"]), row["mime"]

    def recognize_attachment(self, scope: Scope, attachment_id: str, text: str, version: str, expected_revision: int):
        reject_secrets(text)
        with self.lock:
            self.read_attachment(scope, attachment_id)
            attachment = self.db.execute("SELECT * FROM ltm_attachments WHERE id=?", (attachment_id,)).fetchone()
            message = self.read_messages(scope, [attachment["message_id"]])[0]
            if message["revision"] != expected_revision:
                raise MemoryFault("识别来源版本已失效", 409)
            with self.transaction():
                self.db.execute("UPDATE ltm_attachments SET recognition=?,recognition_version=? WHERE id=?", (text, version, attachment_id))
                self.db.execute("UPDATE ltm_messages SET revision=revision+1 WHERE id=?", (message["id"],))
                self._refresh_search_index(message["id"])
                self._invalidate([message["id"]])
                self.db.execute("INSERT INTO ltm_jobs(id,message_id,revision,run_id,seq,status) VALUES(?,?,?,?,?,'pending')", (uid("job"), message["id"], message["revision"]+1, self.run_id, self.seq))
            self.epoch += 1

    def stats(self, scope: Scope):
        with self.lock:
            self.check(scope)
            self.barrier()
            return {"messages": self.db.execute("SELECT COUNT(*) FROM ltm_messages WHERE user_id=? AND status='active'", (scope.user_id,)).fetchone()[0],
                    "jobs": {r[0]: r[1] for r in self.db.execute("SELECT j.status,COUNT(*) FROM ltm_jobs j JOIN ltm_messages m ON m.id=j.message_id WHERE m.user_id=? GROUP BY j.status", (scope.user_id,))},
                    "recording": self.recording, "epoch": self.epoch, "lifecycle_seq": self.seq,
                    "cleanup_error": getattr(self, "cleanup_error", None)}

    def close(self):
        with self.lock:
            self.db.close()
            self.ledger.close()
