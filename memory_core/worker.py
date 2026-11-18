from __future__ import annotations

import asyncio
import hashlib
import json

from .types import MemoryFault, Scope, uid


class ConsolidationWorker:
    def __init__(self, service, provider_getter):
        self.service = service
        self.provider_getter = provider_getter
        self.running = False
        self.task = None
        self.last_error = None

    def start(self):
        self.running = True
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def loop(self):
        while self.running:
            try:
                did_work = await self.once()
                if not did_work:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = type(error).__name__
                await asyncio.sleep(3)

    async def once(self):
        s = self.service
        with s.lock:
            s.barrier()
            job = s.db.execute("SELECT j.*,m.user_id,m.session_id,m.ordinal FROM ltm_jobs j JOIN ltm_messages m ON m.id=j.message_id WHERE j.status='pending' AND m.status='active' ORDER BY m.pk LIMIT 1").fetchone()
            if not job:
                return False
            scope = Scope(s.persona_id, job["user_id"], job["session_id"], s.run_id)
            # A small continuous block; all source IDs remain associated, including short replies.
            raw = s.db.execute("SELECT * FROM ltm_messages WHERE user_id=? AND session_id=? AND status='active' AND ordinal BETWEEN ? AND ? ORDER BY ordinal", (scope.user_id, scope.session_id, max(1, job["ordinal"]-5), job["ordinal"]+5)).fetchall()
            rows, length = [], 0
            for row in raw:
                if rows and length+len(row["body"]) > 6000:
                    break
                rows.append(s._message(row))
                length += len(row["body"])
            # Ensure the target is included even when preceding messages filled the budget.
            if job["message_id"] not in {r["id"] for r in rows}:
                rows = [s.read_messages(scope, [job["message_id"]])[0]]
            ids = [row["id"] for row in rows]
            versions = {row["id"]: row["revision"] for row in rows}
            captured_seq, captured_epoch, run_id = s.seq, s.epoch, s.run_id
            for mid in ids:
                s.db.execute("UPDATE ltm_jobs SET status='running',attempts=attempts+1,run_id=? WHERE message_id=? AND revision=? AND status='pending'", (run_id, mid, versions[mid]))
        body = "\n".join(render_source(row) for row in rows)
        chunk_id = "chunk_"+hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()[:32]
        generation = s.vector_index.generation if s.vector_index else "none"
        vector = None
        try:
            if s.vector_index:
                vector = await asyncio.to_thread(s.vector_index.embedding.encode, [body])
            provider = self.provider_getter()
            summary = await provider.summarize(rows)
            from .types import reject_secrets
            reject_secrets(summary)
            with s.lock:
                s.barrier()
                if run_id != s.run_id or captured_epoch != s.epoch or captured_seq != s.seq:
                    raise MemoryFault("后台任务所属上下文已变化", 409)
                current = s.read_messages(scope, ids)
                if any(row["revision"] != versions[row["id"]] for row in current):
                    raise MemoryFault("后台任务来源已变化", 409)
                if vector is not None:
                    s.vector_index.write([chunk_id], vector)
                with s.transaction():
                    s.db.execute("INSERT OR REPLACE INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)", (chunk_id, scope.user_id, scope.session_id, json.dumps(ids), json.dumps(versions), body, generation, "active"))
                    s.db.execute("INSERT OR REPLACE INTO ltm_episodes VALUES(?,?,?,?,?,?,?,?)", ("episode_"+chunk_id[6:], scope.user_id, json.dumps(ids), json.dumps(versions), summary, None, s.clock(), "active"))
                    for mid in ids:
                        s.db.execute("UPDATE ltm_jobs SET status='done',error=NULL WHERE message_id=? AND revision=? AND status='running'", (mid, versions[mid]))
                if s.vector_index:
                    active = {r[0] for r in s.db.execute("SELECT id FROM ltm_chunks WHERE status='active' AND generation=?", (generation,))}
                    s.vector_index.compact(active)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            with s.lock:
                for mid in ids:
                    s.db.execute("UPDATE ltm_jobs SET status='failed',error=? WHERE message_id=? AND revision=? AND status='running'", (type(error).__name__, mid, versions[mid]))
            self.last_error = type(error).__name__
            return True

    def retry(self, scope):
        with self.service.lock:
            self.service.check(scope)
            self.service.db.execute("UPDATE ltm_jobs SET status='pending',error=NULL,run_id=? WHERE status IN ('failed','invalid') AND message_id IN (SELECT id FROM ltm_messages WHERE user_id=? AND status='active')", (self.service.run_id, scope.user_id))


def render_source(row):
    who = "用户原话" if row["role"] == "user" else "助手发言（不是现实事实证据）"
    text = f"[{row['id']} · {who} · {row['created']}] {row['body']}"
    for correction in row.get("corrections", []):
        label = "原记录错误，当前采用" if correction["kind"] == "error" else "历史变化／识别修订"
        text += f"\n[{label}，事件时间：{correction.get('event_time') or '未知'}] {correction['correction']}"
    for attachment in row.get("attachments", []):
        text += f"\n[附件 {attachment['id']} · {attachment['mime']}]"
        if attachment.get("recognition"):
            text += f" 识别文字（可能有误）：{attachment['recognition']}"
        else:
            text += " 尚未识别；不能声称看过或听过内容"
    return text
