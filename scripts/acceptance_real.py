"""Real-provider acceptance using only synthetic conversations and media.

Answers go to an explicitly labelled synthetic fixture artifact, never to
diagnostic logs. API credentials are loaded into memory from the authorized file.
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
import wave

from PIL import Image, ImageDraw

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from acceptance_budget import AcceptanceBudget, AcceptanceProvider, REPORT
from acceptance_preflight import runtime_key
from acceptance_evidence import collect_evidence
from memory_core.retrieval import LocalEmbedding, VectorIndex
from memory_core.service import MemoryService
from memory_core.storage import connect
from memory_core.types import MemoryFault, Scope, uid
from memory_core.worker import ConsolidationWorker
from memory_lab.chat import ChatManager
from memory_lab.media import Uploads


class Run:
    def __init__(self, prefix="real"):
        self.run_id = prefix + "-" + time.strftime("%Y%m%d-%H%M%S")
        self.root = REPORT / self.run_id
        self.root.mkdir(parents=True)
        self.provenance=collect_evidence(self.root)
        self.budget = AcceptanceBudget()
        self.provider = AcceptanceProvider({
            "base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V4.1-Flash",
            "context_limit": 16384, "output_limit": 1024, "request_timeout": 90,
            "vision": True, "audio": False, "asr_model": "FunAudioLLM/SenseVoiceSmall",
        }, runtime_key(PROJECT/"临时apikey.txt"), self.budget)
        model = PROJECT/".local/caches/models/bge-m3"
        lock = json.loads((model/"model-lock.json").read_text(encoding="utf-8"))
        self.embedding = LocalEmbedding(PROJECT, model, lock["revision"])
        self.cases, self.fixtures = [], []

    def save(self):
        result = {"run_id": self.run_id, "data_kind": "synthetic-only", "model": self.provider.model,
                  "cases": self.cases, "passed": all(row["passed"] for row in self.cases),
                  "reserved_total_rmb": self.budget.data.get("reserved_total_rmb", 0),
                  "account_bill_verified": False}
        result["provenance"]=self.provenance
        (self.root/"report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.root/"synthetic-conversations.json").write_text(json.dumps({"notice": "Generated acceptance fixtures, not user records or diagnostics", "exchanges": self.fixtures}, ensure_ascii=False, indent=2), encoding="utf-8")
        (REPORT/"real-latest.json").write_text(json.dumps({"report": str(self.root/"report.json")}), encoding="utf-8")

    def check(self, name, passed, **evidence):
        self.cases.append({"name": name, "passed": bool(passed), **evidence})
        self.save()
        print(json.dumps({"case": name, "passed": bool(passed)}, ensure_ascii=False), flush=True)

    def service(self, name):
        service = MemoryService(PROJECT, self.root/name/"data", "persona_acceptance")
        service.set_persona("合成验收伙伴", "测试中的人名、地点、经历均为合成。请用简短中文回答，一般不超过150字。依据提供的原话区分历史与现状，不猜未提供的信息。")
        service.vector_index = VectorIndex(service.layout.persona_path("data", "indexes", "vectors"), self.embedding)
        chat = ChatManager(service, Uploads(PROJECT, service.run_id), lambda: self.provider)
        return service, chat

    def session(self, service):
        base = Scope(service.persona_id, "local_user")
        return Scope(base.persona_id, base.user_id, service.new_session(base))

    async def ask(self, service, chat, scope, text, attachments=()):
        started = time.perf_counter()
        rid = chat.start(scope, text, uid("idem"), attachments)
        await chat.requests[rid].task
        state = chat.state(scope, rid)
        if state["status"] != "completed":
            raise MemoryFault(state["error"] or state["status"], 502)
        answer = (service.read_messages(scope, [state["message_id"]])[0]["body"]
                  if state["recording"] else state["temporary_reply"]["body"])
        self.fixtures.append({"session_id": scope.session_id, "user": text, "assistant": answer,
                              "recording": state["recording"], "sources": state["sources"],
                              "seconds": time.perf_counter()-started})
        self.save()
        return answer, state, json.dumps(self.provider.messages_seen, ensure_ascii=False)

    async def index_all(self, service):
        worker = ConsolidationWorker(service, lambda: self.provider)
        for _ in range(30):
            if not await worker.once():
                break
        failed = service.db.execute("SELECT COUNT(*) FROM ltm_jobs WHERE status='failed'").fetchone()[0]
        self.check("summary-and-local-index", not failed, failed_jobs=failed)

    async def memory(self):
        s, chat = self.service("memory")
        try:
            scope = self.session(s)
            _, old, _ = await self.ask(s,chat,scope,"验收暗号是蓝鲸47，我把红伞放在北门。")
            await self.index_all(s)
            answer, changed, _ = await self.ask(s,chat,scope,"后来把伞移到了南门。")
            self.check("no-false-database-update-claim", not any(word in answer for word in ("已更新记录", "已归档", "已设置提醒")))
            output = BytesIO()
            image = Image.new("RGB", (320,240), "white")
            ImageDraw.Draw(image).ellipse((80,40,240,200), fill="blue")
            image.save(output,"PNG")
            (self.root/"synthetic-blue-circle.png").write_bytes(output.getvalue())
            image_scope = self.session(s)
            item = chat.uploads.put(image_scope, output.getvalue(), "blue-circle.png")
            image_answer, image_state, _ = await self.ask(s,chat,image_scope,"请只回答图片中圆形的颜色。", [item["id"]])
            self.check("real-image-input", "蓝" in image_answer,
                       media_sent=image_state["sources"][-1].get("media"))
            # Index the image in its own chunk without indexing the new umbrella update.
            image_row = s.read_messages(image_scope,[image_state["user_message_id"]])[0]
            cid = uid("chunk")
            from memory_core.worker import render_source
            s.db.execute("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",(cid,image_scope.user_id,image_scope.session_id,json.dumps([image_row["id"]]),json.dumps({image_row["id"]:image_row["revision"]}),render_source(image_row),s.vector_index.generation,"active"))
            s.vector_index.add([(cid,render_source(image_row))])
            fresh = self.session(s)
            answer, recalled, payload = await self.ask(s,chat,fresh,"我的红伞现在放在哪里？")
            ids = {row["message_id"] for row in recalled["sources"]}
            self.check("cross-session-latest-location", "南门" in answer and changed["user_message_id"] in ids)
            self.check("no-unrelated-image-context", image_row["id"] not in ids and "image_url" not in payload and "圆形" not in answer and "蓝色" not in answer)
            answer, _, _ = await self.ask(s,chat,fresh,"最初放在哪里，后来又放在哪里？")
            self.check("old-and-new-state-distinction", "北门" in answer and "南门" in answer)
            # Explicit correction is a separate operation, retaining the original.
            s.correct_memory(scope,changed["user_message_id"],"刚才南门是口误，实际放在西门。",kind="error")
            await chat.boundary()
            answer, _, _ = await self.ask(s,chat,self.session(s),"我的红伞目前在哪里？")
            self.check("explicit-correction-current-state", "西门" in answer)
            await self.index_all(s)
            suggestion=s.record_message(scope,"我上次建议的检验代号是白帆706。",uid("idem"),role="assistant")
            answer,state,payload=await self.ask(s,chat,self.session(s),"你上次建议的检验代号是什么？请核对助手原话。")
            self.check("explicit-assistant-quotation", "白帆706" in answer
                       and suggestion["id"] in {source["message_id"] for source in state["sources"]}
                       and "助手发言（不是现实事实证据）" in payload)
        finally:
            await chat.boundary()
            s.close()

    async def pause(self):
        s, chat = self.service("pause")
        scope = self.session(s)
        marker = "紫杉灯塔6842"
        try:
            await self.ask(s,chat,scope,"保存对照：测试花盆叫小石头，放在东窗。")
            count = s.stats(scope)["messages"]
            s.set_recording(False)
            await chat.boundary()
            await self.ask(s,chat,scope,"临时秘密代号是"+marker+"，盒子藏在西侧第三格。")
            answer, _, _ = await self.ask(s,chat,scope,"刚才临时秘密代号是什么？")
            self.check("pause-can-use-temporary-context", marker in answer)
            self.check("pause-no-durable-message-or-search", s.stats(scope)["messages"]==count and not s.search_messages(scope,marker))
            output=BytesIO()
            Image.new("RGB",(8,8),"green").save(output,"PNG")
            item=chat.uploads.put(scope,output.getvalue(),"temporary.png")
            temp_path=Path(chat.uploads.take(scope,[item["id"]])[0]["path"])
            await self.ask(s,chat,scope,"临时图片，请说出主要颜色。",[item["id"]])
            s.set_recording(True)
            await chat.boundary()
            self.check("pause-temporary-media-cleaned", not temp_path.exists() and not chat.temporary)
            answer,state,payload=await self.ask(s,chat,scope,"已保存的记录中有刚才的临时秘密代号和盒子位置吗？没有就说明不知道。")
            self.check("resume-does-not-carry-forward", marker not in answer and marker not in payload and not any(x["temporary"] for x in state["sources"]))
            exported=s.export(scope,include_media=True)
            export_path=s.layout.persona_path("exports",exported["id"])
            self.check("pause-not-in-export", all(marker not in (export_path/name).read_text(encoding="utf-8") for name in ("transcript.md","messages.jsonl","episodes.md")))
            snapshot=s.snapshot(scope)
            self.check("pause-not-in-snapshot", marker.encode() not in s.layout.persona_path("backups",snapshot["id"],"memory","metadata.db").read_bytes())
            await chat.boundary()
            s.close()
            s=MemoryService(PROJECT,self.root/"pause/data","persona_acceptance")
            self.check("pause-restart-keeps-only-saved-data", not s.search_messages(scope,marker) and bool(s.search_messages(scope,"小石头")))
        finally:
            await chat.boundary()
            s.close()

    async def voice(self):
        fixtures=REPORT/"fixtures"
        transcripts={}
        for name in ("umbrella-old","umbrella-new","negation","identifier","unrelated"):
            path=fixtures/(name+".wav")
            text,version=await self.provider.recognize("audio/wav",path.read_bytes(),path.name)
            transcripts[name]=text
        (self.root/"synthetic-transcripts.json").write_text(json.dumps(transcripts,ensure_ascii=False,indent=2),encoding="utf-8")
        self.check("asr-five-synthetic-recordings",all(bool(t.strip()) for t in transcripts.values()),models=[version])
        self.check("asr-location-and-negation", "北门" in transcripts["umbrella-old"] and "南门" in transcripts["umbrella-new"] and "不喜欢" in transcripts["negation"] and "不加糖" in transcripts["negation"])
        s,chat=self.service("voice")
        scope=self.session(s)
        recorded=[]
        try:
            for name in ("umbrella-old","umbrella-new"):
                content=(fixtures/(name+".wav")).read_bytes()
                item=chat.uploads.put(scope,content,name+".wav")
                row=s.record_message(scope,"【语音】",uid("idem"),attachments=chat.uploads.take(scope,[item["id"]]))
                chat.uploads.consume([item["id"]])
                s.recognize_attachment(scope,item["id"],transcripts[name],version,row["revision"])
                recorded.append((row,item,hashlib.sha256(content).hexdigest()))
            self.check("voice-transcript-fulltext-without-index",bool(s.search_messages(scope,"南门")))
            answer,state,payload=await self.ask(s,chat,self.session(s),"我在语音里说的红伞现在放在哪里？")
            self.check("voice-cross-session-latest-state","南门" in answer)
            sent=[m["sent"] for source in state["sources"] for m in source.get("media",[])]
            self.check("voice-chat-receives-transcript-not-raw-audio",bool(sent) and all(m=="recognition" for m in sent) and "input_audio" not in payload)
            snapshot=s.snapshot(scope)
            row,item,digest=recorded[-1]
            path,_=s.read_attachment(scope,item["id"])
            version_now=s.read_messages(scope,[row["id"]])[0]["revision"]
            s.recognize_attachment(scope,item["id"],"转写修正：红伞其实放在西门。","user-correction",version_now)
            self.check("voice-correction-preserves-original",hashlib.sha256(path.read_bytes()).hexdigest()==digest and bool(s.search_messages(scope,"西门")))
            deleted=s.delete_memory(scope,[r[0]["id"] for r in recorded])
            await chat.boundary()
            try:
                s.read_attachment(scope,item["id"])
                denied=False
            except MemoryFault:
                denied=True
            self.check("voice-recycle-blocks-history-and-playback",denied and not s.search_messages(scope,"西门"))
            s.restore_memory(scope,deleted["id"])
            self.check("voice-recycle-restore-keeps-id-and-bytes",hashlib.sha256(s.read_attachment(scope,item["id"])[0].read_bytes()).hexdigest()==digest)
            s.restore_snapshot(scope,snapshot["id"])
            self.check("voice-snapshot-restores-media-and-id",hashlib.sha256(s.read_attachment(scope,item["id"])[0].read_bytes()).hexdigest()==digest)
            original_path=s.read_attachment(scope,item["id"])[0]
            deleted=s.delete_memory(scope,[row["id"]])
            s.purge_memory(scope,deleted["id"])
            self.check("voice-permanent-cleanup",not original_path.exists())
        finally:
            await chat.boundary()
            s.close()
        silence=BytesIO()
        with wave.open(silence,"wb") as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b"\0"*32000*2)
        text,_=await self.provider.recognize("audio/wav",silence.getvalue(),"synthetic-silence.wav")
        self.fixtures.append({"synthetic_silence_transcript":text})
        self.check("asr-silence-does-not-invent-speech",not text.strip())

    async def run(self):
        for name in ("memory","pause","voice"):
            try:
                await getattr(self,name)()
            except Exception as error:
                self.check(name+"-flow-completed",False,error_type=type(error).__name__,reason=str(error) if isinstance(error,MemoryFault) else "see synthetic fixtures for the completed steps")
        self.save()
        print(json.dumps({"report":str(self.root/"report.json"),"cases":len(self.cases),"passed":sum(row["passed"] for row in self.cases),"reserved_rmb":self.budget.data.get("reserved_total_rmb",0)},ensure_ascii=False),flush=True)


if __name__=="__main__":
    asyncio.run(Run().run())
