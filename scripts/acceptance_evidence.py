"""Record source identity for synthetic acceptance reports, never persona data."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import platform
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from memory_core.paths import guarded


def collect_evidence(data_root):
    sources=[]
    # Only these source directories are inspected; never traverse a data root.
    for folder in ("memory_core","memory_lab","scripts","tests"):
        sources.extend(guarded(PROJECT,folder).glob("*.py"))
    web=guarded(PROJECT,"memory_lab","web")
    sources.extend(web.glob("*.js"))
    sources.extend(web.glob("*.html"))
    sources.extend(web.glob("*.css"))
    return {"captured_utc":datetime.now(timezone.utc).isoformat(),
            "python":sys.version,"platform":platform.platform(),"data_root":str(data_root),
            "source_sha256":{str(p.relative_to(PROJECT)):hashlib.sha256(guarded(PROJECT,str(p.relative_to(PROJECT))).read_bytes()).hexdigest()
                             for p in sorted(sources)}}
