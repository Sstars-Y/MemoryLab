from pathlib import Path
import asyncio
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance_preflight import runtime_key
from acceptance_budget import AcceptanceBudget, AcceptanceProvider, PROJECT, REPORT


async def main():
    budget = AcceptanceBudget()
    config = {"base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V4.1-Flash",
              "asr_model": "FunAudioLLM/SenseVoiceSmall", "context_limit": 16384,
              "output_limit": 1024, "request_timeout": 90}
    provider = AcceptanceProvider(config, runtime_key(PROJECT/"临时apikey.txt"), budget)
    try:
        result = await provider.test()
    except Exception as error:
        result = {"passed": False, "error": str(error)}
    else:
        result["passed"] = True
    (REPORT/"connection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    print(json.dumps({"reserved_rmb": budget.data.get("reserved_total_rmb", 0)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
