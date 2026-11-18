def test_host_auth_origin_and_csrf(web):
    client, app, scope = web
    assert client.get("/api/state", headers={"Host":"evil.test"}).status_code == 403
    assert client.post("/api/sessions", json={}, headers={"Origin":"https://evil.test"}).status_code == 403
    assert client.post("/api/sessions", json={}, headers={"X-CSRF-Token":"wrong"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/state").status_code == 401


def test_web_history_search_and_untrusted_text(web):
    client, app, scope = web
    row = app.state.service.record_message(scope, '<script>alert("x")</script> 小王', "html")
    result = client.get("/api/search", params={"q":"小王"})
    assert result.status_code == 200
    assert result.json()["messages"][0]["body"].startswith("<script>")
    assert client.get("/").headers["Content-Security-Policy"].find("script-src 'self'") >= 0
    preview = client.get(f"/api/messages/{row['id']}/delete-preview").json()
    assert len(preview["messages"]) == 1
    deleted = client.post("/api/forget", json={"message_ids":[row["id"]]})
    assert deleted.status_code == 200
    assert client.get(f"/api/messages/{row['id']}").status_code == 404
    assert client.post(f"/api/recycle/{deleted.json()['id']}/restore", json={}).status_code == 200


def test_scope_not_client_controlled_and_config_no_secret(web):
    client, app, scope = web
    assert client.post("/api/sessions", json={"persona_id":"evil","user_id":"evil"}).status_code == 200
    assert app.state.service.sessions(scope)[0]["user_id"] == "local_user"
    response = client.post("/api/config", json={"mode":"fake", "api_key":"secret-test"})
    assert response.status_code == 200
    path = app.state.service.layout.persona_path("config", "provider.json")
    assert "secret-test" not in path.read_text()
