"""Real-model voice checks through application HTTP routes using ASGI transport.

All data is synthetic and isolated. This does not claim a browser file-picker,
speaker playback, microphone capture, or TCP server test.
"""
import asyncio
import hashlib
import json
import time

import httpx

from acceptance_real import PROJECT, REPORT, Run
from memory_core.types import MemoryFault, uid
from memory_lab.server import create_app


async def verify():
    run=Run(prefix="http")
    service,_=run.service("application")
    app=create_app(PROJECT,service.layout.data,service.persona_id,"synthetic-bootstrap",
                   port=8877,start_worker=False,service=service)
    app.state.runtime["provider"]=run.provider
    app.state.runtime["config"]["mode"]="remote"
    base="http://127.0.0.1:8877"
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=base,timeout=180)

    async def request(method,path,**kwargs):
        response=await client.request(method,path,**kwargs)
        if response.status_code!=200:
            raise MemoryFault(f"HTTP acceptance route {path}: status {response.status_code}",502)
        return response

    async def session():
        return (await request("POST","/api/sessions",json={})).json()["id"]

    async def ask(sid,text,attachments=()):
        payload={"session_id":sid,"text":text,"attachments":list(attachments),"idempotency_key":uid("http_idem")}
        started=(await request("POST","/api/chat",json=payload)).json()
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            state=(await request("GET","/api/requests/"+started["request_id"],params={"session_id":sid})).json()
            if state["status"]=="completed":
                history=(await request("GET","/api/sessions/"+sid)).json()["messages"]
                answer=next(row["body"] for row in history if row["id"]==state["message_id"])
                run.fixtures.append({"session_id":sid,"user":text,"assistant":answer,"sources":state["sources"]})
                run.save()
                return state,answer,payload
            if state["status"] not in ("pending","running"):
                raise MemoryFault("HTTP chat did not complete: "+state["status"],502)
            await asyncio.sleep(.1)
        raise MemoryFault("HTTP chat completion timeout",504)

    try:
        client.headers.update({"Origin":base})
        await request("POST","/api/bootstrap",json={"token":"synthetic-bootstrap"})
        state=(await request("GET","/api/state")).json()
        client.headers.update({"X-CSRF-Token":state["csrf"]})
        run.check("http-bootstrap-and-csrf",bool(state["csrf"]))
        sid=await session()
        content=(REPORT/"fixtures/umbrella-old.wav").read_bytes()
        digest=hashlib.sha256(content).hexdigest()
        upload=(await request("POST","/api/uploads",params={"session_id":sid,"filename":"synthetic-umbrella.wav"},content=content)).json()
        first,receipt,payload=await ask(sid,"保存这条合成语音，稍后再识别。请确认应用是否已保存原文件。",[upload["id"]])
        run.check("http-model-confirms-host-saving-receipt","已保存" in receipt and not any(word in receipt for word in ("未实际保存","尚未保存","未保存")))
        duplicate=(await request("POST","/api/chat",json=payload)).json()
        run.check("http-upload-save-and-idempotent-retry",duplicate["request_id"]==first["id"])
        playback=await request("GET","/api/attachments/"+upload["id"])
        run.check("http-playback-bytes-and-mime",hashlib.sha256(playback.content).hexdigest()==digest and playback.headers["content-type"].startswith("audio/wav"))
        recognition=(await request("POST","/api/attachments/"+upload["id"]+"/recognize",json={})).json()
        run.fixtures.append({"synthetic_asr":recognition["text"]})
        run.check("http-real-asr",all(word in recognition["text"] for word in ("红伞","北门")))
        found=(await request("GET","/api/search",params={"q":"红伞"})).json()["messages"]
        run.check("http-recognition-fulltext-search",first["user_message_id"] in {row["id"] for row in found})
        latest,_,_=await ask(sid,"后来把它移到了南门。")
        fresh=await session()
        recalled,answer,_=await ask(fresh,"我的红伞现在放在哪里？")
        ids={row["message_id"] for row in recalled["sources"]}
        run.check("http-voice-cross-session-update", "南门" in answer and first["user_message_id"] in ids and latest["user_message_id"] in ids)
        sent=json.dumps(run.provider.messages_seen,ensure_ascii=False)
        run.check("http-voice-sends-transcript",recognition["text"] in sent and "input_audio" not in sent)
        sources=(await request("GET","/api/requests/"+recalled["id"]+"/sources",params={"session_id":fresh})).json()["sources"]
        run.check("http-sources-include-original-session",any(row["message_id"]==first["user_message_id"] and row["session_id"]==sid for row in sources))
        await request("POST","/api/attachments/"+upload["id"]+"/correction",json={"text":"合成转写修订：红伞在北门，校验词为薄荷9051。"})
        found=(await request("GET","/api/search",params={"q":"薄荷9051"})).json()["messages"]
        playback=await request("GET","/api/attachments/"+upload["id"])
        run.check("http-transcript-correction-preserves-original",bool(found) and hashlib.sha256(playback.content).hexdigest()==digest)
        snapshot=(await request("POST","/api/snapshots",json={})).json()
        deleted=(await request("POST","/api/forget",json={"message_ids":[first["user_message_id"]]})).json()
        denied=await client.get("/api/attachments/"+upload["id"])
        recycled=await request("GET","/api/recycle-attachments/"+upload["id"])
        run.check("http-recycle-restricts-normal-playback",denied.status_code==404 and hashlib.sha256(recycled.content).hexdigest()==digest)
        await request("POST","/api/recycle/"+deleted["id"]+"/restore",json={})
        restored=await request("GET","/api/attachments/"+upload["id"])
        run.check("http-recycle-restore-stable-attachment",hashlib.sha256(restored.content).hexdigest()==digest)
        await request("POST","/api/snapshots/"+snapshot["id"]+"/restore",json={})
        restored=await request("GET","/api/attachments/"+upload["id"])
        found=(await request("GET","/api/search",params={"q":"薄荷9051"})).json()["messages"]
        run.check("http-snapshot-voice-and-index-restoration",bool(found) and hashlib.sha256(restored.content).hexdigest()==digest,
                  search_found=bool(found),original_bytes_unchanged=hashlib.sha256(restored.content).hexdigest()==digest)
    except Exception as error:
        run.check("http-flow-completed",False,error_type=type(error).__name__,reason=str(error) if isinstance(error,MemoryFault) else "Synthetic HTTP acceptance step failed")
    finally:
        await app.state.chat.boundary()
        await client.aclose()
        service.close()
        run.save()
        print(json.dumps({"report":str(run.root/"report.json"),"passed":sum(c["passed"] for c in run.cases),"total":len(run.cases)}),flush=True)


if __name__=="__main__":
    asyncio.run(verify())
