from pathlib import Path
import shutil
import uuid

import pytest

from memory_core.service import MemoryService
from memory_core.types import Scope


@pytest.fixture
def lab():
    project = Path(__file__).resolve().parents[1]
    root = project / ".local" / "verification" / uuid.uuid4().hex
    service = MemoryService(project, root, "persona_test")
    bound = Scope(service.persona_id, "local_user")
    session = service.new_session(bound)
    scope = Scope(service.persona_id, "local_user", session)
    yield service, scope
    service.close()


@pytest.fixture
def web(lab):
    from fastapi.testclient import TestClient
    from memory_lab.server import create_app
    service, scope = lab
    app = create_app(service.layout.project, service.layout.data, service.persona_id, "test-token", port=8765, start_worker=False, service=service)
    # Do not enter lifespan: the lab fixture owns the DB's lifetime.
    client = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    origin = {"Origin": "http://127.0.0.1:8765"}
    assert client.post("/api/bootstrap", json={"token": "test-token"}, headers=origin).status_code == 200
    token = client.get("/api/state").json()["csrf"]
    client.headers.update({**origin, "X-CSRF-Token": token})
    yield client, app, scope
    client.close()
