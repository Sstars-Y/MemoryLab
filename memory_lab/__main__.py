from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import socket
import threading
import time
import urllib.request
import webbrowser
import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
os.environ.setdefault("OMP_NUM_THREADS", "8")

import uvicorn

from memory_core.paths import guarded
from memory_core.lifecycle import remove_managed_tree
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="MemoryLab local-only web application")
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--persona", default="persona_default")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    if not 1024 <= args.port <= 65535:
        parser.error("端口须在 1024 到 65535 之间")
    for key in ("TMP", "TEMP", "TMPDIR", "XDG_CACHE_HOME", "HF_HOME", "TORCH_HOME"):
        target = guarded(project, ".local", "tmp" if key in ("TMP", "TEMP", "TMPDIR") else "caches", key.lower())
        target.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(target)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    # Bind before opening any DB. Unknown processes are never terminated.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        parser.exit(2, f"端口 {args.port} 已被占用；未操作任何已有进程。请使用 --port 指定其他端口。\n")
    # An OS-held lock prevents two different ports opening the same persona.
    lock_dir = guarded(project, ".local", "locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    import hashlib
    lock_name = hashlib.sha256((str(args.data_root.absolute())+args.persona).encode()).hexdigest()+".lock"
    lock_file = guarded(lock_dir, lock_name).open("a+b")
    try:
        # Do not read a byte already locked by another Windows process.
        if os.fstat(lock_file.fileno()).st_size == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        listener.close()
        lock_file.close()
        parser.exit(2, "该人格数据已由本项目另一进程打开；请使用原窗口或先退出。\n")
    token = secrets.token_urlsafe(32)
    holder = {}
    app = create_app(project, args.data_root, args.persona, token, args.port,
                     shutdown_callback=lambda: setattr(holder["server"], "should_exit", True))
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    holder["server"] = server
    def open_when_ready():
        url = f"http://127.0.0.1:{args.port}/#token={token}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            try:
                with opener.open(f"http://127.0.0.1:{args.port}/health", timeout=.3) as response:
                    if response.status == 200:
                        print(f"网页已就绪。如浏览器未打开，请复制以下完整地址：\n{url}", flush=True)
                        if not args.no_browser:
                            try:
                                webbrowser.open(url)
                            except webbrowser.Error:
                                print("无法自动打开浏览器，请使用上方地址。", flush=True)
                        return
            except OSError:
                time.sleep(.1)
        print("网页服务尚未就绪，请查看启动错误。", flush=True)
    threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"MemoryLab 正在 127.0.0.1:{args.port} 启动。关闭网页不会退出服务；请使用网页的退出按钮。", flush=True)
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        lock_file.close()


if __name__ == "__main__":
    main()
