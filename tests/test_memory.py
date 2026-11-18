import builtins
from io import BytesIO
import json
from pathlib import Path
import sqlite3

from PIL import Image
import pytest

from memory_core.service import MemoryService
from memory_core.types import Scope, MemoryFault
from memory_lab.media import Uploads


def record(service, scope, body="小王住在上海", key="first", **kwargs):
    return service.record_message(scope, body, key, **kwargs)


def test_repeat_and_retry(lab):
    s, scope = lab
    first = record(s, scope)
    assert record(s, scope)["id"] == first["id"]
    second = record(s, scope, key="second")
    assert first["id"] != second["id"]
    with pytest.raises(MemoryFault):
        record(s, scope, "不同内容")
    assert len(s.history(scope)) == 2


def test_fts_short_names_phrase_id_filters(lab):
    s, scope = lab
    first = record(s, scope)
    record(s, scope, "编号 ABC-123，不喜欢北京", "other")
    for q in ("王", "小王", "住在上海", first["id"], "ABC-123"):
        assert s.search_messages(scope, q)
    assert not s.search_messages(scope, "ABC-999")
    assert not s.search_messages(scope, "小王", {"since": s.clock()+10})
    assert not s.search_messages(scope, "小王", {"role": "assistant"})
    assert not s.search_messages(scope, '" OR 1=1 --')


def test_scope_and_context_expansion(lab):
    s, scope = lab
    first = record(s, scope)
    for wrong in (Scope("another", scope.user_id), Scope(s.persona_id, "other_user")):
        with pytest.raises(MemoryFault):
            s.read_messages(wrong, [first["id"]], 3, 3)
    other_session = s.new_session(Scope(s.persona_id, "other_user"))
    private = Scope(s.persona_id, "other_user", other_session)
    record(s, private, "小王其他用户私聊")
    assert len(s.search_messages(scope, "小王")) == 1


def test_correction_and_historical_change(lab):
    s, scope = lab
    first = record(s, scope, "我住北京")
    s.correct_memory(scope, first["id"], "那天说错了，我一直住上海")
    s.correct_memory(scope, first["id"], "2027年搬到杭州", "change", "2027-01-01")
    assert s.search_messages(scope, "上海")[0]["id"] == first["id"]
    current = s.read_messages(scope, [first["id"]])[0]
    assert current["body"] == "我住北京"
    assert [r["kind"] for r in current["corrections"]] == ["error", "change"]


def test_whole_turn_default_and_selected_only(lab):
    s, scope = lab
    first = record(s, scope)
    answer1 = record(s, scope, "好的", "answer1", role="assistant", turn_id=first["turn_id"])
    answer2 = record(s, scope, "又一个完成回答", "answer2", role="assistant", turn_id=first["turn_id"])
    independent = record(s, scope, "另一次也提到了小王", "independent")
    assert len(s.delete_preview(scope, first["id"])) == 3
    deleted = s.delete_memory(scope, [first["id"], answer1["id"], answer2["id"]])
    assert [m["id"] for m in s.history(scope)] == [independent["id"]]
    with pytest.raises(MemoryFault):
        s.read_messages(scope, [first["id"]])
    s.restore_memory(scope, deleted["id"])
    assert {m["id"] for m in s.history(scope)} == {first["id"], answer1["id"], answer2["id"], independent["id"]}


def test_expiry_and_purge_removes_bytes(lab):
    s, scope = lab
    secret_phrase = "unique_physical_deletion_marker_78a2"
    row = record(s, scope, secret_phrase)
    recycle = s.delete_memory(scope, [row["id"]])
    s.clock = lambda: recycle["expires"]+1
    assert not s.search_messages(scope, secret_phrase)
    assert s.recycle_items(scope)[0]["status"] == "purged"
    with pytest.raises(MemoryFault):
        s.restore_memory(scope, recycle["id"])
    for path in [s.layout.database, Path(str(s.layout.database)+"-wal")]:
        if path.exists():
            assert secret_phrase.encode() not in path.read_bytes()


def test_replay_durable_intent_and_missing_ledger(lab):
    s, scope = lab
    row = record(s, scope)
    s.ledger.execute("INSERT INTO operations(kind,target_ids,recycle_id,user_id,created,expires) VALUES(?,?,?,?,?,?)", ("delete", json.dumps([row["id"]]), "recycle_crash", scope.user_id, s.clock(), s.clock()+604800))
    assert not s.search_messages(scope, "小王")
    assert s.ledger.execute("SELECT applied FROM operations").fetchone()[0] == 1
    # Opening existing DB without ledger must fail instead of manufacturing fresh history.
    other_root = s.layout.project / ".local" / "verification" / ("missing_"+s.run_id)
    other = MemoryService(s.layout.project, other_root, "p")
    other.close()
    (other_root / "lifecycle" / "p.db").unlink()
    with pytest.raises(MemoryFault, match="缺少最新"):
        MemoryService(s.layout.project, other_root, "p")


def test_media_ids_atomicity_and_recycle(lab):
    s, scope = lab
    uploads = Uploads(s.layout.project, s.run_id)
    image = BytesIO()
    Image.new("RGB", (4,4), "green").save(image, "PNG")
    upload = uploads.put(scope, image.getvalue(), "test.png")
    row = record(s, scope, "图片", attachments=uploads.take(scope, [upload["id"]]))
    attachment = row["attachments"][0]
    assert attachment["id"] == upload["id"]
    assert s.read_attachment(scope, upload["id"])[0].read_bytes() == image.getvalue()
    recycle = s.delete_memory(scope, [row["id"]])
    with pytest.raises(MemoryFault):
        s.read_attachment(scope, upload["id"])
    assert s.read_attachment(scope, upload["id"], recycle=True)[0].exists()
    s.restore_memory(scope, recycle["id"])
    assert s.read_attachment(scope, upload["id"])[0].exists()
    recycle2 = s.delete_memory(scope, [row["id"]])
    s.purge_memory(scope, recycle2["id"])
    with pytest.raises(MemoryFault):
        s.read_attachment(scope, upload["id"], recycle=True)
    with pytest.raises(MemoryFault):
        s.read_attachment(scope, "../../users/secrets")


def test_snapshot_media_and_live_lifecycle(lab):
    s, scope = lab
    row = record(s, scope)
    snapshot = s.snapshot(scope)
    assert s.verify_snapshot(scope, snapshot["id"])["persona_id"] == s.persona_id
    late = record(s, scope, "快照之后", "late")
    s.restore_snapshot(scope, snapshot["id"])
    assert s.read_messages(scope, [row["id"]])
    with pytest.raises(MemoryFault):
        s.read_messages(scope, [late["id"]])
    # An older snapshot remains a recovery point but cannot resurrect deletion.
    s.delete_memory(scope, [row["id"]])
    s.restore_snapshot(scope, snapshot["id"])
    assert not s.history(scope)
    recycle = s.recycle_items(scope)[0]
    s.purge_memory(scope, recycle["id"])
    with pytest.raises(MemoryFault):
        s.verify_snapshot(scope, snapshot["id"])


def test_corrupt_snapshot_refused(lab):
    s, scope = lab
    record(s, scope)
    snapshot = s.snapshot(scope)
    source = s.layout.persona_path("backups", snapshot["id"], "persona", "editor.json")
    source.write_text("tampered")
    with pytest.raises(MemoryFault, match="校验失败"):
        s.restore_snapshot(scope, snapshot["id"])
    assert len(s.history(scope)) == 1


def test_users_access_probe_and_managed_exports(lab, monkeypatch):
    s, scope = lab
    sentinel = s.layout.persona / "users" / "private.txt"
    sentinel.write_text("never-read", encoding="utf-8")
    original_open = builtins.open
    original_io_open = __import__("io").open
    original_iterdir = Path.iterdir
    def refuse(path):
        if "users" in Path(path).parts:
            raise AssertionError("users/ was opened or enumerated")
    def probe_open(path, *args, **kwargs):
        if not isinstance(path, int):
            refuse(path)
        return original_open(path, *args, **kwargs)
    def probe_io_open(path, *args, **kwargs):
        if not isinstance(path, int):
            refuse(path)
        return original_io_open(path, *args, **kwargs)
    def probe_iterdir(path):
        refuse(path)
        return original_iterdir(path)
    monkeypatch.setattr(builtins, "open", probe_open)
    monkeypatch.setattr(__import__("io"), "open", probe_io_open)
    monkeypatch.setattr(Path, "iterdir", probe_iterdir)
    row = record(s, scope)
    export = s.export(scope)
    snapshot = s.snapshot(scope)
    s.verify_snapshot(scope, snapshot["id"])
    s.search_messages(scope, "never-read")
    s.delete_memory(scope, [row["id"]])
    products = {p["kind"]:p for p in s.products(scope)}
    assert products["export"]["status"] == "removed"
    assert products["snapshot"]["status"] == "active"


def test_links_cannot_reach_users(lab):
    s, scope = lab
    target = s.layout.persona / "users" / "private.txt"
    target.write_text("secret")
    link = s.layout.persona_path("lore", "linked.md")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable; junction test is separate")
    with pytest.raises(MemoryFault):
        s.compile_persona()


def test_credentials_rejected_before_persistence(lab):
    s, scope = lab
    with pytest.raises(MemoryFault):
        record(s, scope, "key sk-abcdefghijklmnopqrstuvwx")
    assert s.history(scope) == []


def test_windows_junction_cannot_bypass_users(lab):
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows-only junction")
    s, scope = lab
    junction = s.layout.persona_path("lore", "alias.md")
    target = s.layout.persona / "users"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(MemoryFault, match="联接"):
            s.compile_persona()
    finally:
        assert junction.is_junction()
        os.rmdir(junction)


def test_cleanup_failure_reports_pending_and_retries(lab, monkeypatch):
    import memory_core.lifecycle as lifecycle
    s,scope=lab
    row=record(s,scope)
    export=s.export(scope)
    original=lifecycle.remove_managed_tree
    def locked_file(*args,**kwargs):
        raise PermissionError("synthetic locked export")
    monkeypatch.setattr(lifecycle,"remove_managed_tree",locked_file)
    deleted=s.delete_memory(scope,[row["id"]])
    result=s.purge_memory(scope,deleted["id"])
    assert result["status"]=="purge_pending"
    assert s.stats(scope)["cleanup_error"]
    assert not s.history(scope)
    monkeypatch.setattr(lifecycle,"remove_managed_tree",original)
    assert s.recycle_items(scope)[0]["status"]=="purged"
