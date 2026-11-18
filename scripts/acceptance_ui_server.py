"""Isolated browser acceptance instance, with budgeted chat and fake summaries."""
from pathlib import Path
import json
import secrets
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
import uvicorn
from acceptance_budget import AcceptanceBudget, AcceptanceProvider, REPORT
from acceptance_preflight import runtime_key
from memory_core.service import MemoryService
from memory_lab.server import create_app
from memory_lab.providers import FakeProvider


def main():
    port=8877
    token=secrets.token_urlsafe(24)
    root=REPORT/("ui-"+time.strftime("%Y%m%d-%H%M%S"))
    root.mkdir(parents=True)
    service=MemoryService(PROJECT,root/"data","persona_ui_acceptance")
    config={"mode":"remote","base_url":"https://api.siliconflow.cn/v1","model":"deepseek-ai/DeepSeek-V4.1-Flash",
            "summary_model":"deepseek-ai/DeepSeek-V4.1-Flash","context_limit":16384,"output_limit":1024,
            "request_timeout":90,"thinking_mode":"default","vision":True,"audio":False,
            "vision_model":"","asr_model":"FunAudioLLM/SenseVoiceSmall"}
    provider=AcceptanceProvider(config,runtime_key(PROJECT/"临时apikey.txt"),AcceptanceBudget())
    app=create_app(PROJECT,service.layout.data,service.persona_id,token,port=port,service=service)
    app.state.runtime["config"]=config
    app.state.runtime["provider"]=provider
    app.state.worker.provider_getter=lambda:FakeProvider(delay=0)
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="warning",access_log=False))
    app.state.acceptance_server=server
    # The normal route calls this closure when the user selects quit.
    # create_app keeps shutdown_callback from construction, so the test process
    # is stopped by its owned process handle after UI verification.
    information={"url":f"http://127.0.0.1:{port}/#token={token}","data_root":str(service.layout.data),
                 "summary_provider":"fake (labelled in summaries)","chat_provider":provider.model}
    (root/"launch.json").write_text(json.dumps(information,indent=2),encoding="utf-8")
    print(json.dumps(information,ensure_ascii=False),flush=True)
    server.run()


if __name__=="__main__":
    main()
