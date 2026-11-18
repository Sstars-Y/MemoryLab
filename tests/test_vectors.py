import json
import threading

import numpy as np
import pytest

from memory_core.retrieval import HashEmbedding, VectorIndex, hybrid_search
from memory_core.types import Scope


def test_incremental_shards_compact_and_generation_isolation(lab):
    s,scope=lab
    provider=HashEmbedding()
    index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),provider)
    for i in range(20):
        index.add([(f"chunk_{i}",f"修理自行车 {i}")])
    assert len(index.search("自行车",20))==20
    index.compact({f"chunk_{i}" for i in range(10)})
    assert {hit[0] for hit in index.search("自行车",20)}=={f"chunk_{i}" for i in range(10)}
    class Changed(HashEmbedding):
        name="synthetic-hash-v2"
    changed=VectorIndex(index.root,Changed())
    assert changed.generation != index.generation
    assert not changed.search("自行车")


def test_vector_candidates_require_current_source_versions_and_scope(lab):
    s,scope=lab
    index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),HashEmbedding())
    s.vector_index=index
    own=s.record_message(scope,"完成了自行车维修","own")
    other_session=s.new_session(Scope(s.persona_id,"other"))
    other=Scope(s.persona_id,"other",other_session)
    private=s.record_message(other,"自行车私密记录","private")
    for row,user,session,cid in [(own,scope.user_id,scope.session_id,"own"),(private,"other",other_session,"private")]:
        s.db.execute("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",(cid,user,session,json.dumps([row["id"]]),json.dumps({row["id"]:1}),row["body"],index.generation,"active"))
        index.add([(cid,row["body"])])
    assert {r["id"] for r in hybrid_search(s,scope,"自行车")["messages"]}=={own["id"]}
    s.correct_memory(scope,own["id"],"原先描述有误，实际修的是汽车")
    # Force a stale chunk active to verify filtering itself, beyond invalidation flags.
    s.db.execute("UPDATE ltm_chunks SET status='active' WHERE id='own'")
    hits=hybrid_search(s,scope,"zzzneveroccur")
    assert not hits["messages"]


def test_embedding_failure_keeps_lexical_retrieval(lab):
    s,scope=lab
    row=s.record_message(scope,"小王一起看日出","sunrise")
    class Broken(HashEmbedding):
        def encode(self,texts):
            raise RuntimeError("synthetic embedding outage")
    s.vector_index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),Broken())
    result=hybrid_search(s,scope,"小王")
    assert result["messages"][0]["id"]==row["id"]
    assert result["degraded"]


def test_compaction_bounds_shards_and_preserves_search_results(lab):
    s,scope=lab
    index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),HashEmbedding())
    index.shard_rows=16
    ids={f"chunk_{i}" for i in range(80)}
    for start in range(0,80,4):
        index.add([(f"chunk_{i}",f"自行车 {i}") for i in range(start,start+4)])
    for _ in range(20):
        index.compact(ids)
    sizes=[len(json.loads(p.read_text())["ids"]) for p in index.directory.glob("*.json")]
    assert max(sizes)<=index.shard_rows
    assert len(sizes)<16
    assert {hit[0] for hit in index.search("自行车",100)}==ids


@pytest.mark.parametrize("action", ["delete", "replace_index"])
def test_parallel_lookup_rechecks_sources_after_late_vector_result(lab, monkeypatch, action):
    s,scope=lab
    row=s.record_message(scope,"自行车修好了","source")
    started,release=threading.Event(),threading.Event()

    class DelayedIndex:
        generation="synthetic-delayed"

        def search(self,query,limit=100):
            started.set()
            assert release.wait(5), "lexical query did not run during vector lookup"
            return [("chunk_delayed",.9)]

        def clear_memory(self):
            pass

    s.vector_index=DelayedIndex()
    s.db.execute("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",
                 ("chunk_delayed",scope.user_id,scope.session_id,json.dumps([row["id"]]),
                  json.dumps({row["id"]:1}),row["body"],s.vector_index.generation,"active"))
    original=s.search_messages

    def during_lookup(*args,**kwargs):
        if kwargs.get("relaxed"):
            assert started.wait(5), "vector lookup did not start alongside lexical query"
            try:
                if action=="delete":
                    s.delete_memory(scope,[row["id"]])
                else:
                    s.vector_index=None
            finally:
                release.set()
        return original(*args,**kwargs)

    monkeypatch.setattr(s,"search_messages",during_lookup)
    result=hybrid_search(s,scope,"unmatched_synthetic_query")
    assert not result["messages"]
    if action=="replace_index":
        assert result["degraded"]
