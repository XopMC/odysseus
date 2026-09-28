export function compactionToastText(event, translate = value => value) {
  const before = event?.before_percent;
  const after = event?.after_percent;
  if (typeof before !== 'number' || typeof after !== 'number'
      || !Number.isFinite(before) || !Number.isFinite(after)
      || before <= after || after < 0) {
    return translate('Context compacted — older messages summarized');
  }
  const trigger = event?.trigger_percent;
  const threshold = typeof trigger === 'number' && Number.isFinite(trigger) && trigger > 0
    ? ` (${translate('threshold')} ${trigger}%)` : '';
  return `${translate('Auto compact')}: ≈${before}% → ≈${after}%${threshold}`;
}
