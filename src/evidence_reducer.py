"""Fail-open reduction of large diagnostic output into verified evidence receipts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Awaitable, Callable, Optional

from src.observation_pack import archive


MIN_BYTES = 4 * 1024
MAX_CHARS = 600_000
MAX_ITEMS = 12
MAX_QUOTE = 600
_DIAGNOSTIC = re.compile(
    r"(?:^|\s)(?:pytest|python\s+-m\s+pytest|unittest|py_compile|cargo\s+(?:build|test|check)|"
    r"cmake\s+--build|make(?:\s|$)|ninja(?:\s|$)|(?:npm|pnpm|yarn)\s+(?:test|run\s+test)|"
    r"go\s+test)(?:\s|$)", re.I,
)
_SECRET = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,})",
    re.I,
)


def eligible(tool: str, command: str, text: str) -> bool:
    data = str(text or "")
    return (
        tool == "bash"
        and MIN_BYTES <= len(data.encode("utf-8"))
        and len(data) <= MAX_CHARS
        and bool(_DIAGNOSTIC.search(str(command or "")))
        and not _SECRET.search(data)
    )


def _validate(raw: str, source: str, *, source_sha256: str, exit_code: int) -> Optional[dict]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"source_sha256", "exit_code", "status", "evidence"}:
        return None
    if value["source_sha256"] != source_sha256 or value["exit_code"] != exit_code:
        return None
    expected = "passed" if exit_code == 0 else "failed"
    if value["status"] != expected or not isinstance(value["evidence"], list):
        return None
    if not 1 <= len(value["evidence"]) <= MAX_ITEMS:
        return None
    for item in value["evidence"]:
        if not isinstance(item, dict) or set(item) != {"kind", "quote", "summary"}:
            return None
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote or len(quote) > MAX_QUOTE or quote not in source:
            return None
        if item.get("kind") not in {"pass", "failure", "warning", "metric"}:
            return None
        if not isinstance(item.get("summary"), str) or not item["summary"].strip():
            return None
    if exit_code != 0 and not any(item["kind"] == "failure" for item in value["evidence"]):
        return None
    return value


def render_receipt(receipt: dict, artifact: dict) -> str:
    lines = [
        "[verified diagnostic evidence receipt]",
        f"status: {receipt['status']}",
        f"exit_code: {receipt['exit_code']}",
        f"source_sha256: {receipt['source_sha256']}",
        f"full_output_artifact: {artifact['id']}",
    ]
    for item in receipt["evidence"]:
        lines.extend((f"- {item['kind']}: {item['summary']}", f"  exact_quote: {item['quote']}"))
    return "\n".join(lines)


async def reduce(
    *, owner: Optional[str], session_id: Optional[str], tool_call_id: str,
    tool: str, command: str, text: str, exit_code: int,
    llm_call: Callable[[list[dict]], Awaitable[str]],
) -> Optional[dict]:
    """Return a smaller verified receipt, or None to retain the original."""
    if not eligible(tool, command, text):
        return None
    source_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    prompt = [
        {"role": "system", "content": (
            "Return ONLY strict JSON with keys source_sha256, exit_code, status, evidence. "
            "evidence is 1-12 objects with exactly kind, quote, summary. quote MUST be an exact "
            "substring of the source. status is passed only for exit_code 0, otherwise failed."
        )},
        {"role": "user", "content": json.dumps({
            "source_sha256": source_sha256, "exit_code": exit_code, "source": text,
        }, ensure_ascii=False)},
    ]
    try:
        raw = await llm_call(prompt)
        receipt = _validate(str(raw or ""), text, source_sha256=source_sha256, exit_code=exit_code)
        if receipt is None:
            return None
        artifact = archive(owner, session_id, tool_name=tool, tool_call_id=tool_call_id,
                           text=text, force=True)
        if not artifact:
            return None
        rendered = render_receipt(receipt, artifact)
        if len(rendered.encode("utf-8")) >= len(text.encode("utf-8")):
            return None
        return {"text": rendered, "receipt": receipt, "artifact": artifact}
    except Exception:
        return None
