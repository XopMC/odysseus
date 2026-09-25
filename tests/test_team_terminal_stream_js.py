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


def test_terminal_snapshot_clears_reconnect_before_timeline_replay():
    source = (Path(__file__).resolve().parents[1] / "static/js/team-workspace.js").read_text()
    load_snapshot = source.split("async function loadSnapshot() {", 1)[1].split("async function loadEvidence() {", 1)[0]
    terminal = load_snapshot.index("if (terminalTeam()) {")
    replay = load_snapshot.index("await loadTimeline(teamId, token)")
    assert terminal < replay
    assert load_snapshot.index("clearReconnectNotice();", terminal) < replay
    assert load_snapshot.index("clearTimeout(retryTimer);", terminal) < replay
