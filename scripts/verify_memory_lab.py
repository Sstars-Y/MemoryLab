from pathlib import Path
import json
import subprocess
import sys
import time
from acceptance_evidence import collect_evidence

project = Path(__file__).resolve().parents[1]
report = project/".local/verification/latest"
report.mkdir(parents=True, exist_ok=True)
started = time.time()
provenance=collect_evidence(report)
result = subprocess.run([sys.executable,"-m","pytest","-q","--junitxml",str(report/"junit.xml")], cwd=project)
(report/"run.json").write_text(json.dumps({"started":started,"finished":time.time(),"exit_code":result.returncode,"provenance":provenance}), encoding="utf-8")
raise SystemExit(result.returncode)
