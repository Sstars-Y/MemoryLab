from __future__ import annotations

from pathlib import Path
import json
import sqlite3

from .paths import guarded
from .types import MemoryFault, Scope, identifier, uid

RETENTION = 7 * 24 * 60 * 60


def remove_managed_tree(root: Path, target: Path):
    """Only call for known product/index/tmp roots. Check before traversal."""
    safe = guarded(root, str(target.relative_to(root)))
    if not safe.exists():
        return
    if safe.is_file():
        safe.unlink()
        return
    for entry in safe.iterdir():
        child = guarded(root, str(entry.relative_to(root)))
        remove_managed_tree(root, child)
    safe.rmdir()


class LifecycleMixin:
    def _invalidate(self, message_ids, purge=False):
        selected = set(message_ids)
        for table in ("ltm_chunks", "ltm_episodes", "ltm_products"):
            for row in self.db.execute(f"SELECT * FROM {table} WHERE status='active'").fetchall():
                if table == "ltm_products" and row["kind"] == "snapshot" and not purge:
                    continue
                if selected.intersection(json.loads(row["source_ids"])):
                    self.db.execute(f"UPDATE {table} SET status='invalid' WHERE id=?", (row["id"],))
        for mid in selected:
            self.db.execute("UPDATE ltm_jobs SET status='invalid' WHERE message_id=?", (mid,))

    def _operation(self, kind, user_id, message_ids, recycle_id, created, expires):
        # Intent is durable before updating the restorable DB. A failed application
        # remains replayable, and every read crosses barrier() before proceeding.
        self.ledger.execute("INSERT INTO operations(kind,target_ids,recycle_id,user_id,created,expires) VALUES(?,?,?,?,?,?)", (kind, json.dumps(message_ids), recycle_id, user_id, created, expires))
        self._replay()
        self.epoch += 1

    def _replay(self):
        applied = self.db.execute("SELECT value FROM ltm_meta WHERE key='lifecycle_seq'").fetchone()
        applied_seq = int(applied[0]) if applied else 0
        if applied_seq > self.seq:
            raise MemoryFault("生命周期清单比数据库旧，拒绝读取", 409)
        for op in self.ledger.execute("SELECT * FROM operations WHERE seq>? ORDER BY seq", (applied_seq,)).fetchall():
            targets = json.loads(op["target_ids"])
            with self.transaction():
                if op["kind"] == "delete":
                    self.db.execute("INSERT OR REPLACE INTO ltm_recycle VALUES(?,?,?,?,?,?,?)", (op["recycle_id"], op["user_id"], op["target_ids"], op["created"], op["expires"], "recycled", op["seq"]))
                    for mid in targets:
                        self.db.execute("UPDATE ltm_messages SET status='recycled',recycle_id=?,revision=revision+1 WHERE id=? AND user_id=? AND status='active'", (op["recycle_id"], mid, op["user_id"]))
                        self.db.execute("UPDATE ltm_attachments SET status='recycled' WHERE message_id=? AND user_id=? AND status='active'", (mid, op["user_id"]))
                elif op["kind"] == "restore":
                    self.db.execute("UPDATE ltm_recycle SET status='restored',seq=? WHERE id=?", (op["seq"], op["recycle_id"]))
                    for mid in targets:
                        self.db.execute("UPDATE ltm_messages SET status='active',recycle_id=NULL,revision=revision+1 WHERE id=? AND user_id=? AND status='recycled' AND recycle_id=?", (mid, op["user_id"], op["recycle_id"]))
                        row = self.db.execute("SELECT * FROM ltm_messages WHERE id=? AND status='active'", (mid,)).fetchone()
                        if row:
                            self.db.execute("UPDATE ltm_attachments SET status='active' WHERE message_id=? AND status='recycled'", (mid,))
                            self._refresh_search_index(mid)
                            self.db.execute("INSERT OR IGNORE INTO ltm_jobs(id,message_id,revision,run_id,seq,status) VALUES(?,?,?,?,?,'pending')", (uid("job"), mid, row["revision"], self.run_id, op["seq"]))
                elif op["kind"] == "purge":
                    self.db.execute("UPDATE ltm_recycle SET status='purge_pending',seq=? WHERE id=?", (op["seq"], op["recycle_id"]))
                    for mid in targets:
                        row = self.db.execute("SELECT pk FROM ltm_messages WHERE id=? AND user_id=?", (mid, op["user_id"])).fetchone()
                        if row:
                            self.db.execute("DELETE FROM ltm_messages_fts WHERE rowid=?", (row[0],))
                            self.db.execute("UPDATE ltm_messages SET status='purged',body='',revision=revision+1 WHERE id=?", (mid,))
                            self.db.execute("UPDATE ltm_attachments SET status='purge_pending',recognition=NULL,recognition_version=NULL WHERE message_id=?", (mid,))
                            self.db.execute("DELETE FROM ltm_revisions WHERE target_id=?", (mid,))
                            self.db.execute("DELETE FROM ltm_jobs WHERE message_id=?", (mid,))
                else:
                    raise MemoryFault("生命周期操作类型未知", 409)
                self._invalidate(targets, purge=op["kind"] == "purge")
                if op["kind"] == "restore":
                    for mid in targets:
                        self.db.execute("UPDATE ltm_jobs SET status='pending',run_id=? WHERE message_id=? AND revision=(SELECT revision FROM ltm_messages WHERE id=? AND status='active')", (self.run_id, mid, mid))
                self.db.execute("INSERT OR REPLACE INTO ltm_meta VALUES('lifecycle_seq',?)", (str(op["seq"]),))
            self.ledger.execute("UPDATE operations SET applied=1 WHERE seq=?", (op["seq"],))
        self._clean_invalid_products()
        self._finish_purges()

    def _clean_invalid_products(self):
        self.cleanup_error = None
        for row in self.db.execute("SELECT * FROM ltm_products WHERE status='invalid'").fetchall():
            target = self.layout.persona_path("backups" if row["kind"] == "snapshot" else "exports", identifier(row["id"]))
            try:
                remove_managed_tree(self.layout.persona, target)
                self.db.execute("UPDATE ltm_products SET status='removed' WHERE id=?", (row["id"],))
            except (OSError, MemoryFault) as error:
                self.cleanup_error = type(error).__name__

    def _finish_purges(self):
        pending = self.db.execute("SELECT * FROM ltm_recycle WHERE status='purge_pending'").fetchall()
        if not pending:
            return
        for item in pending:
            try:
                self._purge_one(item)
            except (OSError, MemoryFault, sqlite3.Error) as error:
                self.cleanup_error = type(error).__name__

    def _purge_one(self, item):
            mids = json.loads(item["message_ids"])
            for product in self.db.execute("SELECT source_ids FROM ltm_products WHERE status='invalid'"):
                if set(mids).intersection(json.loads(product["source_ids"])):
                    raise MemoryFault("受管副本仍待清理", 409)
            for mid in mids:
                for attachment in self.db.execute("SELECT * FROM ltm_attachments WHERE message_id=? AND status='purge_pending'", (mid,)).fetchall():
                    self.layout.persona_path(attachment["relative_path"]).unlink(missing_ok=True)
                    self.db.execute("DELETE FROM ltm_attachments WHERE id=?", (attachment["id"],))
            # Remove text from invalid derived rows, and all physical vector generations.
            self.db.execute("DELETE FROM ltm_chunks WHERE status!='active'")
            self.db.execute("DELETE FROM ltm_episodes WHERE status!='active'")
            vector_root = self.layout.persona_path("data", "indexes", "vectors")
            for child in vector_root.iterdir():
                remove_managed_tree(vector_root, child)
            if self.vector_index:
                self.vector_index.clear_memory()
            self.db.execute("UPDATE ltm_chunks SET status='invalid' WHERE status='active'")
            self.db.execute("UPDATE ltm_jobs SET status='pending',run_id=? WHERE status='done'", (self.run_id,))
            # WAL and freelists are managed storage too; compact before declaring done.
            self.db.execute("INSERT INTO ltm_messages_fts(ltm_messages_fts) VALUES('optimize')")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("UPDATE ltm_recycle SET status='purged' WHERE id=?", (item["id"],))

    def expire(self):
        if getattr(self, "_expiring", False):
            return
        self._expiring = True
        try:
            for item in self.db.execute("SELECT * FROM ltm_recycle WHERE status='recycled' AND expires<=?", (self.clock(),)).fetchall():
                self._operation("purge", item["user_id"], json.loads(item["message_ids"]), item["id"], self.clock(), item["expires"])
        finally:
            self._expiring = False

    def delete_preview(self, scope: Scope, message_id: str, whole_turn=True):
        with self.lock:
            row = self.read_messages(scope, [message_id])[0]
            if not whole_turn:
                return [row]
            mids = [r[0] for r in self.db.execute("SELECT id FROM ltm_messages WHERE user_id=? AND session_id=? AND turn_id=? AND status='active' ORDER BY ordinal", (scope.user_id, row["session_id"], row["turn_id"]))]
            return self.read_messages(scope, mids)

    def delete_memory(self, scope: Scope, message_ids: list[str]):
        if not message_ids or len(message_ids) > 200:
            raise MemoryFault("请选择 1 到 200 条消息")
        with self.lock:
            self.read_messages(scope, message_ids)
            recycle_id = uid("recycle")
            created = self.clock()
            self._operation("delete", scope.user_id, list(dict.fromkeys(message_ids)), recycle_id, created, created+RETENTION)
            return {"id": recycle_id, "expires": created+RETENTION, "message_ids": message_ids, "status": "recycled"}

    def recycle_items(self, scope: Scope):
        with self.lock:
            self.check(scope)
            self.barrier()
            result = []
            for item in self.db.execute("SELECT * FROM ltm_recycle WHERE user_id=? ORDER BY created DESC LIMIT 200", (scope.user_id,)):
                obj = dict(item)
                obj["message_ids"] = json.loads(obj["message_ids"])
                obj["messages"] = []
                if item["status"] == "recycled":
                    for mid in obj["message_ids"]:
                        row = self.db.execute("SELECT * FROM ltm_messages WHERE id=? AND user_id=? AND status='recycled' AND recycle_id=?", (mid, scope.user_id, item["id"])).fetchone()
                        if row:
                            obj["messages"].append(self._message(row))
                result.append(obj)
            return result

    def _recycle(self, scope, recycle_id):
        self.check(scope)
        self.barrier()
        row = self.db.execute("SELECT * FROM ltm_recycle WHERE id=? AND user_id=?", (identifier(recycle_id), scope.user_id)).fetchone()
        if not row or row["status"] != "recycled":
            raise MemoryFault("回收站记录不存在、已到期或已处理", 409)
        return row

    def restore_memory(self, scope: Scope, recycle_id: str):
        with self.lock:
            row = self._recycle(scope, recycle_id)
            self._operation("restore", scope.user_id, json.loads(row["message_ids"]), row["id"], self.clock(), row["expires"])
            return {"id": recycle_id, "status": "restored"}

    def purge_memory(self, scope: Scope, recycle_id: str):
        with self.lock:
            row = self._recycle(scope, recycle_id)
            self._operation("purge", scope.user_id, json.loads(row["message_ids"]), row["id"], self.clock(), row["expires"])
            status = self.db.execute("SELECT status FROM ltm_recycle WHERE id=?", (recycle_id,)).fetchone()[0]
            return {"id": recycle_id, "status": status}

    def clear_recycle(self, scope: Scope):
        with self.lock:
            self.check(scope)
            self.barrier()
            items = self.db.execute("SELECT id FROM ltm_recycle WHERE user_id=? AND status='recycled'", (scope.user_id,)).fetchall()
            for item in items:
                self.purge_memory(scope, item[0])
            pending = self.db.execute("SELECT COUNT(*) FROM ltm_recycle WHERE user_id=? AND status='purge_pending'", (scope.user_id,)).fetchone()[0]
            return {"status": "purge_pending" if pending else "purged", "count": len(items), "pending": pending}
