import json

import httpx


def mock_endpoint(monkeypatch):
    requests = []
    def route(request):
        requests.append(request)
        if request.url.host == "failure.example":
            return httpx.Response(401, text="secret upstream body")
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "test-chat"}]})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(route), **kwargs))
    return requests


def test_models_probe_save_reuse_key_and_clear(web, monkeypatch):
    client, app, scope = web
    requests = mock_endpoint(monkeypatch)
    settings = {"mode": "remote", "base_url": " https://model.example/v1/chat/completions/ ", "model": "test-chat", "api_key": "secret-connection-test", "request_timeout":300}
    before = app.state.service.stats(scope)
    models = client.post("/api/config/models", json={**settings, "model": ""})
    assert models.status_code == 200
    assert models.json()["models"] == ["test-chat"]
    probe = client.post("/api/config/test", json=settings)
    assert probe.status_code == 200
    assert probe.json()["chat_verified"] is True
    assert client.get("/api/state").json()["connection"] == {"active": False, "has_key": False}
    assert app.state.service.stats(scope) == before
    saved = client.post("/api/config", json=settings)
    assert saved.status_code == 200
    assert requests[-1].headers["Authorization"] == "Bearer secret-connection-test"
    state = client.get("/api/state").json()
    assert state["config"]["base_url"] == "https://model.example/v1"
    assert state["config"]["request_timeout"] == 300
    assert state["connection"] == {"active": True, "has_key": True}
    assert "secret-connection-test" not in json.dumps(state)
    on_disk = app.state.service.layout.persona_path("config", "provider.json").read_text()
    assert "secret-connection-test" not in on_disk
    assert json.loads(on_disk)["mode"] == "fake"
    assert json.loads(on_disk)["request_timeout"] == 300
    assert client.post("/api/config", json={"output_limit": 512}).status_code == 200
    assert requests[-1].headers["Authorization"] == "Bearer secret-connection-test"
    assert client.post("/api/config/credentials/clear", json={}).status_code == 200
    assert client.get("/api/state").json()["connection"] == {"active": False, "has_key": False}
    assert client.post("/api/config/test", json={**settings, "api_key":""}).status_code == 200
    assert "Authorization" not in requests[-1].headers


def test_changed_endpoint_never_inherits_key_and_failed_save_keeps_connection(web, monkeypatch):
    client, app, scope = web
    requests = mock_endpoint(monkeypatch)
    settings = {"mode":"remote", "base_url":"https://model.example/v1", "model":"test-chat", "api_key":"only-for-original"}
    assert client.post("/api/config", json=settings).status_code == 200
    before = app.state.runtime["provider"]
    disk = app.state.service.layout.persona_path("config", "provider.json").read_bytes()
    result = client.post("/api/config", json={**settings, "base_url":"https://failure.example/v1", "api_key":""})
    assert result.status_code == 502
    assert "密钥" in result.json()["error"]
    assert "Authorization" not in requests[-1].headers
    assert app.state.runtime["provider"] is before
    assert app.state.service.layout.persona_path("config", "provider.json").read_bytes() == disk
    assert client.post("/api/config/test", json={**settings, "api_key":"", "use_existing_key":False}).status_code == 200
    assert "Authorization" not in requests[-1].headers
    assert client.post("/api/config", json={"context_limit":"bad"}).status_code == 400
    assert app.state.runtime["provider"] is before


def test_qwen35_activation_saves_thinking_setting_and_uses_basic_probe(web,monkeypatch):
    client, app, scope = web
    requests=mock_endpoint(monkeypatch)
    settings={"mode":"remote","base_url":"https://api.siliconflow.cn/v1","model":"Qwen/Qwen3.5-27B", "api_key":"synthetic-only", "thinking_mode":"off"}
    result=client.post("/api/config",json=settings)
    assert result.status_code == 200
    assert json.loads(requests[-1].content)["enable_thinking"] is False
    assert result.json()["test"]["probe_thinking_disabled"] == ["Qwen/Qwen3.5-27B"]
    state=client.get("/api/state").json()
    assert state["config"]["thinking_mode"] == "off"
    persisted=json.loads(app.state.service.layout.persona_path("config","provider.json").read_text())
    assert persisted["thinking_mode"] == "off"
    assert "synthetic-only" not in json.dumps(state)
    assert "synthetic-only" not in json.dumps(persisted)
