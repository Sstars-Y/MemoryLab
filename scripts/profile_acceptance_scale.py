"""Profile existing synthetic capacity fixtures, never the trial data root."""
from pathlib import Path
import argparse
import cProfile
import io
import json
import pstats
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from memory_core.retrieval import LocalEmbedding,VectorIndex,hybrid_search
from memory_core.recall import recall_for_chat
from memory_core.service import MemoryService
from memory_core.types import Scope,identifier
from threadpoolctl import threadpool_limits


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',action='store_true')
    parser.add_argument('--compact',action='store_true')
    parser.add_argument('--hybrid',action='store_true')
    parser.add_argument('--chat',action='store_true')
    parser.add_argument('--timings',action='store_true')
    parser.add_argument('--run-id',default='hybrid-capacity')
    args=parser.parse_args()
    identifier(args.run_id)
    model=PROJECT/".local/caches/models/bge-m3"
    locked=json.loads((model/"model-lock.json").read_text())
    s=MemoryService(PROJECT,PROJECT/".local/benchmarks"/args.run_id/"data","persona_capacity")
    index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),LocalEmbedding(PROJECT,model,locked["revision"]))
    index.embedding.encode(["小王一起修好自行车"])
    if args.timings:
        s.vector_index=index
        timings={}
        def measure(owner,name,label):
            original=getattr(owner,name)
            def measured(*values,**keywords):
                tick=time.perf_counter()
                try:
                    return original(*values,**keywords)
                finally:
                    key=label
                    if label=='fulltext_search':
                        key+='_'+('recent' if keywords.get('newest') else 'relaxed' if keywords.get('relaxed') else 'strict')
                    row=timings.setdefault(key,{"calls":0,"seconds":0})
                    row["calls"]+=1
                    row["seconds"]+=time.perf_counter()-tick
            setattr(owner,name,measured)
        measure(index.embedding,'encode','encoding')
        measure(index,'search','vector_search_including_encoding')
        measure(s,'search_messages','fulltext_search')
        measure(s,'read_messages','source_reads')
        for _ in range(3):
            timings.clear()
            tick=time.perf_counter()
            result=recall_for_chat(s,Scope(s.persona_id,'synthetic'),
                                   {"id":"synthetic_current","body":"小王上次一起修好自行车的交流"})
            print(json.dumps({"diagnostic_only":True,"total_seconds":time.perf_counter()-tick,
                              "returned_sources":len(result['messages']),"stages":timings}),flush=True)
        s.close()
        return
    if args.compact:
        active={r[0] for r in s.db.execute("SELECT id FROM ltm_chunks WHERE status='active'")}
        tick=time.perf_counter()
        for _ in range(256):
            before=len(list(index.directory.glob('*.json')))
            index.compact(active)
            after=len(list(index.directory.glob('*.json')))
            if after>=before:
                break
        print(json.dumps({'compaction_seconds':time.perf_counter()-tick,'shards':after}),flush=True)
    if args.profile:
        profiler=cProfile.Profile()
        if args.chat:
            s.vector_index=index
            profiler.runcall(recall_for_chat,s,Scope(s.persona_id,'synthetic'),
                             {"id":"synthetic_current","body":"小王上次一起修好自行车的交流"})
        elif args.hybrid:
            s.vector_index=index
            profiler.runcall(hybrid_search,s,Scope(s.persona_id,'synthetic'),"小王上次一起修好自行车的交流",10)
        else:
            profiler.runcall(index.search,"小王一起修好自行车",100)
        output=io.StringIO()
        pstats.Stats(profiler,stream=output).strip_dirs().sort_stats('cumtime').print_stats(22)
        print(output.getvalue(),flush=True)
        scope=Scope(s.persona_id,'synthetic')
        tick=time.perf_counter()
        s.search_messages(scope,'自行车',limit=20)
        print(json.dumps({'fts_seconds':time.perf_counter()-tick}),flush=True)
        s.close()
        return
    for threads in (24,1,4):
        with threadpool_limits(limits=threads,user_api="blas"):
            tick=time.perf_counter()
            index.search("小王一起修好自行车",100)
            print(json.dumps({"blas_threads":threads,"vector_seconds":time.perf_counter()-tick}),flush=True)
    s.close()


if __name__=="__main__":
    main()
