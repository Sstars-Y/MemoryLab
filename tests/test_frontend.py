from pathlib import Path
import shutil
import subprocess

import pytest


def test_page_bootstrap_and_authenticated_changes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to execute the browser script regression test")
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/web_bootstrap.test.cjs"], cwd=project,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
