"""Terminal Team snapshots must not keep a reconnecting SSE poller alive."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_terminal_team_snapshots_do_not_reconnect():
    if not shutil.which("node"):
        pytest.skip("node is unavailable")
    module = (Path(__file__).resolve().parents[1] / "static/js/team-workspace.js").as_uri()
    script = """
      import { shouldConnectTeamEvents } from MODULE;
      const values = [null, {status:'pending'}, {status:'running'},
        {status:'paused'}, {status:'waiting_approval'}, {status:'blocked'},
        {status:'done'}, {status:'completed'}, {status:'cancelled'},
        {task:{status:'done'}}];
      console.log(JSON.stringify(values.map(shouldConnectTeamEvents)));
    """.replace("MODULE", json.dumps(module))
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True] * 6 + [False] * 4
