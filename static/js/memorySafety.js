// Conservative defense-in-depth for legacy per-message memory metadata.
// Keep in sync with src/memory_safety.py; this prevents old stored recall rows
// from being copied into the browser's title/expandable detail after reload.
const SENSITIVE_MEMORY_RE = /\b(?:(?:sudo\s+)?password|passwd|passphrase|api[_\s-]*key|access[_\s-]*token|refresh[_\s-]*token|client[_\s-]*secret|credentials?|private[_\s-]*key|recovery[_\s-]*code|bearer[_\s-]*token)\b\s*(?:is\s+|[:=\-]\s*)\S+|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})\b/i;

export function isSensitiveMemoryText(value) {
  return typeof value === 'string' && SENSITIVE_MEMORY_RE.test(value);
}

export function safeMemoryRows(rows) {
  return Array.isArray(rows)
    ? rows.filter(row => row && !isSensitiveMemoryText(row.text))
    : [];
}
