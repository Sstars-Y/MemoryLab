import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx


def test_port_conflict_does_not_open_database_or_kill_listener():
    project=Path(__file__).resolve().parents[1]
    data=project/".local/verification"/("port_"+uuid.uuid4().hex)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0))
        listener.listen(1)
        port=listener.getsockname()[1]
        result=subprocess.run([sys.executable,"-m","memory_lab","--data-root",str(data),"--port",str(port),"--no-browser"],cwd=project,capture_output=True,timeout=30)
        assert result.returncode==2
        assert not data.exists()
        assert listener.fileno()>=0


def test_headless_launcher_readiness_and_same_persona_lock():
    project=Path(__file__).resolve().parents[1]
    data=project/".local/verification"/("launcher_"+uuid.uuid4().hex)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1",0))
        port=reservation.getsockname()[1]
    child=subprocess.Popen([sys.executable,"-m","memory_lab","--data-root",str(data),"--port",str(port),"--no-browser"],cwd=project,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        ready=False
        with httpx.Client(trust_env=False,timeout=.5) as client:
            for _ in range(100):
                if child.poll() is not None:
                    break
                try:
                    ready=client.get(f"http://127.0.0.1:{port}/health").status_code==200
                    if ready:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            assert ready
            assert client.get(f"http://127.0.0.1:{port}/api/state").status_code==401
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1",0))
            second_port=reservation.getsockname()[1]
        second=subprocess.run([sys.executable,"-m","memory_lab","--data-root",str(data),"--port",str(second_port),"--no-browser"],cwd=project,capture_output=True,timeout=30)
        assert second.returncode==2
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=15)
