from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import re
import time
import uuid


class MemoryFault(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now() -> float:
    return time.time()


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise MemoryFault("无效的对象编号")
    return value


@dataclass(frozen=True)
class Scope:
    persona_id: str
    user_id: str
    session_id: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class Source:
    message_id: str
    revision: int
    content: str
    role: Literal["user", "assistant"]
    created_at: float
    corrections: tuple = ()


def reject_secrets(text: str) -> None:
    # A narrow guard, deliberately not advertised as comprehensive detection.
    if re.search(r"\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9_.-]{16,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text, re.I):
        raise MemoryFault("检测到疑似凭据，已拒绝保存或发送；请移除后重试")
