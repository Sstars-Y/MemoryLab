"""Read-only runtime checks. Never prints credentials or account identity."""
from pathlib import Path
import json
import re

import httpx


def runtime_key(path):
    raw = Path(path).read_text(encoding="utf-8-sig").strip()
    found = re.findall(r"sk-[A-Za-z0-9_-]+", raw)
    if len(found) == 1:
        return found[0]
    if raw and not any(char.isspace() for char in raw):
        return raw
    raise ValueError("Credential file must contain one API key")


if __name__ == "__main__":
    key = runtime_key(Path(__file__).resolve().parents[1] / "临时apikey.txt")
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False,
                      headers={"Authorization": "Bearer " + key}) as client:
        for route in ("models", "user/info"):
            response = client.get("https://api.siliconflow.cn/v1/" + route)
            item = {"route": route, "http_status": response.status_code}
            if response.status_code == 200:
                data = response.json().get("data")
                if route == "models":
                    ids = [row["id"] for row in data if isinstance(row, dict) and "id" in row]
                    item["requested_chat_available"] = "deepseek-ai/DeepSeek-V4.1-Flash" in ids
                    item["requested_asr_available"] = "FunAudioLLM/SenseVoiceSmall" in ids
                    item["requested_model_metadata"] = [row for row in data if row.get("id") in
                        ("deepseek-ai/DeepSeek-V4.1-Flash", "FunAudioLLM/SenseVoiceSmall")]
                else:
                    item["balance_query_available"] = "totalBalance" in (data or {})
            print(json.dumps(item, ensure_ascii=False), flush=True)
