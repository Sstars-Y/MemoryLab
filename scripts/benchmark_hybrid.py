"""Real BGE vectors at scale, using an explicitly repetitive capacity corpus.

Each repeated utterance remains a separate message and vector-source relation.
Identical input encoding is cached only during fixture preparation. This measures
storage/query load, not diverse semantic quality (evaluate_semantic.py does that).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import numpy as np

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from memory_core.retrieval import LocalEmbedding,VectorIndex,hybrid_search
from memory_core.recall import recall_for_chat
from memory_core.service import MemoryService
from memory_core.storage import terms
from memory_core.types import Scope,identifier
from benchmark_memory_lab import peak_memory,percentile
from acceptance_evidence import collect_evidence


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--sizes",type=int,nargs="+",default=[10000,100000,1000000])
    parser.add_argument("--run-id",default=time.strftime("hybrid-%Y%m%d-%H%M%S"))
    parser.add_argument("--queries",type=int,default=32)
    parser.add_argument("--reuse-run-id",help="Recheck a single existing synthetic scale without rebuilding its fixtures")
    parser.add_argument("--chat",action="store_true",help="Measure the complete chat recall path, including reranking and follow-ups")
    args=parser.parse_args()
    identifier(args.run_id)
    if args.reuse_run_id:
        identifier(args.reuse_run_id)
    root=PROJECT/".local/benchmarks"/args.run_id
    root.mkdir(parents=True,exist_ok=True)
    model=PROJECT/".local/caches/models/bge-m3"
    lock=json.loads((model/"model-lock.json").read_text())
    embedding=LocalEmbedding(PROJECT,model,lock["revision"])
    started=time.perf_counter()
    names=["小王","小李","阿青","小陈","小林","阿明","小赵","阿秋"]
    events=["一起修好自行车","讨论周末做饭","核对旧项目编号","在雨天散步","帮忙整理书架","讨论生日安排","一起完成绘画","回忆第一次交流"]
    texts=[f"{name}说：上次我们{event}，这是一段在交流中记录的经历。" for name in names for event in events]
    vectors=embedding.encode(texts)
    encode_seconds=time.perf_counter()-started
    input_root=PROJECT/".local/benchmarks"/args.reuse_run_id if args.reuse_run_id else root
    s=MemoryService(PROJECT,input_root/"data","persona_capacity")
    if args.reuse_run_id and (len(args.sizes)!=1 or s.db.execute("SELECT COUNT(*) FROM ltm_messages").fetchone()[0]!=args.sizes[0]):
        s.close()
        parser.error("reuse requires one size exactly matching the existing synthetic corpus")
    session=s.new_session(Scope(s.persona_id,"synthetic"))
    scope=Scope(s.persona_id,"synthetic",session)
    index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),embedding)
    s.vector_index=index
    report={"model":embedding.name,"revision":lock["revision"],"corpus":"64 distinct synthetic texts, repeated as separate messages; encoding identical texts cached only in fixture construction",
            "quality_report":"semantic-real/report.json (100 separate paraphrase cases)","load_and_encode_seconds":encode_seconds,
            "platform":platform.platform(),"provenance":collect_evidence(input_root),"reused_fixture":args.reuse_run_id,"query_path":"chat-recall" if args.chat else "hybrid-search","scales":[],"remote_calls":0,"remote_cost":0}
    count=args.sizes[0] if args.reuse_run_id else 0
    for size in sorted(args.sizes):
        tick=time.perf_counter()
        for start in range(count,size,4096):
            stop=min(start+4096,size)
            messages,fts,chunks,ids,which=[],[],[],[],[]
            for i in range(start,stop):
                mid=f"msg_capacity_{i:08d}"
                cid=f"chunk_capacity_{i:08d}"
                text=texts[i%len(texts)]
                messages.append((i+1,mid,scope.user_id,session,i+1,"user" if i%2==0 else "assistant",text,1700000000+i,f"fixture_{i}",mid))
                fts.append((i+1,terms(text)))
                chunks.append((cid,scope.user_id,session,json.dumps([mid]),json.dumps({mid:1}),text,index.generation,"active"))
                ids.append(cid)
                which.append(i%len(texts))
            with s.transaction():
                s.db.executemany("INSERT INTO ltm_messages(pk,id,user_id,session_id,ordinal,role,body,created,idem,turn_id) VALUES(?,?,?,?,?,?,?,?,?,?)",messages)
                s.db.executemany("INSERT INTO ltm_messages_fts(rowid,terms) VALUES(?,?)",fts)
                s.db.executemany("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",chunks)
            index.write(ids,vectors[which])
            active={r[0] for r in s.db.execute("SELECT id FROM ltm_chunks WHERE status='active'")}
            index.compact(active)
            if start%40960==0:
                print(f"capacity indexed {stop:,}/{size:,}",flush=True)
        index_seconds=time.perf_counter()-tick
        count=size
        s.db.execute("INSERT INTO ltm_messages_fts(ltm_messages_fts) VALUES('optimize')")
        s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        latencies,correct=[],0
        for i in range(args.queries):
            # Odd event slots are assistant statements in this fixture. Chat
            # recall intentionally excludes them as user fact evidence.
            which=(i*(14 if args.chat else 7))%len(texts)
            name=names[which//8]
            event=events[which%8]
            tick=time.perf_counter()
            query=f"{name}上次{event}的交流"
            result=recall_for_chat(s,scope,{"id":"synthetic_current","body":query}) if args.chat else hybrid_search(s,scope,query,10)
            latencies.append((time.perf_counter()-tick)*1000)
            if any(row["body"]==texts[which] for row in result["messages"]):
                correct+=1
        result={"messages":size,"chunks":s.db.execute("SELECT COUNT(*) FROM ltm_chunks").fetchone()[0],"integrity":s.db.execute("PRAGMA quick_check").fetchone()[0],
                "incremental_fixture_index_seconds":index_seconds,"hybrid_p50_ms":statistics.median(latencies),"hybrid_p95_ms":percentile(latencies),"hybrid_max_ms":max(latencies),
                "cases_correct":correct,"cases_total":len(latencies),"peak_working_set_bytes":peak_memory(),"vector_bytes":sum(p.stat().st_size for p in index.directory.glob("*.npy")),
                "database_bytes":s.layout.database.stat().st_size,"vector_shards":len(list(index.directory.glob('*.json'))),"pending_jobs":s.db.execute("SELECT COUNT(*) FROM ltm_jobs WHERE status='pending'").fetchone()[0],"acceptance":percentile(latencies)<=3000 and correct==len(latencies) and peak_memory()<=8*1024**3}
        report["scales"].append(result)
        (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False),flush=True)
    s.close()
    print(root/"report.json",flush=True)
    raise SystemExit(0 if all(row["acceptance"] for row in report["scales"]) else 1)


if __name__=="__main__":
    main()
