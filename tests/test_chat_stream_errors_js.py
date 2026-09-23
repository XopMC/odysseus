"""Execute terminal stream-error classification under Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_MODULE = (_REPO / "static" / "js" / "chatStreamErrors.js").as_uri()


def test_terminal_provider_errors_preserve_text_and_never_auto_retry():
    if not shutil.which("node"):
        pytest.skip("node is not installed")

    script = f"""
      import {{ createTerminalStreamError, isRecoverableStreamError, streamErrorPresentation }} from {json.dumps(_MODULE)};
      const stringError = createTerminalStreamError({{ status: 401, error: 'invalid key' }});
      const objectError = createTerminalStreamError({{ status: 404, error: {{ message: 'model missing' }} }});
      const uncertain = createTerminalStreamError({{
        status: 504, error: 'safe timeout', error_category: 'unknown_outcome',
        fallback_eligible: true, retry_after_seconds: 999999,
      }});
      const forged = createTerminalStreamError({{
        error_category: '<script>bad</script>', retry_after_seconds: -1,
      }});
      console.log(JSON.stringify({{
        stringMessage: stringError.message,
        objectMessage: objectError.message,
        terminalRecoverable: isRecoverableStreamError(stringError),
        eofRecoverable: isRecoverableStreamError(new Error('Stream closed before completion')),
        networkRecoverable: isRecoverableStreamError(new TypeError('fetch failed')),
        uncertain: streamErrorPresentation(uncertain),
        uncertainCategory: uncertain.category,
        retryAfter: uncertain.retryAfterSeconds,
        fallbackEligible: uncertain.fallbackEligible,
        forgedPresentation: streamErrorPresentation(forged),
        forgedRetryAfter: forged.retryAfterSeconds,
      }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "stringMessage": "invalid key",
        "objectMessage": "model missing",
        "terminalRecoverable": False,
        "eofRecoverable": True,
        "networkRecoverable": True,
        "uncertain": {
            "category": "unknown_outcome",
            "title": "Request outcome is unknown",
            "action": "Check run and provider state; do not replay effectful work automatically.",
            "retryAfterSeconds": 86400,
            "fallbackEligible": True,
        },
        "uncertainCategory": "unknown_outcome",
        "retryAfter": 86400,
        "fallbackEligible": True,
        "forgedPresentation": None,
        "forgedRetryAfter": None,
    }


def test_chat_stream_renders_server_error_guidance_without_html_interpolation():
    source = (_REPO / "static" / "js" / "chat.js").read_text()
    assert "appendStreamErrorGuidance(errDiv, createTerminalStreamError(json))" in source
    assert "appendStreamErrorGuidance(errorHolder, err)" in source
    assert "guidance.textContent = `${presentation.title}. ${presentation.action}`" in source
    assert "guidance.innerHTML" not in source
