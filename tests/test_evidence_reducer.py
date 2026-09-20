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


def test_fused_mutation_projects_only_verification_output():
    content = json.dumps({"path": "x", "content": "y", "verify": {"command": "pytest -q"}})
    result = {"fused": True, "exit_code": 0, "output": "mutation wrapper",
              "verification": {"exit_code": 1, "stdout": "FAILED exact verification"}}
    candidate = reducer.candidate_from_result("write_file", content, result)
    assert candidate == {"command": "pytest -q", "text": "FAILED exact verification", "exit_code": 1}


def test_full_output_path_wins_over_inline_preview(tmp_path):
    full = tmp_path / "odysseus-tool-full.log"
    full.write_text("FULL\n" * 1000)
    candidate = reducer.candidate_from_result(
        "bash", "pytest -q", {"exit_code": 1, "output": "preview", "full_output_path": str(full)}
    )
    assert candidate["text"].startswith("FULL\nFULL") and "preview" not in candidate["text"]


@pytest.mark.asyncio
async def test_v1_receipt_records_line_hash_route_and_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(reducer, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.observation_pack.DATA_DIR", str(tmp_path))
    source = _source(True)
    sha = hashlib.sha256(source.encode()).hexdigest()
    quote = "FAILED tests/test_x.py::test_a - AssertionError: expected 1 got 2"
    async def call(_):
        return json.dumps({"schema": reducer.SCHEMA, "source_sha256": sha,
                           "status": "failure", "uncertain": False,
                           "evidence": [{"kind": "failure", "quote": quote}]})
    result = await reducer.reduce(owner="a", session_id="s", tool_call_id="c", tool="bash",
                                  command="pytest", text=source, exit_code=1, llm_call=call,
                                  route={"endpoint_id": "ep", "model": "m"})
    assert result and "line=" in result["text"] and "quote_sha256=" in result["text"]
    assert "reducer_endpoint: ep" in result["text"]
    assert "reducer_total_tokens:" in result["text"]
    assert result["usage"]["total_tokens"] > 0
    journal = next(tmp_path.glob("evidence_reducer/*/*/journal.jsonl")).read_text()
    assert '"event": "candidate"' in journal and '"event": "applied"' in journal


@pytest.mark.asyncio
async def test_provider_reducer_usage_is_recorded_exactly(tmp_path, monkeypatch):
    monkeypatch.setattr(reducer, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.observation_pack.DATA_DIR", str(tmp_path))
    source = _source()
    sha = hashlib.sha256(source.encode()).hexdigest()
    async def call(_):
        return {"text": json.dumps({"schema": reducer.SCHEMA, "source_sha256": sha,
                    "status": "success", "uncertain": False,
                    "evidence": [{"kind": "pass", "quote": "120 passed in 3.14s"}]}),
                "usage": {"prompt_tokens": 123, "completion_tokens": 17}}
    result = await reducer.reduce(owner="a", session_id="s", tool_call_id="provider", tool="bash",
                                  command="pytest -q", text=source, exit_code=0, llm_call=call)
    assert result["usage"] == {"input_tokens": 123, "output_tokens": 17,
                               "total_tokens": 140, "source": "provider"}
