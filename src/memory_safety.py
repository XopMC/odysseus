"""Conservative screening for credential-shaped saved memories.

Saved memories remain available to the owner in the memory manager, but a
credential accidentally saved as a memory must not be injected into unrelated
model prompts or copied into per-message "memories used" metadata.
"""

from __future__ import annotations

import re
from typing import Any


_LABELLED_SECRET = re.compile(
    r"\b(?:"
    r"(?:sudo\s+)?password|passwd|passphrase|"
    r"api[_\s-]*key|access[_\s-]*token|refresh[_\s-]*token|"
    r"client[_\s-]*secret|credential(?:s)?|private[_\s-]*key|"
    r"recovery[_\s-]*code|bearer[_\s-]*token"
    r")\b\s*(?:is\s+|[:=\-]\s*)\S+",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
_PRIVATE_KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_COMMON_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})\b")


def is_sensitive_memory_text(value: Any) -> bool:
    """Return true for explicit or recognizable credential material.

    This is a fail-closed *recall* gate, not a storage validator: it never
    mutates or deletes owner data. A false positive merely keeps one memory
    out of model context and the per-message recall detail.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    return bool(
        _LABELLED_SECRET.search(value)
        or _BEARER_TOKEN.search(value)
        or _PRIVATE_KEY_BLOCK.search(value)
        or _COMMON_TOKEN.search(value)
    )
