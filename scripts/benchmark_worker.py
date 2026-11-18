from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from memory_core.service import MemoryService
from memory_core.types import Scope
from memory_core.retrieval import LocalEmbedding,VectorIndex
from memory_core.worker import ConsolidationWorker
from memory_lab.providers import FakeProvider
from benchmark_memory_lab import peak_memory
from acceptance_evidence import collect_evidence


async def main():
    root=PROJECT/".local/benchmarks"/time.strftime("worker-%Y%m%d-%H%M%S")
    provenance=collect_evidence(root)
    model=PROJECT/".local/caches/models/bge-m3"
    lock=json.loads((model/"model-lock.json").read_text())
    s=MemoryService(PROJECT,root/"data","persona_worker_benchmark")
    session=s.new_session(Scope(s.persona_id,"synthetic"))
    scope=Scope(s.persona_id,"synthetic",session)
    embedding=LocalEmbedding(PROJECT,model,lock["revision"])
    s.vector_index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),embedding)
    for i in range(120):
        s.record_message(scope,f"合成经历 {i}：今天讨论修理自行车、准备晚饭和整理书架的安排。",f"worker_{i}","user" if i%2==0 else "assistant")
    worker=ConsolidationWorker(s,lambda:FakeProvider(delay=0))
    pending_before=s.db.execute("SELECT COUNT(*) FROM ltm_jobs WHERE status='pending'").fetchone()[0]
    tick=time.perf_counter()
    calls=0
    while await worker.once():
        calls+=1
        if calls%10==0:
            print(f"worker batches: {calls}",flush=True)
    elapsed=time.perf_counter()-tick
    jobs={r[0]:r[1] for r in s.db.execute("SELECT status,COUNT(*) FROM ltm_jobs GROUP BY status")}
    covered={mid for row in s.db.execute("SELECT source_ids FROM ltm_chunks WHERE status='active'") for mid in json.loads(row[0])}
    report={"messages":120,"elapsed_seconds_including_model_load":elapsed,"batches":calls,"jobs":jobs,"peak_working_set_bytes":peak_memory(),
            "embedding":embedding.name,"summary_provider":"fake; not real-model summary quality", "remote_calls":0,
            "chunk_count":s.db.execute("SELECT COUNT(*) FROM ltm_chunks WHERE status='active'").fetchone()[0],
            "pending_before":pending_before,"covered_messages":len(covered),"integrity":s.db.execute("PRAGMA quick_check").fetchone()[0],
            "provenance":provenance}
    report["acceptance"]=jobs=={"done":120} and len(covered)==120 and report["integrity"]=="ok"
    (root/"report.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report),flush=True)
    print(root/"report.json",flush=True)
    s.close()
    raise SystemExit(0 if report["acceptance"] else 1)


if __name__=="__main__":
    asyncio.run(main())
