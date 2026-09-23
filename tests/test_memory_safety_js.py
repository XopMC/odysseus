import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_memory_safety_filter_blocks_credentials_but_keeps_normal_facts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    module = (Path(__file__).resolve().parents[1] / "static/js/memorySafety.js").as_uri()
    script = (
        f"import {{ isSensitiveMemoryText, safeMemoryRows }} from {module!r};"
        "if (!isSensitiveMemoryText('Jetson host sudo password: QA_SECRET_SENTINEL')) process.exit(1);"
        "if (isSensitiveMemoryText('User prefers concise technical answers.')) process.exit(2);"
        "const rows = safeMemoryRows([{text:'Password: QA_SECRET_SENTINEL'}, {text:'User prefers concise answers'}]);"
        "if (rows.length !== 1 || rows[0].text !== 'User prefers concise answers') process.exit(3);"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
