import json
import asyncio

import httpx
import pytest

from memory_core.types import MemoryFault
from memory_lab.providers import RemoteProvider


def config():
    return {"base_url":"https://model.example/v1", "model":"synthetic-chat", "context_limit":8192,"output_limit":1024}


async def test_remote_payload_and_complete_stream(monkeypatch):
    requests = []
    def route(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200,json={"data":[{"id":"synthetic-chat"}]})
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"完成回答"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', headers={"content-type":"text/event-stream"})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(route),**kwargs))
    provider = RemoteProvider(config(),"only-in-memory")
    assert (await provider.list_models())["selected_listed"]
    assert (await provider.test())["chat_verified"]
    messages=[{"role":"user","content":[{"type":"text","text":"合成图片"},{"type":"image_url","image_url":{"url":"data:image/png;base64,c3ludGhldGlj"}}]}]
    result = "".join([part async for part in provider.generate(messages)])
    assert result == "完成回答"
    assert json.loads(requests[-1].content)["messages"] == messages
    assert requests[-1].headers["Authorization"] == "Bearer only-in-memory"


async def test_interrupted_stream_and_remote_error_do_not_complete(monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,text='data: {"choices":[{"delta":{"content":"incomplete"}}]}\n\n')),**kwargs))
    provider=RemoteProvider(config())
    with pytest.raises(MemoryFault,match="中断"):
        async for _ in provider.generate([{"role":"user","content":"test"}]):
            pass


def test_remote_rejects_credential_urls_and_insecure_transport():
    for base in ("https://user:password@example.com/v1","http://example.com/v1","file:///secret","https://example.com/v1?key=secret"):
        with pytest.raises(MemoryFault):
            RemoteProvider({**config(),"base_url":base})


def test_remote_normalizes_pasted_routes_and_validates_limits():
    provider = RemoteProvider({**config(), "base_url": " https://model.example/v1/chat/completions/ "})
    assert provider.base == "https://model.example/v1"
    for value in (None, "NaN", 2048.5, True, [], 2**100):
        with pytest.raises(MemoryFault):
            RemoteProvider({**config(), "context_limit": value})
    for base in ("https://model.example:bad/v1", "https://model.example/v1\nwrong", "https://model.example/v1?api_key=secret", "https://model.example\\v1"):
        with pytest.raises(MemoryFault):
            RemoteProvider({**config(), "base_url": base})


async def test_reply_probe_does_not_depend_on_model_listing(monkeypatch):
    requests = []
    def route(request):
        requests.append(request)
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["max_tokens"] == 128
        assert payload["messages"] == [{"role": "user", "content": "Reply with OK only."}]
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(route), **kwargs))
    result = await RemoteProvider({**config(), "summary_model": "summary-model"}).test()
    assert result["models"] == ["synthetic-chat", "summary-model"]
    assert result["media_verified"] is False
    assert len(requests) == 2


@pytest.mark.parametrize("code,reason", [(401,"密钥"),(403,"权限"),(404,"基址"),(429,"额度"),(503,"暂时"),(302,"重定向")])
async def test_actionable_errors_do_not_echo_upstream_body(monkeypatch, code, reason):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(code, text="private-upstream-secret")), **kwargs))
    with pytest.raises(MemoryFault, match=reason) as error:
        await RemoteProvider(config(), "credential-test").test()
    assert "private-upstream-secret" not in str(error.value)


async def test_list_models_without_selected_model_and_bad_json(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={"data":[{"id":"model-b"},{"id":"model-a"},{"id":"model-a"},None]})), **kwargs))
    result = await RemoteProvider({**config(), "model":""}, require_model=False).list_models()
    assert result["models"] == ["model-a", "model-b"]
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(200,json=[])), **kwargs))
    with pytest.raises(MemoryFault, match="手动"):
        await RemoteProvider(config()).list_models()


async def test_stop_marker_finishes_without_waiting_for_gateway_close(monkeypatch):
    class FinishedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
            raise httpx.ReadTimeout("gateway left the completed response open")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=FinishedStream())), **kwargs))
    assert "".join([part async for part in RemoteProvider(config()).generate([])]) == "OK"


@pytest.mark.parametrize("first,stage", [
    (b'', "尚未返回正文"),
    (b'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}\n\n', "推理片段"),
    (b'data: {"choices":[{"delta":{"content":"private draft"}}]}\n\n', "草稿已丢弃"),
])
async def test_overall_deadline_stops_keepalives_without_retries(monkeypatch, first, stage):
    calls = []
    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if first:
                yield first
            while True:
                yield b': heartbeat\n\n'
                await asyncio.sleep(.005)
    def route(request):
        calls.append(request)
        return httpx.Response(200,stream=WaitingStream())
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(route),**kwargs))
    provider = RemoteProvider(config())
    provider.request_timeout = .03  # Exercise the deadline without a wall-clock wait.
    with pytest.raises(MemoryFault, match=stage) as error:
        async for _ in provider.generate([]):
            pass
    assert len(calls) == 1
    assert "private" not in str(error.value)


@pytest.mark.parametrize("exception,stage", [(httpx.ConnectTimeout,"连接模型服务"),(httpx.ReadTimeout,"响应头")])
async def test_timeout_stage_is_reported_without_provider_secrets(monkeypatch, exception, stage):
    def route(request):
        raise exception("private diagnostic payload")
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(route),**kwargs))
    with pytest.raises(MemoryFault,match=stage) as error:
        await RemoteProvider(config(),"private-key").test()
    assert "聊天模型测试未通过" in str(error.value)
    assert "不写入聊天记录" in str(error.value)
    assert "private" not in str(error.value)


def test_timeout_configuration_is_bounded():
    for value in (0, 29, 601, "bad", None, True):
        with pytest.raises(MemoryFault):
            RemoteProvider({**config(),"request_timeout":value})
    assert RemoteProvider({**config(),"request_timeout":300}).request_timeout == 300


async def test_siliconflow_qwen35_probe_disables_thinking_without_changing_chat(monkeypatch):
    payloads = []
    def route(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload["max_tokens"] == 128 and payload.get("enable_thinking") is not False:
            raise httpx.ReadTimeout("simulated slow reasoning in a short activation probe")
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(route),**kwargs))
    provider = RemoteProvider({**config(),"base_url":"https://api.siliconflow.cn/v1", "model":"Qwen/Qwen3.5-27B",
                               "summary_model":"Qwen/Qwen3.5-9B", "thinking_mode":"on"})
    result = await provider.test()
    assert result["probe_thinking_disabled"] == ["Qwen/Qwen3.5-27B","Qwen/Qwen3.5-9B"]
    assert result["thinking_verified"] is False
    assert all(payload["enable_thinking"] is False for payload in payloads)
    assert all(payload["messages"] == [{"role":"user","content":"Reply with OK only."}] for payload in payloads)
    assert provider.thinking_mode == "on"
    assert "".join([part async for part in provider.generate([{ "role":"user", "content":"synthetic chat"}])]) == "OK"
    assert payloads[-1]["enable_thinking"] is True
    assert payloads[-1]["max_tokens"] == 1024
    assert await provider.summarize([]) == "OK"
    assert payloads[-1]["enable_thinking"] is True
    assert payloads[-1]["model"] == "Qwen/Qwen3.5-9B"


@pytest.mark.parametrize("base,model,mode,expected", [
    ("https://api.siliconflow.cn/v1","Qwen/Qwen3.5-27B","off",False),
    ("https://api.siliconflow.cn/v1","Qwen/Qwen3.5-27B","on",True),
    ("https://api.siliconflow.cn/v1","Qwen/Qwen3.5-27B","default",None),
    ("https://api.siliconflow.cn/v1","other/model","off",None),
    ("https://model.example/v1","Qwen/Qwen3.5-27B","off",None),
])
async def test_thinking_extension_is_scoped_to_supported_provider_and_model(monkeypatch,base,model,mode,expected):
    payloads = []
    def route(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(route),**kwargs))
    provider=RemoteProvider({**config(),"base_url":base,"model":model,"thinking_mode":mode})
    assert "".join([part async for part in provider.generate([])]) == "OK"
    if expected is None:
        assert "enable_thinking" not in payloads[-1]
    else:
        assert payloads[-1]["enable_thinking"] is expected


def test_invalid_thinking_mode_is_rejected():
    with pytest.raises(MemoryFault,match="思考模式"):
        RemoteProvider({**config(),"thinking_mode":"maybe"})
