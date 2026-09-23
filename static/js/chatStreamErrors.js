/** Build a terminal stream error while preserving provider-supplied text. */
const STREAM_ERROR_CATEGORIES = new Set([
  'rate_limit', 'provider_unload', 'schema_mismatch', 'context',
  'timeout', 'transport', 'unknown_outcome', 'provider_error',
]);

export function createTerminalStreamError(payload = {}) {
  const rawError = payload.error;
  const message = (
    payload.text
    || (typeof rawError === 'string' ? rawError : rawError?.message)
    || `Error ${payload.status || 'unknown'}`
  );
  const error = new Error(message);
  error.name = 'TerminalStreamError';
  error.terminalStreamError = true;
  error.status = payload.status;
  error.category = STREAM_ERROR_CATEGORIES.has(payload.error_category)
    ? payload.error_category
    : '';
  error.fallbackEligible = payload.fallback_eligible === true;
  const retryAfter = Number(payload.retry_after_seconds);
  error.retryAfterSeconds = Number.isFinite(retryAfter) && retryAfter >= 0
    ? Math.min(retryAfter, 86400)
    : null;
  return error;
}

/** Return safe operator guidance for a classified provider error. */
export function streamErrorPresentation(error, translate = (value) => value) {
  const guidance = {
    rate_limit: ['Model request was rate limited', 'Wait before retrying manually.'],
    provider_unload: ['Selected model is not loaded', 'Load the model on the selected endpoint, then retry.'],
    schema_mismatch: ['Model rejected the request schema', 'Choose a compatible model or disable unsupported tool features.'],
    context: ['Prompt exceeds the model context window', 'Reduce the request or compact the conversation before retrying.'],
    timeout: ['Model request timed out', 'Check the endpoint before retrying.'],
    transport: ['Model endpoint connection failed', 'Check endpoint availability before retrying.'],
    unknown_outcome: ['Request outcome is unknown', 'Check run and provider state; do not replay effectful work automatically.'],
    provider_error: ['Model endpoint returned an error', 'Review the error and retry only after correcting its cause.'],
  };
  const item = guidance[error?.category];
  if (!item) return null;
  return {
    category: error.category,
    title: translate(item[0]),
    action: translate(item[1]),
    retryAfterSeconds: error.retryAfterSeconds,
    fallbackEligible: error.fallbackEligible === true,
  };
}

/** Only connection-class stream failures are safe to resubmit automatically. */
export function isRecoverableStreamError(error) {
  if (!error || error.terminalStreamError || error.name === 'TerminalStreamError') return false;
  if (error.name === 'TypeError') return true;
  const message = (error.message || '').toLowerCase();
  if (/\btool\b|unsupported|json|parse|\b4\d\d\b|\b5\d\d\b/.test(message)) return false;
  return /network|fetch|connection|reset|closed|aborted|stream|tim(?:e|ed)\s?out|econn|eof/.test(message);
}
