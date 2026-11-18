"""Fixed positive and distractor cases for the actual chat recall path."""
from pathlib import Path
import json
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from evaluate_semantic import CASES
from memory_core.recall import recall_for_chat
from memory_core.retrieval import LocalEmbedding,VectorIndex
from memory_core.service import MemoryService
from memory_core.types import Scope,uid
from acceptance_evidence import collect_evidence


def main():
    root=PROJECT/".local/verification/acceptance-20261114"/("focused-"+time.strftime("%Y%m%d-%H%M%S"))
    root.mkdir(parents=True)
    model=PROJECT/".local/caches/models/bge-m3"
    locked=json.loads((model/"model-lock.json").read_text(encoding="utf-8"))
    s=MemoryService(PROJECT,root/"data","persona_focused")
    s.vector_index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),LocalEmbedding(PROJECT,model,locked["revision"]))
    scope=Scope(s.persona_id,"synthetic")
    cases=[]
    for name in ["林一","陈二","周三","吴四","郑五"]:
        for original,query in CASES:
            session=s.new_session(scope)
            bound=Scope(s.persona_id,scope.user_id,session)
            row=s.record_message(bound,f"{name}说：上次我们{original}。",uid("idem"))
            cid=uid("chunk")
            s.db.execute("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",(cid,scope.user_id,session,json.dumps([row["id"]]),json.dumps({row["id"]:row["revision"]}),row["body"],s.vector_index.generation,"active"))
            cases.append({"row":row,"chunk":cid,"query":f"{name}{query}是怎样的？"})
    s.vector_index.add([(c["chunk"],c["row"]["body"]) for c in cases])
    query_scope=Scope(s.persona_id,scope.user_id,s.new_session(scope))
    results=[]
    for i,case in enumerate(cases):
        started=time.perf_counter()
        found=recall_for_chat(s,query_scope,{"id":"synthetic_current","body":case["query"]})["messages"]
        ids=[row["id"] for row in found]
        results.append({"case":i,"query":case["query"],"expected":case["row"]["id"],"selected":ids,
                        "hit":case["row"]["id"] in ids,"milliseconds":(time.perf_counter()-started)*1000})
        if (i+1)%20==0:
            print(f"focused recall evaluated {i+1}/100",flush=True)
    hits=sum(row["hit"] for row in results)
    selected=sum(len(row["selected"]) for row in results)
    report={"provenance":collect_evidence(root),"cases":100,"recall":hits/100,"source_precision":hits/selected if selected else 0,
            "acceptance":hits>=90 and (hits/selected if selected else 0)>=.8,
            "note":"Each query has one relevant source; same-name other-topic records are distractors.","results":results}
    (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("results","provenance")},ensure_ascii=False),flush=True)
    print(str(root/"report.json"),flush=True)
    s.close()


if __name__=="__main__":
    main()
