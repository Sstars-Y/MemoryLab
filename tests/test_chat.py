import asyncio
import json

import pytest

from memory_core.types import MemoryFault
from memory_core.worker import ConsolidationWorker
from memory_core.retrieval import VectorIndex, HashEmbedding, hybrid_search
from memory_lab.chat import ChatManager
from memory_lab.media import Uploads
from memory_lab.providers import FakeProvider


def manager(lab, provider=None):
    s, scope = lab
    uploads = Uploads(s.layout.project, s.run_id)
    return ChatManager(s, uploads, lambda: provider or FakeProvider(delay=0)), scope


async def complete(chat, scope, text, key):
    request = chat.start(scope, text, key)
    await chat.requests[request].task
    return request, chat.state(scope, request)


async def test_completed_answers_and_transport_retry(lab):
    chat, scope = manager(lab)
    rid, state = await complete(chat, scope, "你好", "one")
    assert state["status"] == "completed"
    assert chat.start(scope, "你好", "one") == rid
    with pytest.raises(MemoryFault):
        chat.start(scope, "更换内容", "one")
    assert len(lab[0].history(scope)) == 2
    retried = chat.start(scope, "", "retry", retry_of=rid)
    await chat.requests[retried].task
    assert len(lab[0].history(scope)) == 3
    assert len(lab[0].delete_preview(scope, state["user_message_id"])) == 3


async def test_failure_and_stop_discard_draft(lab):
    chat, scope = manager(lab, FakeProvider(fail=True))
    _, state = await complete(chat, scope, "用户消息应保留", "fail")
    assert state["status"] == "failed"
    assert len(lab[0].history(scope)) == 1
    slow, scope = manager(lab, FakeProvider(delay=.05))
    rid = slow.start(scope, "停止测试", "stop")
    await asyncio.sleep(.08)
    slow.cancel(scope, rid)
    await slow.requests[rid].task
    assert slow.state(scope, rid)["draft"] == ""
    assert [r["role"] for r in lab[0].history(scope)] == ["user", "user"]


async def test_pause_never_consolidates_or_carries_forward(lab):
    s, scope = lab
    chat, _ = manager(lab)
    s.set_recording(False)
    await chat.boundary()
    _, state = await complete(chat, scope, "temporary_private_marker_90x", "temp")
    assert state["status"] == "completed" and not state["recording"]
    assert not s.history(scope)
    assert not s.db.execute("SELECT 1 FROM ltm_requests").fetchone()
    s.set_recording(True)
    await chat.boundary()
    _, state = await complete(chat, scope, "恢复保存", "after")
    assert state["status"] == "completed"
    assert not any(row["temporary"] for row in state["sources"])
    assert b"temporary_private_marker_90x" not in s.layout.database.read_bytes()


async def test_delete_during_generation_blocks_completion(lab):
    s, scope = lab
    chat, _ = manager(lab, FakeProvider(delay=.05))
    rid = chat.start(scope, "删除中的输入", "delete_while_generating")
    await asyncio.sleep(.08)
    mid = chat.requests[rid].user_message_id
    s.delete_memory(scope, [mid])
    await chat.requests[rid].task
    assert chat.state(scope, rid)["status"] == "failed"
    assert not s.history(scope)


async def test_worker_sources_and_invalidation(lab):
    s, scope = lab
    s.vector_index = VectorIndex(s.layout.persona_path("data", "indexes", "vectors"), HashEmbedding())
    s.record_message(scope, "小王今天一起修好了自行车", "bicycle")
    s.record_message(scope, "嗯", "short")
    worker = ConsolidationWorker(s, lambda: FakeProvider(delay=0))
    assert await worker.once()
    episode = s.db.execute("SELECT * FROM ltm_episodes WHERE status='active'").fetchone()
    assert episode is not None
    assert len(json.loads(episode["source_ids"])) == 2
    result = hybrid_search(s, scope, "自行车", 10)
    assert result["messages"]
    selected = result["messages"][0]["id"]
    s.delete_memory(scope, [selected])
    assert not s.db.execute("SELECT 1 FROM ltm_episodes WHERE status='active'").fetchone()
    assert all(row["id"] != selected for row in hybrid_search(s, scope, "自行车")["messages"])


async def test_late_worker_results_rejected(lab):
    s, scope = lab
    row = s.record_message(scope, "后台删除竞态", "race")
    entered, release = asyncio.Event(), asyncio.Event()
    class Delayed(FakeProvider):
        async def summarize(self, rows):
            entered.set()
            await release.wait()
            return "迟到摘要"
    worker = ConsolidationWorker(s, lambda: Delayed())
    task = asyncio.create_task(worker.once())
    await entered.wait()
    s.delete_memory(scope, [row["id"]])
    release.set()
    await task
    assert not s.db.execute("SELECT 1 FROM ltm_episodes WHERE status='active'").fetchone()
