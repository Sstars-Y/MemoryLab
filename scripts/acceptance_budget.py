"""Conservative, durable cost reservations for this synthetic acceptance run.

No keys, request bodies, media or answers are written into the cost ledger.
Rates checked against SiliconFlow pricing on 2026-11-14: Flash input 2 RMB/M,
output 8 RMB/M at the higher time tier; SenseVoiceSmall listed free.
"""
from decimal import Decimal
import json
from pathlib import Path
import threading
import time

from memory_lab.providers import RemoteProvider

PROJECT = Path(__file__).resolve().parents[1]
REPORT = PROJECT / ".local/verification/acceptance-20261114"


class AcceptanceBudget:
    def __init__(self):
        REPORT.mkdir(parents=True, exist_ok=True)
        self.path = REPORT / "remote-cost.json"
        self.lock = threading.Lock()
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "limit_rmb": 15, "account_bill_verified": False,
            "method": "durable upper-bound reservation; full UTF-8 bytes as input tokens plus media allowance; all output at max_tokens; no cache or off-peak discount",
            "pricing_checked": "2026-11-14", "pricing_source": "https://siliconflow.cn/pricing",
            "input_rmb_per_million": 2, "output_rmb_per_million": 8,
            "entries": [],
        }

    def reserve(self, label, messages, max_tokens, *, free_asr=False):
        encoded = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        images = sum(1 for message in messages if isinstance(message.get("content"), list)
                     for part in message["content"] if part.get("type") == "image_url")
        upper_input = encoded + 1024 + images * 32768
        amount = Decimal(0) if free_asr else (Decimal(upper_input)*2 + Decimal(max_tokens)*8)/1_000_000
        with self.lock:
            total = sum(Decimal(str(entry["reserved_rmb"])) for entry in self.data["entries"])
            if total + amount > Decimal(15):
                raise RuntimeError("Acceptance API budget exhausted; no request sent")
            self.data["entries"].append({"label": label, "time_utc": time.time(),
                "reserved_input_tokens": upper_input, "max_output_tokens": max_tokens,
                "reserved_rmb": float(amount), "free_asr": free_asr})
            self.data["reserved_total_rmb"] = float(total + amount)
            pending = self.path.with_suffix(".pending")
            pending.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            pending.replace(self.path)


class AcceptanceProvider(RemoteProvider):
    def __init__(self, config, key, budget):
        super().__init__(config, key)
        self.budget = budget
        self.messages_seen = []

    async def generate(self, messages, *, output_limit=None, probe=False):
        self.budget.reserve("chat-probe" if probe else "chat", messages, output_limit or self.output_limit)
        self.messages_seen = messages
        async for part in super().generate(messages, output_limit=output_limit, probe=probe):
            yield part

    async def summarize(self, rows):
        from memory_core.worker import render_source
        from memory_lab.providers import RULES
        messages = [{"role": "system", "content": RULES + "\n整理经历、变化及来源编号；不要虚构。"},
                    {"role": "user", "content": "\n".join(render_source(row) for row in rows)}]
        return "".join([part async for part in self.generate(messages)])

    async def recognize(self, mime, content, filename):
        if mime.startswith("audio/"):
            if self.asr_model != "FunAudioLLM/SenseVoiceSmall":
                raise RuntimeError("Unbudgeted ASR model")
            self.budget.reserve("asr", [], 0, free_asr=True)
        else:
            raise RuntimeError("Use the budgeted chat vision test for image recognition")
        return await super().recognize(mime, content, filename)
