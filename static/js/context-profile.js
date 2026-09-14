// Portable data only: no task identity, authority, endpoint or conversation data.
export const CONTEXT_PROFILE_FORMAT = 'odysseus-context-policy';
export const CONTEXT_PROFILE_MAX_BYTES = 32768;

export function parseContextProfile(text, fields) {
  if (typeof text !== 'string' || new TextEncoder().encode(text).length > CONTEXT_PROFILE_MAX_BYTES) throw new Error('Context profile is too large. Maximum size is 32 KiB.');
  let value;
  try { value = JSON.parse(text); } catch { throw new Error('Context profile must contain valid JSON.'); }
  if (!value || Array.isArray(value) || typeof value !== 'object'
    || Object.keys(value).sort().join(',') !== 'format,overrides,version'
    || value.format !== CONTEXT_PROFILE_FORMAT || value.version !== 1) throw new Error('Unsupported context profile format or version. No settings were changed.');
  if (!value.overrides || Array.isArray(value.overrides) || typeof value.overrides !== 'object') throw new Error('Context profile overrides must be an object.');
  const specs = new Map(fields.map(spec => [spec[0], spec]));
  for (const [key, item] of Object.entries(value.overrides)) {
    const spec = specs.get(key);
    if (!spec) throw new Error('Context profile contains unknown fields. No settings were changed.');
    if (typeof spec[2] === 'boolean' ? typeof item !== 'boolean'
      : !Number.isInteger(item) || item < spec[3] || item > spec[4]) throw new Error('Context profile contains an invalid setting value. No settings were changed.');
  }
  return value.overrides;
}

export function serializeContextProfile(overrides, fields) {
  // Validate even exports; do not silently discard unknown persisted parameters.
  const text = JSON.stringify({ format: CONTEXT_PROFILE_FORMAT, version: 1, overrides }, null, 2);
  parseContextProfile(text, fields);
  return text + '\n';
}
