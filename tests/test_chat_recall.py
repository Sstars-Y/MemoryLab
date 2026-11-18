"""Synthetic cross-session recall regressions; no real model or user records."""
import json
from io import BytesIO
import wave
import numpy as np

from PIL import Image
import pytest

from memory_core.types import Scope
from memory_core.types import MemoryFault
from memory_lab.chat import ChatManager
from memory_lab.media import Uploads
from memory_lab.providers import FakeProvider
from memory_core.recall import rerank


class CapturingProvider(FakeProvider):
    vision = True

    async def generate(self, messages):
        self.messages = messages
        yield "合成回复"


class FixedCandidates:
    generation = "synthetic-ranked-candidates"

    def __init__(self, hits):
        self.hits = hits

    def search(self, query, limit=100):
        return self.hits[:limit]


def index_candidates(service, rows):
    hits = []
    for row, score in rows:
        cid = "chunk_" + row["id"]
        service.db.execute(
            "INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",
            (cid, row["user_id"], row["session_id"], json.dumps([row["id"]]),
             json.dumps({row["id"]: row["revision"]}), row["body"],
             FixedCandidates.generation, "active"),
        )
        hits.append((cid, score))
    service.vector_index = FixedCandidates(hits)


async def ask(service, text):
    scope = Scope(service.persona_id, "local_user")
    scope = Scope(scope.persona_id, scope.user_id, service.new_session(scope))
    provider = CapturingProvider(delay=0)
    chat = ChatManager(service, Uploads(service.layout.project, service.run_id), lambda: provider)
    rid = chat.start(scope, text, "new_question")
    await chat.requests[rid].task
    state = chat.state(scope, rid)
    assert state["status"] == "completed", state["error"]
    return state, provider


@pytest.mark.parametrize("update", ["后来把伞移到了南门", "后来把它移到了南门"])
async def test_recall_includes_unindexed_later_update(lab, update):
    service, scope = lab
    old = service.record_message(scope, "我的红伞放在北门", "old")
    latest = service.record_message(scope, update, "latest")
    # Only the old state has reached the asynchronous semantic index.
    index_candidates(service, [(old, .9)])
    state, provider = await ask(service, "我的伞现在在哪？")
    assert latest["id"] in {source["message_id"] for source in state["sources"]}
    assert "南门" in provider.messages[1]["content"]


async def test_unrelated_other_session_image_is_not_injected(lab):
    service, scope = lab
    old = service.record_message(scope, "我的红伞放在北门", "old")
    picture_scope = Scope(service.persona_id, scope.user_id, service.new_session(scope))
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, "PNG")
    uploads = Uploads(service.layout.project, service.run_id)
    item = uploads.put(picture_scope, output.getvalue(), "synthetic.png")
    picture = service.record_message(picture_scope, "看看这张图片", "picture",
                                     attachments=uploads.take(picture_scope, [item["id"]]))
    index_candidates(service, [(old, .9), (picture, .04)])
    state, provider = await ask(service, "我的伞现在在哪？")
    assert picture["id"] not in {source["message_id"] for source in state["sources"]}
    assert item["id"] not in json.dumps(provider.messages, ensure_ascii=False)
    assert not isinstance(provider.messages[-1]["content"], list)


async def test_unrelated_media_in_same_vector_chunk_is_not_injected(lab):
    service, scope = lab
    old = service.record_message(scope, "我的红伞放在北门", "old")
    uploads = Uploads(service.layout.project, service.run_id)
    output = BytesIO()
    Image.new("RGB", (8,8), "blue").save(output, "PNG")
    item = uploads.put(scope, output.getvalue(), "other-topic.png")
    picture = service.record_message(scope, "换个话题，看看这张图片", "picture",
                                     attachments=uploads.take(scope, [item["id"]]))
    refs = [old["id"], picture["id"]]
    service.db.execute("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",
        ("chunk_shared", scope.user_id, scope.session_id, json.dumps(refs),
         json.dumps({mid:1 for mid in refs}), "synthetic exchange", FixedCandidates.generation, "active"))
    service.vector_index = FixedCandidates([("chunk_shared", .9)])
    state, provider = await ask(service, "我的伞现在在哪？")
    assert old["id"] in {source["message_id"] for source in state["sources"]}
    assert picture["id"] not in {source["message_id"] for source in state["sources"]}


async def test_unrelated_followup_is_not_mistaken_for_a_change(lab):
    service, scope = lab
    old = service.record_message(scope, "我的红伞放在北门", "old")
    service.record_message(scope, "换个话题，今天整理了书柜", "other")
    unrelated = service.record_message(scope, "后来把它移到了客厅", "other_update")
    index_candidates(service, [(old, .9)])
    state, _ = await ask(service, "我的伞现在在哪？")
    assert unrelated["id"] not in {source["message_id"] for source in state["sources"]}


async def test_recall_survives_semantic_provider_memory_fault(lab):
    service, scope = lab
    old = service.record_message(scope, "我的红伞放在北门", "old")
    index_candidates(service, [(old, .9)])

    class UnavailableEmbedding:
        def encode(self, texts):
            raise MemoryFault("本地模型暂不可用", 503)

    service.vector_index.embedding = UnavailableEmbedding()
    state, provider = await ask(service, "我的红伞在哪？")
    assert old["id"] in {row["message_id"] for row in state["sources"]}
    assert "北门" in provider.messages[1]["content"]


async def test_recall_keeps_latest_when_many_old_text_matches_tie(lab):
    service,scope=lab
    for i in range(100):
        service.record_message(scope,"红伞原先在北门",f"old_{i}")
    latest=service.record_message(scope,"红伞后来在南门","latest")
    state,provider=await ask(service,"我的红伞在哪里？")
    assert latest["id"] in {row["message_id"] for row in state["sources"]}


@pytest.mark.parametrize("recording",[True,False])
async def test_model_receives_host_saving_receipt_matching_persistence(lab,recording):
    service,_=lab
    service.set_recording(recording)
    state,provider=await ask(service,"请确认这条消息的保存状态")
    system=provider.messages[0]["content"]
    assert "宿主保存回执" in system
    if recording:
        assert "已由应用保存" in system
        assert service.db.execute("SELECT 1 FROM ltm_messages WHERE id=?",(state["user_message_id"],)).fetchone()
    else:
        assert "未写入长期记录" in system
        assert state["user_message_id"] is None
        assert service.db.execute("SELECT COUNT(*) FROM ltm_messages").fetchone()[0]==0


@pytest.mark.parametrize("transcript,expected",[("补充刚才的内容，后来把红伞移到了南门。",True),("换个话题，蓝箱放在客厅。",False)])
async def test_recall_reads_voice_followup_after_semantic_rerank(lab,transcript,expected):
    service,scope=lab
    output=BytesIO()
    with wave.open(output,"wb") as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b"\0"*3200)
    uploads=Uploads(service.layout.project,service.run_id)
    rows=[]
    for i,text in enumerate(("红伞放在北门。",transcript)):
        item=uploads.put(scope,output.getvalue(),f"synthetic-{i}.wav")
        row=service.record_message(scope,"【语音】",f"voice_{i}",attachments=uploads.take(scope,[item["id"]]))
        service.recognize_attachment(scope,item["id"],text,"synthetic-recognition",row["revision"])
        rows.append(service.read_messages(scope,[row["id"]])[0])
    index_candidates(service,[(rows[0],.9)])

    class OldStateOnlyEmbedding:
        def encode(self,texts):
            # Reproduce the actual failure: the old wording scores higher than
            # the update, even though both are lexical candidates.
            return np.asarray([[1,0] if i==0 or "北门" in text else [.5,.866] for i,text in enumerate(texts)],dtype=np.float32)

    service.vector_index.embedding=OldStateOnlyEmbedding()
    state,_=await ask(service,"我在语音里说的红伞现在放在哪里？")
    assert (rows[1]["id"] in {source["message_id"] for source in state["sources"]}) is expected


def test_rerank_keeps_repeated_source_identities_and_distinguishes_derived_text(lab):
    service,_=lab

    class LocationEmbedding:
        def encode(self,texts):
            return np.asarray([[0,1] if "南门" in text else [1,0] for text in texts],dtype=np.float32)

    class Index:
        embedding=LocationEmbedding()

    service.vector_index=Index()
    rows=[{"id":f"source_{i}","body":"红伞在北门","corrections":[],"attachments":[]} for i in range(4)]
    rows[2]["corrections"]=[{"correction":"实际在南门"}]
    rows[3]["attachments"]=[{"recognition":"红伞在南门"}]
    assert [row["id"] for row in rerank(service,"原来的入口",rows)]==["source_0","source_1"]


@pytest.mark.parametrize("query,expected",[("你上次建议的测试代号是什么？",True),("我的测试代号是什么？",False)])
async def test_explicit_question_can_recall_assistant_words_without_promoting_them_to_facts(lab,query,expected):
    service,scope=lab
    reply=service.record_message(scope,"我建议测试代号使用蓝雀735。","past_suggestion",role="assistant")
    index_candidates(service,[(reply,.9)])
    state,provider=await ask(service,query)
    assert (reply["id"] in {row["message_id"] for row in state["sources"]}) is expected
    if expected:
        assert "助手发言（不是现实事实证据）" in provider.messages[1]["content"]


async def test_quotation_instructions_do_not_displace_the_substantive_exact_source(lab):
    service,scope=lab
    old=service.record_message(scope,"我复述的验收暗号是蓝鲸47。","old_quote",role="assistant")
    expected=service.record_message(scope,"我上次建议的检验代号是白帆706。","new_quote",role="assistant")
    index_candidates(service,[(old,.99),(expected,.6)])

    class DistractedEmbedding:
        def encode(self,texts):
            return np.asarray([[.6,.8] if "白帆" in text else [1,0] for text in texts],dtype=np.float32)

    service.vector_index.embedding=DistractedEmbedding()
    state,_=await ask(service,"你上次建议的检验代号是什么？请核对助手原话。")
    assert expected["id"] in {source["message_id"] for source in state["sources"]}
