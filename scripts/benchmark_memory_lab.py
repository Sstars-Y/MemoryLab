"""Synthetic scale measurement. Semantic quality and remote time are separate reports."""
from __future__ import annotations

import argparse
from pathlib import Path
import ctypes
import json
import os
import platform
import random
import sqlite3
import statistics
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from memory_core.service import MemoryService
from memory_core.storage import terms
from memory_core.types import Scope,identifier
from acceptance_evidence import collect_evidence


def peak_memory():
    if os.name != "nt":
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD), *[(name,ctypes.c_size_t) for name in ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage")]]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    current = ctypes.windll.kernel32.GetCurrentProcess
    current.restype = wintypes.HANDLE
    information = ctypes.windll.psapi.GetProcessMemoryInfo
    information.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    information.restype = wintypes.BOOL
    if not information(current(),ctypes.byref(counters),counters.cb):
        raise ctypes.WinError()
    return counters.PeakWorkingSetSize


def percentile(values, q=.95):
    return sorted(values)[min(len(values)-1, int(len(values)*q))]


def seed(s, scope, start, stop):
    names = ["小王","小李","阿青","小陈","小林","阿明","小赵","阿秋"]
    events = ["一起修好自行车","讨论周末做饭","核对旧项目编号","在雨天散步","帮忙整理书架","讨论生日安排","一起完成绘画","回忆第一次交流"]
    for batch in range(start, stop, 1000):
        messages, indexes, jobs = [], [], []
        for i in range(batch, min(batch+1000, stop)):
            text = f"{names[i%8]}记录实验事件{i:08d}：{events[(i//8)%8]}。这次对话的检索编号为 LAB{i:08d}。"
            message_id = f"msg_bench_{i:08d}"
            messages.append((i+1,message_id,scope.user_id,scope.session_id,i+1,"user" if i%2==0 else "assistant",text,1700000000+i,f"bench_{i}",message_id))
            indexes.append((i+1,terms(text)))
            jobs.append((f"job_bench_{i}",message_id,1,s.run_id,0,"pending"))
        with s.transaction():
            s.db.executemany("INSERT INTO ltm_messages(pk,id,user_id,session_id,ordinal,role,body,created,idem,turn_id) VALUES(?,?,?,?,?,?,?,?,?,?)", messages)
            s.db.executemany("INSERT INTO ltm_messages_fts(rowid,terms) VALUES(?,?)", indexes)
            s.db.executemany("INSERT INTO ltm_jobs(id,message_id,revision,run_id,seq,status) VALUES(?,?,?,?,?,?)", jobs)
        if (batch+1000)%50000 == 0:
            print(f"seeded {batch+1000:,}",flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[10000,100000,1000000])
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--queries", type=int, default=120)
    parser.add_argument("--reuse-run-id", help="Recheck one existing synthetic scale without reseeding")
    args = parser.parse_args()
    identifier(args.run_id)
    if args.reuse_run_id:
        identifier(args.reuse_run_id)
    root = PROJECT/".local/benchmarks"/args.run_id
    root.mkdir(parents=True,exist_ok=True)
    input_root=PROJECT/".local/benchmarks"/args.reuse_run_id if args.reuse_run_id else root
    s = MemoryService(PROJECT,input_root/"data","persona_benchmark")
    if args.reuse_run_id and (len(args.sizes)!=1 or s.db.execute("SELECT COUNT(*) FROM ltm_messages").fetchone()[0]!=args.sizes[0]):
        s.close()
        parser.error("reuse requires one size exactly matching the existing synthetic corpus")
    session = s.new_session(Scope(s.persona_id,"synthetic"))
    scope = Scope(s.persona_id,"synthetic",session)
    report = {"machine":{"platform":platform.platform(),"cpu":platform.processor(),"logical_cpus":os.cpu_count()},
              "sqlite":sqlite3.sqlite_version,"provenance":collect_evidence(input_root),"reused_fixture":args.reuse_run_id,"corpus":"fixed synthetic message text; no personal data", "scales":[],
              "semantic":"not measured by this script; run evaluate_semantic.py", "remote":{"calls":0,"cost":0,"reason":"no remote credentials configured"}}
    count = args.sizes[0] if args.reuse_run_id else 0
    rng = random.Random(42)
    for size in sorted(args.sizes):
        started = time.perf_counter()
        seed(s,scope,count,size)
        seeding = time.perf_counter()-started
        count = size
        s.db.execute("INSERT INTO ltm_messages_fts(ltm_messages_fts) VALUES('optimize')")
        s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        queries, correct = [], 0
        for index in range(args.queries):
            target = rng.randrange(size)
            q = f"LAB{target:08d}" if index%3 else "小王"
            tick = time.perf_counter()
            hits = s.search_messages(scope,q,limit=30)
            queries.append((time.perf_counter()-tick)*1000)
            if (index%3 and any(row["id"] == f"msg_bench_{target:08d}" for row in hits)) or (not index%3 and hits and all("小王" in row["body"] for row in hits)):
                correct += 1
        actual = s.db.execute("SELECT COUNT(*) FROM ltm_messages").fetchone()[0]
        indexed = s.db.execute("SELECT COUNT(*) FROM ltm_messages_fts").fetchone()[0]
        result = {"messages":size,"actual_messages":actual,"indexed_messages":indexed,"integrity":s.db.execute("PRAGMA quick_check").fetchone()[0],
                  "incremental_seed_seconds":seeding,"fts_p50_ms":statistics.median(queries),"fts_p95_ms":percentile(queries),"fts_max_ms":max(queries),
                  "exact_cases_correct":correct,"exact_cases_total":len(queries),"peak_working_set_bytes":peak_memory(),
                  "database_bytes":s.layout.database.stat().st_size,"pending_jobs":s.db.execute("SELECT COUNT(*) FROM ltm_jobs WHERE status='pending'").fetchone()[0],
                  "vector_index":"not built in lexical-scale benchmark","hybrid_p95_ms":None,"acceptance_fts":percentile(queries)<=500 and correct==len(queries) and actual==size==indexed}
        report["scales"].append(result)
        (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False),flush=True)
    s.close()
    print(f"Report: {root/'report.json'}",flush=True)
    raise SystemExit(0 if all(row["acceptance_fts"] for row in report["scales"]) else 1)


if __name__ == "__main__":
    main()
