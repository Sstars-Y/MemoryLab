from io import BytesIO
import json
import zipfile
import shutil
import sqlite3
import uuid

from PIL import Image
import pytest

from memory_core.types import MemoryFault
from memory_lab.media import Uploads


def add_image(s,scope):
    output=BytesIO()
    Image.new("RGB",(8,8),"blue").save(output,"PNG")
    uploads=Uploads(s.layout.project,s.run_id)
    item=uploads.put(scope,output.getvalue(),"image.png")
    row=s.record_message(scope,"合成附件原文","media",attachments=uploads.take(scope,[item["id"]]))
    return row,item,output.getvalue()


def test_snapshot_media_roundtrip_and_export_zip(lab):
    s,scope=lab
    row,item,content=add_image(s,scope)
    export=s.export(scope,include_media=True,include_persona=True)
    with zipfile.ZipFile(s.export_archive(scope,export["id"])) as bundle:
        assert bundle.read("media/"+item["id"]+".png")==content
        assert "persona.md" in bundle.namelist()
        assert not any("users/" in name for name in bundle.namelist())
    snapshot=s.snapshot(scope)
    recycle=s.delete_memory(scope,[row["id"]])
    s.restore_snapshot(scope,snapshot["id"])
    with pytest.raises(MemoryFault):
        s.read_attachment(scope,item["id"])
    assert s.read_attachment(scope,item["id"],recycle=True)[0].read_bytes()==content
    s.restore_memory(scope,recycle["id"])
    assert s.read_attachment(scope,item["id"])[0].read_bytes()==content


def test_missing_media_refuses_snapshot_and_corrupt_media_refuses_restore(lab):
    s,scope=lab
    row,item,content=add_image(s,scope)
    snapshot=s.snapshot(scope)
    snapshot_media=s.layout.persona_path("backups",snapshot["id"],"media","images",item["id"]+".png")
    snapshot_media.unlink()
    with pytest.raises(MemoryFault):
        s.verify_snapshot(scope,snapshot["id"])
    source,_=s.read_attachment(scope,item["id"])
    source.unlink()
    with pytest.raises(MemoryFault):
        s.snapshot(scope)


def test_recognition_versions_do_not_change_media(lab):
    s,scope=lab
    row,item,content=add_image(s,scope)
    s.recognize_attachment(scope,item["id"],"蓝色合成图片","test-recognition",row["revision"])
    assert s.read_attachment(scope,item["id"])[0].read_bytes()==content
    with pytest.raises(MemoryFault,match="版本已失效"):
        s.recognize_attachment(scope,item["id"],"迟到结果","old",row["revision"])


def test_recognition_is_searchable_without_embedding_and_revisions_preserve_it(lab):
    s,scope=lab
    row,item,content=add_image(s,scope)
    s.recognize_attachment(scope,item["id"],"测试红伞放在南门","test-recognition",row["revision"])
    assert [hit["id"] for hit in s.search_messages(scope,"红伞")] == [row["id"]]
    s.correct_memory(scope,row["id"],"附加说明：这是合成资料")
    assert [hit["id"] for hit in s.search_messages(scope,"红伞")] == [row["id"]]
    version=s.read_messages(scope,[row["id"]])[0]["revision"]
    s.recognize_attachment(scope,item["id"],"识别修订：蓝色箱子在西门","user-correction",version)
    assert not s.search_messages(scope,"红伞")
    assert s.search_messages(scope,"蓝色箱子")
    assert s.read_attachment(scope,item["id"])[0].read_bytes()==content


def test_existing_recognition_search_index_is_repaired_on_restart(lab):
    from memory_core.service import MemoryService
    from memory_core.storage import terms
    s,scope=lab
    row,item,_=add_image(s,scope)
    s.recognize_attachment(scope,item["id"],"以前识别的独特词：红伞","recognition",row["revision"])
    # Recreate the old index format, without changing the authoritative record.
    s.db.execute("UPDATE ltm_messages_fts SET terms=? WHERE rowid=?",(terms(row["body"]),row["pk"]))
    s.db.execute("DELETE FROM ltm_meta WHERE key='search_media_v2'")
    s.db.execute("INSERT OR REPLACE INTO ltm_meta VALUES('search_media_v1','1')")
    s.close()
    repaired=MemoryService(s.layout.project,s.layout.data,s.persona_id)
    try:
        assert repaired.search_messages(scope,"红伞")[0]["id"]==row["id"]
    finally:
        # The owning fixture must still have valid handles for cleanup.
        s.db,s.ledger=repaired.db,repaired.ledger


def test_legacy_snapshot_recognition_index_is_repaired_on_restore(lab):
    from memory_core.storage import terms
    s,scope=lab
    row,item,content=add_image(s,scope)
    s.recognize_attachment(scope,item["id"],"历史识别：红伞在东门","recognition",row["revision"])
    s.db.execute("UPDATE ltm_messages_fts SET terms=? WHERE rowid=?",(terms(row["body"]),row["pk"]))
    s.db.execute("DELETE FROM ltm_meta WHERE key='search_media_v2'")
    snapshot=s.snapshot(scope)
    assert not s.search_messages(scope,"红伞")
    s.ensure_search_index()
    assert s.search_messages(scope,"红伞")
    s.restore_snapshot(scope,snapshot["id"])
    assert [r["id"] for r in s.search_messages(scope,"红伞")]==[row["id"]]
    assert s.read_attachment(scope,item["id"])[0].read_bytes()==content


def test_recognition_search_survives_recycle_and_snapshot_lifecycle_replay(lab):
    s,scope=lab
    row,item,content=add_image(s,scope)
    s.recognize_attachment(scope,item["id"],"附件独有检索词：薄荷9051","recognition",row["revision"])
    snapshot=s.snapshot(scope)
    deleted=s.delete_memory(scope,[row["id"]])
    assert not s.search_messages(scope,"薄荷9051")
    s.restore_memory(scope,deleted["id"])
    assert [r["id"] for r in s.search_messages(scope,"薄荷9051")]==[row["id"]]
    s.restore_snapshot(scope,snapshot["id"])
    assert [r["id"] for r in s.search_messages(scope,"薄荷9051")]==[row["id"]]
    assert s.read_attachment(scope,item["id"])[0].read_bytes()==content


def test_media_reference_survives_relocation_of_managed_files(lab):
    from memory_core.service import MemoryService
    s,scope=lab
    row,item,content=add_image(s,scope)
    s.recognize_attachment(scope,item["id"],"搬迁验证词：薄荷9051","recognition",row["revision"])
    project=s.layout.project/".local/verification"/uuid.uuid4().hex/"relocated-project"
    data=project/".local/verification/data"
    persona=data/"personas"/s.persona_id
    # Explicit managed-file copies only; never traverse the source persona.
    for relative in ("manifest.json","editor.json"):
        target=persona/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(s.layout.persona_path(relative),target)
    for source,target in ((s.db,persona/"data/memory/metadata.db"),(s.ledger,data/"lifecycle"/(s.persona_id+".db"))):
        target.parent.mkdir(parents=True,exist_ok=True)
        connection=sqlite3.connect(target)
        try:
            source.backup(connection)
        finally:
            connection.close()
    source=s.read_attachment(scope,item["id"])[0]
    target=persona/source.relative_to(s.layout.persona)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)
    moved=MemoryService(project,data,s.persona_id)
    try:
        assert moved.read_attachment(scope,item["id"])[0].read_bytes()==content
        assert moved.search_messages(scope,"薄荷9051")[0]["id"]==row["id"]
    finally:
        moved.close()
