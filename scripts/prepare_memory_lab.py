"""Explicit, reproducible setup. Run from a Python or the supplied .cmd launcher."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]


def ranged_weight(endpoint, revision, filename, size, destination):
    """Bounded resumable HTTP ranges for connections that reset large transfers."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import httpx
    import time
    part_root=destination/".cache/range-download"
    part_root.mkdir(parents=True,exist_ok=True)
    width=8*1024*1024
    intervals=[(start,min(start+width,size)-1) for start in range(0,size,width)]
    def fetch(interval):
        start,end=interval
        target=part_root/f"{start:012d}.part"
        if target.exists() and target.stat().st_size==end-start+1:
            return target
        for attempt in range(4):
            try:
                url=f"{endpoint}/BAAI/bge-m3/resolve/{revision}/{filename}?download=true&range_start={start}"
                with httpx.Client(timeout=90,follow_redirects=True) as client:
                    with client.stream("GET",url,headers={"Range":f"bytes={start}-{end}"}) as response:
                        if response.status_code!=206 or response.headers.get("content-range")!=f"bytes {start}-{end}/{size}":
                            raise RuntimeError(f"Range request rejected: HTTP {response.status_code}")
                        with target.open("wb") as stream:
                            count=0
                            for block in response.iter_bytes(1024*1024):
                                count+=len(block)
                                if count>end-start+1:
                                    raise RuntimeError("Range exceeded expected size")
                                stream.write(block)
                        if count!=end-start+1:
                            raise RuntimeError("Incomplete range")
                return target
            except Exception:
                target.unlink(missing_ok=True)
                if attempt==3:
                    raise
                time.sleep(min(4,attempt+1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(fetch,interval) for interval in intervals]
        for count,future in enumerate(as_completed(futures),1):
            future.result()
            if count%16==0 or count==len(intervals):
                print(f"Model ranges ready: {count}/{len(intervals)}",flush=True)
    output=destination/(filename+".assembling")
    with output.open("wb") as stream:
        for start,end in intervals:
            with (part_root/f"{start:012d}.part").open("rb") as part:
                shutil.copyfileobj(part,stream,1024*1024)
    output.replace(destination/filename)
    for start,end in intervals:
        (part_root/f"{start:012d}.part").unlink()
    part_root.rmdir()


def configure_environment():
    locations = {"UV_CACHE_DIR": PROJECT/".local/caches/uv", "UV_PYTHON_INSTALL_DIR": PROJECT/".local/runtime/python",
                 "UV_PROJECT_ENVIRONMENT": PROJECT/".local/runtime/venv", "HF_HOME": PROJECT/".local/caches/huggingface",
                 "TORCH_HOME": PROJECT/".local/caches/torch", "TEMP": PROJECT/".local/tmp/setup", "TMP": PROJECT/".local/tmp/setup"}
    for key, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedding", action="store_true", help="Download public BGE-M3 weights into this project")
    parser.add_argument("--skip-sync", action="store_true", help="Use the already prepared project environment")
    parser.add_argument("--revision", help="Public BGE-M3 commit; otherwise resolve once and record it")
    parser.add_argument("--endpoint", default="https://huggingface.co", choices=["https://huggingface.co", "https://hf-mirror.com"], help="Explicit public model download source")
    parser.add_argument("--ranged-download",action="store_true",help="Resume large weight downloads in bounded HTTP ranges")
    args = parser.parse_args()
    configure_environment()
    uv = shutil.which("uv")
    if not args.skip_sync:
        if not uv:
            parser.exit(2, "需要 uv；请安装 uv 后重试，脚本不会更改系统 Python。\n")
        subprocess.run([uv,"python","install","3.12.15","--no-bin","--no-registry"], cwd=PROJECT, check=True)
        interpreters = list((PROJECT/".local/runtime/python").glob("cpython-3.12.15-*/python.exe"))
        if not interpreters:
            parser.exit(2, "未找到项目内 Python 3.12.15。\n")
        command = [uv,"sync","--locked","--python",str(interpreters[0]),"--group","dev"]
        if args.embedding:
            command += ["--extra","embedding"]
        subprocess.run(command, cwd=PROJECT, check=True)
        if args.embedding:
            subprocess.run([str(PROJECT/".local/runtime/venv/Scripts/python.exe"),str(Path(__file__)),"--embedding","--skip-sync","--endpoint",args.endpoint, *(["--revision", args.revision] if args.revision else [])], cwd=PROJECT, check=True)
            return
    if args.embedding:
        os.environ["HF_ENDPOINT"] = args.endpoint
        from huggingface_hub import HfApi, snapshot_download
        destination = PROJECT/".local/caches/models/bge-m3"
        destination.mkdir(parents=True, exist_ok=True)
        lock_path = destination/"model-lock.json"
        locked = json.loads(lock_path.read_text()) if lock_path.exists() else {}
        if lock_path.exists() and not (destination/"pytorch_model.bin").exists() and not (destination/"model.safetensors").exists():
            lock_path.unlink()
        revision = args.revision or locked.get("revision")
        info = HfApi(endpoint=args.endpoint, token=False).model_info("BAAI/bge-m3", revision=revision, files_metadata=True)
        revision = info.sha
        patterns = ["config.json","config_sentence_transformers.json","modules.json","sentence_bert_config.json",
                    "sentencepiece.bpe.model","special_tokens_map.json","tokenizer.json","tokenizer_config.json",
                    "model.safetensors","pytorch_model.bin","1_Pooling/config.json"]
        files = [{"path": f.rfilename, "size": f.size, "sha256": f.lfs.sha256 if f.lfs else None} for f in info.siblings if f.rfilename in patterns]
        if not any(f["path"] in ("model.safetensors", "pytorch_model.bin") for f in files):
            raise RuntimeError("The selected revision contains no supported model weights")
        size = sum(f["size"] or 0 for f in files)
        print(f"Public model: BAAI/bge-m3 @ {revision}\nFiles: {len(files)}; download: {size/1024**3:.2f} GiB; allow 10 GiB free for model and dependencies.", flush=True)
        small_patterns=patterns
        if args.ranged_download:
            small_patterns=[f["path"] for f in files if (f["size"] or 0)<64*1024*1024]
        snapshot_download("BAAI/bge-m3", revision=revision, local_dir=destination, allow_patterns=small_patterns, token=False, max_workers=4, endpoint=args.endpoint)
        if args.ranged_download:
            for item in files:
                if (item["size"] or 0)>=64*1024*1024 and not (destination/item["path"]).exists():
                    ranged_weight(args.endpoint,revision,item["path"],item["size"],destination)
        import hashlib
        for item in files:
            if item["sha256"]:
                digest = hashlib.sha256()
                with (destination/item["path"]).open("rb") as stream:
                    for block in iter(lambda: stream.read(1024*1024), b""):
                        digest.update(block)
                if digest.hexdigest() != item["sha256"]:
                    raise RuntimeError("Model file checksum mismatch: "+item["path"])
        result = {"repository":"BAAI/bge-m3", "revision":revision, "dimension":1024, "max_sequence_length":512,
                  "device":"cpu", "download_endpoint":args.endpoint, "files":files, "download_bytes":size}
        lock_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print("Model prepared. Runtime loads local files only.", flush=True)
    else:
        print("MemoryLab runtime prepared. Double-click 启动实验版.cmd.")


if __name__ == "__main__":
    main()
