from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import hmac
import json
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from memory_core.paths import guarded
from memory_core.retrieval import LocalEmbedding, VectorIndex, hybrid_search
from memory_core.service import MemoryService
from memory_core.types import MemoryFault, Scope, identifier
from memory_core.worker import ConsolidationWorker
from .chat import ChatManager
from .media import Uploads
from .providers import FakeProvider, RemoteProvider


def create_app(project: Path, data_root: Path, persona_id="persona_default", bootstrap_token=None,
               port=8765, shutdown_callback=None, start_worker=True, service=None):
    project = Path(project).absolute()
    service = service or MemoryService(project, data_root, persona_id)
    bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
    auth_cookie = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    uploads = Uploads(project, service.run_id)
    model_lock = guarded(project, ".local", "caches", "models", "bge-m3", "model-lock.json")
    if service.vector_index is None and model_lock.is_file():
        locked = json.loads(model_lock.read_text(encoding="utf-8"))
        if any(f["path"] in ("pytorch_model.bin", "model.safetensors") for f in locked.get("files", [])):
            service.vector_index = VectorIndex(service.layout.persona_path("data", "indexes", "vectors"), LocalEmbedding(project, model_lock.parent, locked["revision"]))
            current = service.db.execute("SELECT value FROM ltm_meta WHERE key='embedding_generation'").fetchone()
            if not current or current[0] != service.vector_index.generation:
                with service.transaction():
                    service.db.execute("UPDATE ltm_jobs SET status='pending',run_id=? WHERE status='done'", (service.run_id,))
                    service.db.execute("UPDATE ltm_chunks SET status='invalid' WHERE generation!=?", (service.vector_index.generation,))
                    service.db.execute("INSERT OR REPLACE INTO ltm_meta VALUES('embedding_generation',?)", (service.vector_index.generation,))
    configuration = {"mode": "fake", "base_url": "", "model": "", "summary_model": "", "context_limit": 8192,
                     "output_limit": 1024, "request_timeout": 180, "thinking_mode": "default", "vision": False, "audio": False, "vision_model": "", "asr_model": ""}
    config_path = service.layout.persona_path("config", "provider.json")
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        configuration.update({k: loaded[k] for k in configuration if k in loaded})
        # A restart never silently enables a remote endpoint without in-memory credentials/confirmation.
        configuration["mode"] = "fake"
    runtime = {"provider": FakeProvider(), "config": configuration, "closing": False}
    config_lock = asyncio.Lock()
    chat = ChatManager(service, uploads, lambda: runtime["provider"])
    worker = ConsolidationWorker(service, lambda: runtime["provider"])

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await chat.boundary()
            service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service, app.state.chat, app.state.worker = service, chat, worker
    app.state.bootstrap_token = bootstrap_token
    app.state.runtime = runtime
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"http://"+host for host in allowed_hosts}

    @app.exception_handler(MemoryFault)
    async def memory_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=error.status)

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        return JSONResponse({"error": "操作未完成，请检查状态后重试；诊断不会记录消息正文"}, status_code=500)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return JSONResponse({"error": "不允许的请求主机"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "拒绝跨站请求"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") not in allowed_origins:
            return JSONResponse({"error": "修改请求必须来自本机网页"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/bootstrap":
            if not hmac.compare_digest(request.cookies.get("memorylab_session", ""), auth_cookie):
                return JSONResponse({"error": "网页会话已失效，请通过启动脚本重新打开"}, status_code=401)
            if request.method not in ("GET", "HEAD") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                return JSONResponse({"error": "修改操作校验失败"}, status_code=403)
        if request.headers.get("content-length"):
            try:
                if int(request.headers["content-length"]) > 26*1024*1024:
                    return JSONResponse({"error": "请求过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "请求长度无效"}, status_code=400)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def scope(session_id=""):
        # The client can select a session but never its persona, user, or data root.
        return Scope(service.persona_id, "local_user", identifier(session_id) if session_id else "", service.run_id)

    async def body(request):
        raw = bytearray()
        async for block in request.stream():
            raw.extend(block)
            if len(raw) > 1_000_000:
                raise MemoryFault("JSON 请求过大", 413)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError):
            raise MemoryFault("请求须为 JSON 对象")

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "web" / "index.html")

    app.mount("/web", StaticFiles(directory=Path(__file__).parent / "web"), name="web")

    @app.get("/health")
    def health():
        return {"ready": True, "app": "MemoryLab"}

    @app.post("/api/bootstrap")
    async def bootstrap(request: Request):
        data = await body(request)
        if not isinstance(data.get("token"), str) or not hmac.compare_digest(data["token"], bootstrap_token):
            raise MemoryFault("启动凭据无效，请用启动脚本打开网页", 401)
        result = JSONResponse({"ok": True})
        result.set_cookie("memorylab_session", auth_cookie, httponly=True, samesite="strict", path="/")
        return result

    @app.get("/api/state")
    def state():
        editor = json.loads(service.layout.persona_path("editor.json").read_text(encoding="utf-8"))
        manifest = json.loads(service.layout.persona_path("manifest.json").read_text(encoding="utf-8"))
        return {"csrf": csrf, "persona": {**manifest, **editor}, "stats": service.stats(scope()), "sessions": service.sessions(scope()),
                "config": runtime["config"], "connection": {"active": isinstance(runtime["provider"], RemoteProvider),
                "has_key": bool(getattr(runtime["provider"], "api_key", ""))},
                "provider": runtime["provider"].name, "worker_error": worker.last_error,
                "embedding": service.vector_index.embedding.name if service.vector_index else "未启用；全文检索可用",
                "data_flow": "原文与媒体保存在本项目。本地 embedding 不上传。启用远程模型后，仅发送选中的输入；未配置识别时媒体标为未识别。"}

    @app.post("/api/sessions")
    async def new_session(request: Request):
        data = await body(request)
        return {"id": service.new_session(scope(), str(data.get("title", "新对话")))}

    @app.get("/api/sessions/{session_id}")
    def history(session_id: str, before: int | None = None):
        bound = scope(session_id)
        return {"messages": service.history(bound, before), "temporary": chat.temporary.get(session_id, [])}

    @app.post("/api/chat")
    async def send(request: Request):
        data = await body(request)
        bound = scope(str(data.get("session_id", "")))
        request_id = chat.start(bound, str(data.get("text", "")), identifier(str(data.get("idempotency_key", ""))),
                                data.get("attachments", []), data.get("retry_of"))
        return {"request_id": request_id, "state": chat.state(bound, request_id)}

    @app.get("/api/requests/{request_id}")
    def request_state(request_id: str, session_id: str):
        return chat.state(scope(session_id), request_id)

    @app.post("/api/requests/{request_id}/stop")
    async def stop(request_id: str, request: Request):
        data = await body(request)
        return chat.cancel(scope(data.get("session_id", "")), request_id)

    @app.get("/api/requests/{request_id}/sources")
    def sources(request_id: str, session_id: str):
        bound = scope(session_id)
        request_info = chat.state(bound, request_id)
        result = []
        for entry in request_info["sources"]:
            item = dict(entry)
            try:
                row = service.read_messages(bound, [entry["message_id"]])[0]
                item["message"] = row
                item["changed"] = row["revision"] != entry["revision"]
            except MemoryFault:
                item["unavailable"] = True
            result.append(item)
        return {"sources": result, "notice": "清单表示实际提供的来源，不证明模型内部使用了哪一条；变更或遗忘后正文可能不可用。"}

    @app.get("/api/messages/{message_id}/sources")
    def answer_sources(message_id: str):
        row = service.read_messages(scope(), [message_id])[0]
        with service.lock:
            request_row = service.db.execute("SELECT id,session_id FROM ltm_requests WHERE user_id=? AND id=(SELECT idem FROM ltm_messages WHERE id=?)", (scope().user_id, message_id)).fetchone()
        if not request_row:
            return {"sources": [], "notice": "该消息没有生成请求来源清单"}
        return sources(request_row["id"], request_row["session_id"])

    @app.get("/api/search")
    async def search(q: str = "", session_id: str = "", since: float | None = None, until: float | None = None,
                     role: str = "", semantic: bool = False, cursor: int = 0):
        filters = {"session_id": session_id, "since": since, "until": until, "role": role}
        if semantic and q:
            return await asyncio.to_thread(hybrid_search, service, scope(), q, 30, filters)
        return {"messages": service.search_messages(scope(), q, filters, 30, cursor), "degraded": None}

    @app.get("/api/messages/{message_id}")
    def read_message(message_id: str):
        return {"messages": service.read_messages(scope(), [message_id], 3, 3)}

    @app.get("/api/messages/{message_id}/delete-preview")
    def preview(message_id: str):
        return {"messages": service.delete_preview(scope(), message_id)}

    @app.post("/api/corrections")
    async def correction(request: Request):
        data = await body(request)
        result = service.correct_memory(scope(), data["message_id"], data["text"], data.get("kind", "error"), data.get("event_time"))
        await chat.boundary()
        return {"id": result, "status": "saved"}

    @app.post("/api/forget")
    async def forget(request: Request):
        data = await body(request)
        result = service.delete_memory(scope(), data.get("message_ids", []))
        await chat.boundary()
        return result

    @app.get("/api/recycle")
    def recycle():
        return {"items": service.recycle_items(scope())}

    @app.post("/api/recycle/clear")
    async def clear_recycle(request: Request):
        await body(request)
        result = service.clear_recycle(scope())
        await chat.boundary()
        return result

    @app.post("/api/recycle/{item_id}/{action}")
    async def recycle_action(item_id: str, action: str, request: Request):
        await body(request)
        if action not in ("restore", "purge"):
            raise MemoryFault("无效回收站操作")
        result = service.restore_memory(scope(), item_id) if action == "restore" else service.purge_memory(scope(), item_id)
        await chat.boundary()
        return result

    @app.post("/api/recording")
    async def recording(request: Request):
        data = await body(request)
        if not isinstance(data.get("enabled"), bool):
            raise MemoryFault("enabled 必须为布尔值")
        result = service.set_recording(data["enabled"])
        await chat.boundary()
        return result

    @app.post("/api/uploads")
    async def upload(request: Request, session_id: str, filename: str):
        bound = scope(session_id)
        service.check(bound, session=True)
        content = bytearray()
        async for block in request.stream():
            content.extend(block)
            if len(content) > 25*1024*1024:
                raise MemoryFault("附件超过上限", 413)
        return uploads.put(bound, bytes(content), filename)

    @app.get("/api/attachments/{attachment_id}")
    def attachment(attachment_id: str):
        with service.lock:
            path, mime = service.read_attachment(scope(), attachment_id)
            return Response(path.read_bytes(), media_type=mime)

    @app.post("/api/attachments/{attachment_id}/correction")
    async def correct_recognition(attachment_id: str, request: Request):
        data = await body(request)
        text = str(data.get("text", ""))
        if not text.strip() or len(text) > 16000:
            raise MemoryFault("识别修订内容不能为空或超过 16000 字符")
        with service.lock:
            service.read_attachment(scope(), attachment_id)
            revision = service.db.execute("SELECT m.revision FROM ltm_messages m JOIN ltm_attachments a ON a.message_id=m.id WHERE a.id=?", (attachment_id,)).fetchone()[0]
            service.recognize_attachment(scope(), attachment_id, text, "user-correction", revision)
        await chat.boundary()
        return {"status": "saved"}

    @app.get("/api/recycle-attachments/{attachment_id}")
    def recycled_attachment(attachment_id: str):
        with service.lock:
            path, mime = service.read_attachment(scope(), attachment_id, recycle=True)
            return Response(path.read_bytes(), media_type=mime)

    @app.post("/api/attachments/{attachment_id}/recognize")
    async def recognize(attachment_id: str, request: Request):
        await body(request)
        provider = runtime["provider"]
        if not isinstance(provider, RemoteProvider):
            raise MemoryFault("请先配置支持图片描述或语音转写的远程服务", 409)
        with service.lock:
            path, mime = service.read_attachment(scope(), attachment_id)
            row = service.db.execute("SELECT m.revision FROM ltm_messages m JOIN ltm_attachments a ON a.message_id=m.id WHERE a.id=?", (attachment_id,)).fetchone()
            revision, epoch = row[0], service.epoch
            content = path.read_bytes()
        text, version = await provider.recognize(mime, content, path.name)
        with service.lock:
            if epoch != service.epoch:
                raise MemoryFault("识别任务所属上下文已失效", 409)
            service.recognize_attachment(scope(), attachment_id, text, version, revision)
        await chat.boundary()
        return {"status": "saved", "text": text, "version": version}

    def candidate_provider(data, *, require_model=True):
        candidate = {k: data.get(k, runtime["config"][k]) for k in configuration}
        if candidate["mode"] not in ("fake", "remote"):
            raise MemoryFault("无效模型模式")
        if candidate["mode"] == "fake":
            return candidate, FakeProvider()
        key = data.get("api_key", "")
        if not isinstance(key, str):
            raise MemoryFault("API 密钥须为文本")
        key = key.strip()
        provider = RemoteProvider(candidate, key, require_model=require_model)
        current = runtime["provider"]
        # Reuse only the active endpoint's key; edits must never forward it elsewhere.
        if not key and data.get("use_existing_key", True) is True and isinstance(current, RemoteProvider) and current.base == provider.base:
            provider.api_key = current.api_key
        candidate.update(base_url=provider.base, model=provider.model,
                         summary_model=str(candidate["summary_model"] or "").strip(),
                         context_limit=provider.context_limit, output_limit=provider.output_limit,
                         request_timeout=provider.request_timeout)
        return candidate, provider

    @app.post("/api/config/models")
    async def provider_models(request: Request):
        _, provider = candidate_provider(await body(request), require_model=False)
        if isinstance(provider, FakeProvider):
            return await provider.test()
        return await provider.list_models()

    @app.post("/api/config/test")
    async def test_provider(request: Request):
        _, provider = candidate_provider(await body(request))
        return await provider.test()

    @app.post("/api/config")
    async def configure(request: Request):
        data = await body(request)
        async with config_lock:
            candidate, provider = candidate_provider(data)
            result = await provider.test() if candidate["mode"] == "remote" else None
            temporary = service.layout.persona_path("config", "provider.pending.json")
            temporary.write_text(json.dumps({**candidate, "mode": "fake"}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(config_path)
            service.epoch += 1
            await chat.boundary()
            runtime["provider"], runtime["config"] = provider, candidate
            return {"status": "saved", "test": result, "credentials": "仅内存保存；重启恢复模拟模式"}

    @app.post("/api/config/credentials/clear")
    async def clear_provider_credentials(request: Request):
        await body(request)
        async with config_lock:
            service.epoch += 1
            await chat.boundary()
            runtime["provider"] = FakeProvider()
            runtime["config"] = {**runtime["config"], "mode": "fake"}
        return {"status": "cleared", "mode": "fake"}

    @app.post("/api/persona")
    async def configure_persona(request: Request):
        data = await body(request)
        service.set_persona(data["name"], data["instructions"])
        await chat.boundary()
        return {"status": "saved"}

    @app.post("/api/embedding")
    async def embedding(request: Request):
        data = await body(request)
        settings = guarded(project, ".local", "caches", "models", "bge-m3", "model-lock.json")
        if not settings.is_file():
            raise MemoryFault("请先运行准备脚本的 --embedding 选项下载并锁定本地模型", 409)
        locked = json.loads(settings.read_text(encoding="utf-8"))
        embedder = LocalEmbedding(project, settings.parent, locked["revision"], "cpu")
        await asyncio.to_thread(embedder.encode, ["连接测试"])
        with service.lock:
            service.epoch += 1
            service.vector_index = VectorIndex(service.layout.persona_path("data", "indexes", "vectors"), embedder)
            service.db.execute("INSERT OR REPLACE INTO ltm_meta VALUES('embedding_generation',?)", (service.vector_index.generation,))
            worker.retry(scope())
            service.db.execute("UPDATE ltm_jobs SET status='pending',run_id=? WHERE status='done'", (service.run_id,))
        await chat.boundary()
        return {"status": "ready", "embedding": embedder.name}

    @app.post("/api/jobs/retry")
    async def retry_jobs(request: Request):
        await body(request)
        worker.retry(scope())
        return {"status": "queued"}

    @app.get("/api/episodes")
    def episodes():
        with service.lock:
            service.barrier()
            return {"episodes": [dict(r) for r in service.db.execute("SELECT id,summary,event_time,source_ids,created FROM ltm_episodes WHERE user_id=? AND status='active' ORDER BY created DESC LIMIT 50", (scope().user_id,))]}

    @app.post("/api/exports")
    async def export(request: Request):
        data = await body(request)
        return service.export(scope(data.get("session_id", "")), data.get("message_ids"), data.get("filters"), bool(data.get("include_media")), bool(data.get("include_persona")))

    @app.get("/api/products")
    def products():
        return {"products": service.products(scope())}

    @app.get("/api/exports/{export_id}/file")
    def export_file(export_id: str, name: str):
        with service.lock:
            path = service.product_file(scope(), export_id, name)
            return Response(path.read_bytes(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{path.name}"'})

    @app.get("/api/exports/{export_id}/archive")
    def export_archive(export_id: str):
        path = service.export_archive(scope(), export_id)
        return FileResponse(path, media_type="application/zip", filename=export_id+".zip")

    @app.post("/api/snapshots")
    async def snapshot(request: Request):
        await body(request)
        return service.snapshot(scope())

    @app.post("/api/snapshots/{snapshot_id}/verify")
    async def verify(snapshot_id: str, request: Request):
        await body(request)
        manifest = service.verify_snapshot(scope(), snapshot_id)
        return {"status": "verified", "created": manifest["created"], "files": len(manifest["files"])}

    @app.post("/api/snapshots/{snapshot_id}/restore")
    async def restore(snapshot_id: str, request: Request):
        await body(request)
        await chat.boundary()
        return service.restore_snapshot(scope(), snapshot_id)

    @app.post("/api/users/open")
    async def open_users(request: Request):
        await body(request)
        import os
        # Hand off directory opening to Explorer; do not enumerate its contents.
        os.startfile(str(service.layout.persona / "users"))
        return {"status": "opened"}

    @app.post("/api/quit")
    async def quit_server(request: Request):
        await body(request)
        runtime["closing"] = True
        await chat.boundary()
        if shutdown_callback:
            asyncio.get_running_loop().call_later(0.2, shutdown_callback)
        return {"status": "stopping"}

    return app
