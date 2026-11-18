import json
from pathlib import Path
import subprocess
import sys
import uuid

from memory_core.service import MemoryService
from memory_core.types import Scope


def test_process_crash_keeps_acknowledged_user_and_drops_draft():
    project=Path(__file__).resolve().parents[1]
    data=project/".local/verification"/("crash_"+uuid.uuid4().hex)
    code="""
import asyncio,os,sys
from pathlib import Path
from memory_core.service import MemoryService
from memory_core.types import Scope
from memory_lab.chat import ChatManager
from memory_lab.media import Uploads
from memory_lab.providers import FakeProvider
async def main():
 s=MemoryService(Path(sys.argv[1]),Path(sys.argv[2]),'p')
 session=s.new_session(Scope('p','local_user'))
 scope=Scope('p','local_user',session)
 c=ChatManager(s,Uploads(s.layout.project,s.run_id),lambda:FakeProvider(delay=1))
 c.start(scope,'saved_user_before_crash','crash_idem')
 await asyncio.sleep(.05)
 os._exit(17)
asyncio.run(main())
"""
    child=subprocess.run([sys.executable,"-c",code,str(project),str(data)],cwd=project)
    assert child.returncode == 17
    s=MemoryService(project,data,"p")
    try:
        session=s.sessions(Scope("p","local_user"))[0]["id"]
        messages=s.history(Scope("p","local_user",session))
        assert len(messages)==1 and messages[0]["role"]=="user"
        assert s.db.execute("SELECT status FROM ltm_requests").fetchone()[0]=="interrupted"
    finally:
        s.close()


def test_restore_intent_replayed_before_opening_database(lab,monkeypatch):
    import memory_core.products as products
    s,scope=lab
    first=s.record_message(scope,"快照内原文","first")
    snapshot=s.snapshot(scope)
    s.record_message(scope,"快照后消息","later")
    original=products.recover_restore
    def simulated_crash(layout):
        raise RuntimeError("simulated process termination after durable restore intent")
    monkeypatch.setattr(products,"recover_restore",simulated_crash)
    import pytest
    with pytest.raises(RuntimeError):
        s.restore_snapshot(scope,snapshot["id"])
    monkeypatch.setattr(products,"recover_restore",original)
    restored=MemoryService(s.layout.project,s.layout.data,s.persona_id)
    try:
        assert [m["id"] for m in restored.history(scope)]==[first["id"]]
        assert not restored.layout.path("restore-intents",s.persona_id+".json").exists()
    finally:
        restored.close()
        # Restore the fixture's connection so its cleanup remains valid.
        from memory_core.storage import connect
        s.db=connect(s.layout.database)
