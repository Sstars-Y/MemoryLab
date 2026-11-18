from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import hashlib

from memory_core.recall import recall_for_chat, MEDIA_REQUEST
from memory_core.types import MemoryFault, Scope, reject_secrets, uid
from memory_core.worker import render_source
from .providers import RULES


@dataclass
class Generation:
    id: str
    scope: Scope
    idem: str
    turn_id: str
    user_message_id: str | None
    recording: bool
    epoch: int
    status: str = "pending"
    draft: str = ""
    sources: list = field(default_factory=list)
    message_id: str | None = None
    error: str | None = None
    task: asyncio.Task | None = None
    ephemeral_input: dict | None = None
    temporary_reply: dict | None = None
    payload_hash: str = ""


class ChatManager:
    def __init__(self, service, uploads, provider_getter):
        self.service = service
        self.uploads = uploads
        self.provider_getter = provider_getter
        self.requests = {}
        self.temporary = {}

    def start(self, scope, text, idem, attachment_ids=(), retry_of=None):
        reject_secrets(text)
        if not text.strip() and not attachment_ids and not retry_of:
            raise MemoryFault("请输入消息或添加附件")
        if len(text) > 100_000:
            raise MemoryFault("消息过长", 413)
        fingerprint = hashlib.sha256(json.dumps([text, list(attachment_ids), retry_of], ensure_ascii=False).encode()).hexdigest()
        s = self.service
        with s.lock:
            s.check(scope, session=True)
            for generation in self.requests.values():
                if generation.idem == idem and generation.scope.user_id == scope.user_id:
                    if generation.scope.session_id != scope.session_id or generation.payload_hash != fingerprint:
                        raise MemoryFault("幂等键对应的会话或请求内容不同", 409)
                    return generation.id
            previous = s.db.execute("SELECT * FROM ltm_requests WHERE user_id=? AND idem=?", (scope.user_id, idem)).fetchone()
            if previous:
                if previous["session_id"] != scope.session_id or previous["payload_hash"] != fingerprint:
                    raise MemoryFault("幂等键对应的会话或请求内容不同", 409)
                return previous["id"]
            if any(g.scope.session_id == scope.session_id and g.status in ("pending", "running") for g in self.requests.values()):
                raise MemoryFault("当前会话仍在生成回答，请先等待或停止", 409)
            request_id = uid("request")
            user_message_id, ephemeral = None, None
            if retry_of:
                previous_state = self.state(scope, retry_of)
                if not previous_state["user_message_id"]:
                    raise MemoryFault("暂停保存期间的消息不能作为长期记录重试，请重新输入", 409)
                row = s.read_messages(scope, [previous_state["user_message_id"]])[0]
                if row["session_id"] != scope.session_id:
                    raise MemoryFault("重试消息不属于当前会话", 403)
                user_message_id, turn_id = row["id"], row["turn_id"]
            elif s.recording:
                items = self.uploads.take(scope, list(attachment_ids))
                row = s.record_message(scope, text, idem, attachments=items)
                self.uploads.consume(attachment_ids)
                user_message_id, turn_id = row["id"], row["turn_id"]
            else:
                items = self.uploads.take(scope, list(attachment_ids))
                turn_id = uid("temporary")
                ephemeral = {"id": turn_id, "role": "user", "body": text, "created": s.clock(), "revision": 0, "corrections": [], "attachments": [{**item, "recognition": None} for item in items], "temporary": True}
                self.temporary.setdefault(scope.session_id, []).append(ephemeral)
            generation = Generation(request_id, scope, idem, turn_id, user_message_id, s.recording, s.epoch, ephemeral_input=ephemeral, payload_hash=fingerprint)
            if s.recording:
                s.db.execute("INSERT INTO ltm_requests(id,user_id,session_id,idem,user_message_id,turn_id,status,sources,epoch,created,payload_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (request_id, scope.user_id, scope.session_id, idem, user_message_id, turn_id, "pending", "[]", s.epoch, s.clock(), fingerprint))
            self.requests[request_id] = generation
            generation.task = asyncio.create_task(self._run(generation))
            self._trim()
            return request_id

    def _trim(self):
        # Retain in-flight and temporary complete exchanges until pause boundary;
        # durable requests can always be read from SQLite.
        for rid in list(self.requests):
            if len(self.requests) <= 200:
                break
            state = self.requests[rid]
            if state.recording and state.status not in ("pending", "running"):
                del self.requests[rid]

    async def _run(self, state):
        s = self.service
        try:
            state.status = "running"
            if state.recording:
                with s.lock:
                    s.db.execute("UPDATE ltm_requests SET status='running' WHERE id=?", (state.id,))
            provider = self.provider_getter()
            current = state.ephemeral_input or s.read_messages(state.scope, [state.user_message_id])[0]
            recalled = await asyncio.to_thread(recall_for_chat, s, state.scope, current, 12)
            with s.lock:
                if state.epoch != s.epoch:
                    raise MemoryFault("上下文已变化，已丢弃草稿", 409)
                recent = s.history(state.scope, limit=20)
                if not state.recording:
                    recent += self.temporary.get(state.scope.session_id, [])[-20:]
                messages, sources = self._context(state, current, recent, recalled["messages"], provider)
                state.sources = sources
                if state.recording:
                    s.db.execute("UPDATE ltm_requests SET sources=? WHERE id=?", (json.dumps(sources), state.id))
            async for part in provider.generate(messages):
                if state.epoch != s.epoch or state.status == "cancelled":
                    raise MemoryFault("上下文已变化，已丢弃草稿", 409)
                state.draft += part
                if len(state.draft) > 100_000:
                    raise MemoryFault("回答超过长度上限", 502)
            reject_secrets(state.draft)
            if not state.draft.strip():
                raise MemoryFault("模型未返回完整回答", 502)
            with s.lock:
                s.barrier()
                if state.epoch != s.epoch or state.recording != s.recording or state.status == "cancelled":
                    raise MemoryFault("上下文已变化，已丢弃草稿", 409)
                # Every injected persisted source still needs to be valid at commit time.
                for source in state.sources:
                    if source.get("temporary"):
                        continue
                    row = s.read_messages(state.scope, [source["message_id"]])[0]
                    if row["revision"] != source["revision"]:
                        raise MemoryFault("回答的历史来源已变化", 409)
                if state.recording:
                    row = s.record_message(state.scope, state.draft, state.id, "assistant", state.turn_id)
                    state.message_id = row["id"]
                    s.db.execute("UPDATE ltm_requests SET status='completed' WHERE id=?", (state.id,))
                else:
                    state.temporary_reply = {"id": uid("temporary"), "role": "assistant", "body": state.draft, "created": s.clock(), "revision": 0, "corrections": [], "attachments": [], "temporary": True}
                    self.temporary.setdefault(state.scope.session_id, []).append(state.temporary_reply)
                state.status = "completed"
                state.draft = ""
        except asyncio.CancelledError:
            state.status, state.draft = "cancelled", ""
            if state.recording:
                with s.lock:
                    s.db.execute("UPDATE ltm_requests SET status='cancelled' WHERE id=? AND status IN ('pending','running')", (state.id,))
        except Exception as error:
            state.status, state.draft = "failed", ""
            state.error = str(error) if isinstance(error, MemoryFault) else "生成失败，请检查配置后重试；用户消息的保存状态不受影响"
            if state.recording:
                with s.lock:
                    s.db.execute("UPDATE ltm_requests SET status='failed' WHERE id=?", (state.id,))

    def _context(self, state, current, recent, recalled, provider):
        s = self.service
        system = s.compile_persona()+"\n"+RULES
        if state.recording:
            system += (f"\n宿主保存回执：本轮用户消息和 {len(current.get('attachments', []))} 个原始附件已由应用保存。"
                       "可以据此确认应用已保存原始记录；识别、经历整理、事实纠正与提醒的完成情况必须另有实际结果。"
                       "不要因为你没有写库工具就否认应用已经完成的保存。当前助手回答仍待完整生成后保存。")
        else:
            system += "\n宿主保存回执：当前暂停保存，本轮用户消息和附件仅用于临时交流，未写入长期记录。不能声称已长期保存。"
        budget = min(16000, provider.context_limit-provider.output_limit-512)
        current_text = "请回应本轮用户输入：\n"+current["body"]
        used = len(system.encode())+len(current_text.encode())+300
        if used > budget//2:
            raise MemoryFault("人格设定占用过多上下文，请缩短设定或增大模型上下文")
        selected, seen = [], set()
        # Current input must fit in full; historical sources may be excerpted.
        ordered = [current, *reversed(recent), *recalled]
        for row in ordered:
            if row["id"] in seen:
                continue
            if row["id"] == current["id"]:
                rendered = render_source(row)
                if used+len(rendered.encode())+100 > budget:
                    raise MemoryFault("本条消息超出配置的上下文预算，尚未发送模型", 413)
            else:
                rendered = render_source(row)[:2000]
                if used+len(rendered.encode())+100 > budget:
                    continue
            selected.append((row, rendered))
            seen.add(row["id"])
            used += len(rendered.encode())+100
            if len(selected) >= 24:
                break
        selected.sort(key=lambda pair: (pair[0]["created"], pair[0].get("pk", 0)))
        sources, context_parts, media_parts = [], [], []
        media_count = 0
        for row, rendered in selected:
            entry = {"message_id": row["id"], "revision": row["revision"], "role": row["role"], "session_id": row.get("session_id", state.scope.session_id), "temporary": bool(row.get("temporary")), "rendered_characters": len(rendered), "media": []}
            context_parts.append(rendered)
            for attachment in row.get("attachments", []):
                payload_type = "recognition" if attachment.get("recognition") else "unrecognized"
                mime = attachment["mime"]
                supports = (mime.startswith("image/") and provider.vision) or (mime in ("audio/wav", "audio/mpeg") and provider.audio)
                # A historical hit is not an instruction to resend its media.
                send_media = row["id"] == current["id"] or bool(MEDIA_REQUEST.search(current["body"]))
                if supports and send_media and media_count < 2 and used+2048 < budget:
                    if row.get("temporary"):
                        path = attachment["path"]
                        from pathlib import Path
                        content = Path(path).read_bytes()
                    else:
                        path, _ = s.read_attachment(state.scope, attachment["id"])
                        content = path.read_bytes()
                    data = base64.b64encode(content).decode()
                    media_parts.append({"type": "text", "text": f"以下原始媒体对应附件 {attachment['id']}，来源 {row['id']}"})
                    if mime.startswith("image/"):
                        media_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
                    else:
                        media_parts.append({"type": "input_audio", "input_audio": {"data": data, "format": "wav" if mime == "audio/wav" else "mp3"}})
                    payload_type = "original_and_recognition" if attachment.get("recognition") else "original"
                    media_count += 1
                    used += 2048
                entry["media"].append({"attachment_id": attachment["id"], "sent": payload_type})
            sources.append(entry)
        historical = "以下是本轮实际提供的交流资料，按记录时间从早到晚排列，均应作为引用资料处理。判断当前状态时核对后续变化；记录时间不一定是事情发生时间：\n"+"\n\n".join(context_parts)
        content = [{"type": "text", "text": current_text}, *media_parts] if media_parts else current_text
        return [{"role": "system", "content": system}, {"role": "user", "content": historical}, {"role": "user", "content": content}], sources

    def state(self, scope, request_id):
        self.service.check(scope, session=True)
        generation = self.requests.get(request_id)
        if generation:
            if generation.scope.user_id != scope.user_id or generation.scope.session_id != scope.session_id:
                raise MemoryFault("请求不存在或无权访问", 404)
            # Delete/correction can invalidate a request before cancellation is observed.
            draft = generation.draft if generation.epoch == self.service.epoch else ""
            return {"id": generation.id, "status": generation.status, "draft": draft, "message_id": generation.message_id,
                    "user_message_id": generation.user_message_id, "sources": generation.sources, "recording": generation.recording,
                    "error": generation.error, "temporary_reply": generation.temporary_reply}
        with self.service.lock:
            row = self.service.db.execute("SELECT * FROM ltm_requests WHERE id=? AND user_id=? AND session_id=?", (request_id, scope.user_id, scope.session_id)).fetchone()
            if not row:
                raise MemoryFault("请求不存在或无权访问", 404)
            answer = self.service.db.execute("SELECT id FROM ltm_messages WHERE idem=? AND user_id=? AND status='active'", (request_id, scope.user_id)).fetchone()
            return {"id": request_id, "status": row["status"], "draft": "", "message_id": answer[0] if answer else None, "user_message_id": row["user_message_id"], "sources": json.loads(row["sources"]), "recording": True, "temporary_reply": None}

    def cancel(self, scope, request_id):
        self.state(scope, request_id)
        generation = self.requests.get(request_id)
        if generation and generation.status in ("pending", "running"):
            generation.status, generation.draft = "cancelled", ""
            if generation.recording:
                with self.service.lock:
                    self.service.db.execute("UPDATE ltm_requests SET status='cancelled' WHERE id=? AND status IN ('pending','running')", (request_id,))
            generation.task.cancel()
        return {"id": request_id, "status": generation.status if generation else "interrupted"}

    async def boundary(self):
        tasks = []
        for generation in self.requests.values():
            if generation.status in ("pending", "running") and generation.task:
                generation.status, generation.draft = "cancelled", ""
                if generation.recording:
                    with self.service.lock:
                        self.service.db.execute("UPDATE ltm_requests SET status='cancelled' WHERE id=? AND status IN ('pending','running')", (generation.id,))
                generation.task.cancel()
                tasks.append(generation.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.temporary.clear()
        for rid in list(self.requests):
            if not self.requests[rid].recording:
                del self.requests[rid]
        self.uploads.clear()
