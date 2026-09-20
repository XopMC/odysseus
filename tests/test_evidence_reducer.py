import hashlib
import json

import pytest

from src import evidence_reducer as reducer


def _source(failed=False):
    line = "FAILED tests/test_x.py::test_a - AssertionError: expected 1 got 2" if failed else "120 passed in 3.14s"
    return ("diagnostic output\n" * 300) + line


@pytest.mark.asyncio
async def test_valid_receipt_archives_exact_source(tmp_path, monkeypatch):
    monkeypatch.setattr("src.observation_pack.DATA_DIR", str(tmp_path))
    source = _source()
    sha = hashlib.sha256(source.encode()).hexdigest()
    async def call(_):
        return json.dumps({"source_sha256": sha, "exit_code": 0, "status": "passed", "evidence": [
            {"kind": "pass", "quote": "120 passed in 3.14s", "summary": "suite passed"}
        ]})
    result = await reducer.reduce(owner="a", session_id="s", tool_call_id="c", tool="bash",
                                  command="pytest -q", text=source, exit_code=0, llm_call=call)
    assert result and "full_output_artifact: obs_" in result["text"]


@pytest.mark.asyncio
async def test_invented_quote_fails_open():
    source = _source(True)
    sha = hashlib.sha256(source.encode()).hexdigest()
    async def call(_):
        return json.dumps({"source_sha256": sha, "exit_code": 1, "status": "failed", "evidence": [
            {"kind": "failure", "quote": "invented", "summary": "bad"}
        ]})
    assert await reducer.reduce(owner="a", session_id="s", tool_call_id="c", tool="bash",
                                command="pytest", text=source, exit_code=1, llm_call=call) is None


def test_secret_and_non_diagnostic_are_ineligible():
    assert not reducer.eligible("bash", "pytest", ("token=abcdefghijklmnop\n" * 300))
    assert not reducer.eligible("bash", "ls", _source())
