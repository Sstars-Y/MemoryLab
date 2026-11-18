"""100 fixed synthetic paraphrase cases; real BGE-M3 only, never hash embeddings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from memory_core.retrieval import LocalEmbedding,VectorIndex,hybrid_search
from memory_core.service import MemoryService
from memory_core.types import Scope
from benchmark_memory_lab import peak_memory,percentile

CASES=[
 ("一起修好了自行车的刹车", "上次处理脚踏车制动失灵"),
 ("雨天撑伞去公园散步", "下雨时在户外步行的经历"),
 ("烤了巧克力生日蛋糕", "庆生那次制作的甜点"),
 ("把凌乱的书架按主题整理好", "给阅读区分类收纳"),
 ("将电脑里误删的照片从回收站找回", "恢复遗失的相片"),
 ("第一次一起在山顶看日出", "登高迎接清晨太阳"),
 ("为流浪的小猫搭建了纸箱窝", "帮助无家可归的小动物安顿"),
 ("练习吉他的和弦转换", "弹奏六弦乐器时切换指法"),
 ("把忘记浇水的绿萝救活了", "恢复缺水室内植物的生机"),
 ("在车站等到了晚点的列车", "铁路交通延误后的等候"),
 ("更换水龙头的密封圈解决漏水", "维修厨房滴水问题"),
 ("一起包饺子并调制蘸料", "制作带馅面食的那一回"),
 ("在海边捡贝壳留作纪念", "沿着沙滩收集海洋小物件"),
 ("用望远镜观察月球的环形山", "通过天文器材看卫星表面"),
 ("熬夜排查代码中的死循环", "程序无法结束时的调试"),
 ("制作了一份每月家庭预算表", "规划日常收支的财务表格"),
 ("给旧木椅重新打磨并刷漆", "翻新老家具的过程"),
 ("完成了十公里慢跑训练", "长距离有氧运动的记录"),
 ("在图书馆归还了借阅的小说", "把文学读物送回公共阅读机构"),
 ("用针线缝好了外套松掉的纽扣", "修补衣服扣件"),
]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--run-id",default=time.strftime("semantic-%Y%m%d-%H%M%S"))
    args=parser.parse_args()
    root=PROJECT/".local/benchmarks"/args.run_id
    root.mkdir(parents=True,exist_ok=True)
    model=PROJECT/".local/caches/models/bge-m3"
    report={"provider":"real BGE-M3", "cases":100,"status":"not_run"}
    try:
        locked=json.loads((model/"model-lock.json").read_text())
        embedding=LocalEmbedding(PROJECT,model,locked["revision"])
        tick=time.perf_counter()
        embedding.encode(["加载测试"])
        report["model_load_seconds"]=time.perf_counter()-tick
        s=MemoryService(PROJECT,root/"data","persona_semantic")
        session=s.new_session(Scope(s.persona_id,"synthetic"))
        scope=Scope(s.persona_id,"synthetic",session)
        index=VectorIndex(s.layout.persona_path("data","indexes","vectors"),embedding)
        s.vector_index=index
        cases=[]
        for name in ["林一","陈二","周三","吴四","郑五"]:
            for original,paraphrase in CASES:
                row=s.record_message(scope,f"{name}说：上次我们{original}。这是共同交流里记录过的经历。",f"case_{len(cases)}")
                chunk="chunk_case_"+str(len(cases))
                s.db.execute("INSERT INTO ltm_chunks VALUES(?,?,?,?,?,?,?,?)",(chunk,scope.user_id,session,json.dumps([row["id"]]),json.dumps({row["id"]:1}),row["body"],index.generation,"active"))
                cases.append({"id":row["id"],"chunk":chunk,"text":row["body"],"query":f"{name}{paraphrase}是怎样的？"})
        tick=time.perf_counter()
        for offset in range(0,len(cases),16):
            group=cases[offset:offset+16]
            index.add([(c["chunk"],c["text"]) for c in group])
        report["index_seconds"]=time.perf_counter()-tick
        results,latency=[],[]
        for i,case in enumerate(cases):
            tick=time.perf_counter()
            found=hybrid_search(s,scope,case["query"],10)["messages"]
            elapsed=(time.perf_counter()-tick)*1000
            latency.append(elapsed)
            ids=[row["id"] for row in found]
            results.append({"query":case["query"],"expected":case["id"],"hit":case["id"] in ids,"rank":ids.index(case["id"])+1 if case["id"] in ids else None,"milliseconds":elapsed})
            if (i+1)%10==0:
                print(f"evaluated {i+1}/100",flush=True)
        recall=sum(r["hit"] for r in results)/len(results)
        report.update(status="complete",recall_at_10=recall,acceptance=recall>=.9,hybrid_p95_ms=percentile(latency),peak_working_set_bytes=peak_memory(),results=results,revision=locked["revision"],corpus_messages=len(cases))
        s.close()
    except Exception as error:
        report.update(status="blocked",reason=type(error).__name__+": "+str(error))
    (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="results"},ensure_ascii=False),flush=True)
    print(root/"report.json",flush=True)
    return 0 if report.get("acceptance") else 1


if __name__=="__main__":
    raise SystemExit(main())
