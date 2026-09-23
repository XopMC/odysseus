// The backend owns the progress verdict. A live SSE heartbeat is shown only
// as transport evidence; it never clears a stalled useful-progress warning.
export function describeProgressHealth(goal, run, nowMs = Date.now()) {
  if (goal?.status !== 'active' || run?.status !== 'running') return null;
  const health = run.progress_health;
  if (health?.stalled !== true) return null;
  const seconds = Number(health.seconds_without_progress);
  const minutes = Number.isFinite(seconds) ? Math.max(10, Math.floor(Math.max(0, seconds) / 60)) : 10;
  const heartbeatMs = Number(health.last_heartbeat_at) * 1000;
  const heartbeatAlive = Number.isFinite(heartbeatMs) && heartbeatMs > 0
    && heartbeatMs <= nowMs + 5000 && nowMs - heartbeatMs <= 30000;
  return {
    runId: String(run.run_id || ''),
    minutes,
    heartbeatAlive,
    lastProgressKind: typeof health.last_progress_kind === 'string' ? health.last_progress_kind : '',
    trackingCapacityExhausted: health.tracking_capacity_exhausted === true,
  };
}

export function describeUiLongTasks(goal, snapshot, {
  countLimit = 3,
  durationLimitMs = 200,
} = {}) {
  if (goal?.status !== 'active' || snapshot?.supported !== true) return null;
  const count = Number(snapshot.count);
  const maxDurationMs = Number(snapshot.max_duration_ms);
  if (!Number.isInteger(count) || count < countLimit
      || !Number.isFinite(maxDurationMs) || maxDurationMs < durationLimitMs) return null;
  return { count, maxDurationMs, countLimit, durationLimitMs };
}

export function describeBudgetWarnings(goal, run) {
  if (goal?.status !== 'active' || run?.status !== 'running') return [];
  const warnings = run.health_metrics?.budget_warnings;
  if (!Array.isArray(warnings)) return [];
  const resources = new Set([
    'model_rounds', 'model_tokens', 'model_requests', 'wall_seconds', 'tool_calls', 'children',
  ]);
  return warnings.filter(item => item && resources.has(item.resource)
    && Number.isSafeInteger(item.used) && item.used >= 0
    && Number.isSafeInteger(item.limit) && item.limit > 0
    && Number.isSafeInteger(item.soft_limit) && item.soft_limit >= 1
    && item.soft_limit <= item.limit && item.used >= item.soft_limit)
    .map(({ resource, used, limit, soft_limit }) => ({ resource, used, limit, soft_limit }));
}

// Browser-local telemetry: no task text, DOM nodes, URLs or network requests.
// Unsupported Safari versions report unavailable instead of zero long tasks.
export function createUiLongTaskMonitor(Observer = globalThis.PerformanceObserver) {
  let observer = null;
  let supported = false;
  let count = 0;
  let maxMs = 0;
  return {
    start() {
      if (observer) return true;
      if (typeof Observer !== 'function') return false;
      try {
        observer = new Observer(list => {
          for (const entry of list.getEntries()) {
            const ms = Number(entry.duration);
            if (!Number.isFinite(ms) || ms < 50) continue;
            count += 1;
            maxMs = Math.max(maxMs, ms);
          }
        });
        observer.observe({ entryTypes: ['longtask'] });
        supported = true;
        return true;
      } catch (_) {
        observer?.disconnect?.();
        observer = null;
        return false;
      }
    },
    stop() { observer?.disconnect?.(); observer = null; },
    reset() { count = 0; maxMs = 0; },
    snapshot() { return { supported, count, max_duration_ms: Math.round(maxMs) }; },
  };
}
