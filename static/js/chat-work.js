import { bindUiText, t, unbindUiText } from './i18n.js';
import { describeProgressHealth, describeUiLongTasks, describeBudgetWarnings, createUiLongTaskMonitor } from './runHealth.js?v=20260924budgetwarn1';

const api = window.location.origin;
let snapshot = { plan: null, goal: null, cursor: 0 };
let sessionId = '';
let continuationPending = false;
let eventTimer = null;
let eventSource = null;
let eventSourceSession = '';
let eventReconnectTimer = null;
let collapseTimer = null;
let refreshGeneration = 0;
let runHealthSnapshot = null;
let healthTimer = null;
let waitSnapshot = null;
let effectInbox = [];
let effectInboxLoaded = false;
const uiLongTasks = createUiLongTaskMonitor();

const el = id => document.getElementById(id);
const json = async (url, options = {}) => {
  const res = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = new Error(data.detail || data.error || `HTTP ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return data;
};
const post = (url, body) => json(url, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

function toast(message, error = false) {
  const ui = window.uiModule;
  (error ? ui?.showError : ui?.showToast)?.(t(message));
}

function renderPlan() {
  const node = el('plan-mode-status');
  if (!node) return;
  const draftEnabled = !!document.getElementById('plan-toggle')?.checked;
  const plan = snapshot.plan;
  node.hidden = plan?.status === 'cancelled' || (!draftEnabled && !plan);
  if (node.hidden) el('subagents-status')?.style.removeProperty('top');
  if (plan?.status === 'cancelled') {
    window.__odysseusSetPlanMode?.(false);
    return;
  }
  if (!plan) {
    el('plan-work-progress').textContent = draftEnabled ? t('Waiting for a plan') : '';
    el('plan-work-current').textContent = draftEnabled ? t('Send a message to create a read-only plan.') : '';
    el('plan-work-steps').replaceChildren();
    ['plan-work-execute', 'plan-work-edit', 'plan-work-cancel'].forEach(id => { if (el(id)) el(id).hidden = true; });
    if (el('plan-mode-status-toggle')) el('plan-mode-status-toggle').hidden = !draftEnabled;
    return;
  }
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const done = steps.filter(step => step.status === 'done').length;
  el('plan-work-progress').textContent = `${done}/${steps.length} · ${t(plan.status)}`;
  const current = steps.find(step => step.id === plan.current_step_id) || steps.find(step => step.status !== 'done');
  el('plan-work-current').textContent = current ? current.text : t('All required steps are complete.');
  const list = el('plan-work-steps'); list.replaceChildren();
  for (const step of steps) {
    const li = document.createElement('li'); li.className = step.status || 'pending';
    li.textContent = `${step.status === 'done' ? '✓ ' : step.status === 'blocked' ? '! ' : ''}${step.text}`;
    list.appendChild(li);
  }
  el('plan-work-execute').hidden = !['draft', 'approved', 'executing'].includes(plan.status);
  el('plan-work-execute').textContent = plan.status === 'executing' ? t('Continue') : t('Execute');
  el('plan-work-edit').hidden = ['executing', 'done', 'cancelled'].includes(plan.status);
  el('plan-work-cancel').hidden = ['done', 'cancelled'].includes(plan.status);
  el('plan-mode-status-toggle').hidden = true;
}

function renderGoal() {
  const node = el('goal-mode-status');
  if (!node) return;
  const draftEnabled = !!window.__odysseusGoalModeActive?.();
  const goal = snapshot.goal;
  const warning = describeProgressHealth(goal, runHealthSnapshot);
  if (goal?.status === 'active' && document.visibilityState !== 'hidden') uiLongTasks.start();
  else uiLongTasks.stop();
  const uiLag = describeUiLongTasks(goal, uiLongTasks.snapshot());
  const uiLagNode = el('goal-work-ui-lag');
  if (uiLagNode) {
    uiLagNode.hidden = !uiLag;
    if (!uiLagNode.hidden) {
      const label = `${t('UI long tasks')}: ${uiLag.count}, ${t('maximum')} ${uiLag.maxDurationMs} ms`;
      uiLagNode.title = label;
      uiLagNode.setAttribute('aria-label', label);
    }
  }
  const budgetWarnings = describeBudgetWarnings(goal, runHealthSnapshot);
  const budgetWarningIcon = el('goal-work-budget-warning-indicator');
  if (budgetWarningIcon) {
    const label = t('Resource budget approaching');
    budgetWarningIcon.hidden = budgetWarnings.length === 0;
    budgetWarningIcon.title = label;
    budgetWarningIcon.setAttribute('aria-label', label);
  }
  const budgetWarningNode = el('goal-work-budget-warning');
  if (budgetWarningNode) {
    budgetWarningNode.hidden = budgetWarnings.length === 0;
    budgetWarningNode.textContent = budgetWarnings.map(warning => {
      const resource = t({
        model_rounds: 'model rounds', model_tokens: 'model tokens',
        model_requests: 'model requests', wall_seconds: 'elapsed time',
        tool_calls: 'tool calls', children: 'subagents',
      }[warning.resource]);
      return `${t('Resource budget approaching')}: ${resource} ${warning.used}/${warning.limit} (${t('soft threshold')} ${warning.soft_limit})`;
    }).join(' · ');
  }
  const indicator = el('goal-work-health-indicator');
  const detail = el('goal-work-health-detail');
  const message = warning
    ? `${t('No verified progress for')} ${warning.minutes} ${t('minutes')}. ${t(warning.heartbeatAlive ? 'Connection alive; this is not task progress.' : 'No recent connection heartbeat.')}${warning.trackingCapacityExhausted ? ` ${t('Progress tracking capacity reached; manual review is needed.')}` : ''}`
    : '';
  if (indicator) {
    indicator.hidden = !warning;
    indicator.title = message;
    indicator.setAttribute('aria-label', message || t('No verified progress'));
  }
  if (detail) { detail.hidden = !warning; detail.textContent = message; }
  const live = goal && !['completed', 'cancelled'].includes(goal.status);
  // A cancelled goal remains in the durable audit log, but it is no longer
  // active UI state and must disappear after cancel, reload, or reconnect.
  node.hidden = goal?.status === 'cancelled' || (!draftEnabled && !goal);
  if (goal?.status === 'cancelled') {
    window.__odysseusSetGoalMode?.(false);
    return;
  }
  if (!goal) {
    const state = el('goal-work-state');
    const objective = el('goal-work-objective');
    const preview = el('goal-work-objective-preview');
    if (draftEnabled) {
      bindUiText(state, 'Waiting for a goal');
      bindUiText(objective, 'Your next message becomes the active goal.');
    } else {
      unbindUiText(state); unbindUiText(objective);
      state.textContent = '';
      if (objective) objective.value = '';
    }
    if (preview) preview.textContent = draftEnabled ? t('Goal pending') : '';
    el('goal-work-progress').textContent = '';
    ['goal-work-pause', 'goal-work-resume', 'goal-work-cancel', 'goal-work-quick-pause', 'goal-work-quick-resume', 'goal-work-quick-cancel'].forEach(id => { if (el(id)) el(id).hidden = true; });
    if (el('goal-mode-status-toggle')) el('goal-mode-status-toggle').hidden = !draftEnabled;
    return;
  }
  unbindUiText(el('goal-work-state'));
  unbindUiText(el('goal-work-objective'));
  const preview = el('goal-work-objective-preview');
  if (preview) {
    preview.textContent = goal.objective || '';
    preview.title = goal.objective || '';
  }
  const needsReview = goal.status === 'review_required'
    || (goal.status === 'waiting_user' && goal.checkpoint?._wait_reason === 'provider_failure');
  el('goal-work-state').textContent = `${t(needsReview ? 'Review required' : goal.status)} · ${t('attempt')} ${goal.attempt || 1}`;
  el('goal-work-objective').value = goal.objective || '';
  el('goal-work-progress').textContent = goal.progress || '';
  const effectFence = goal.status === 'waiting_user'
    && goal.checkpoint?._wait_reason === 'unknown_side_effect'
    && (!effectInboxLoaded || effectInbox.some(effect =>
      ['unknown', 'verified_not_applied'].includes(effect.status)));
  el('goal-work-pause').hidden = goal.status !== 'active';
  el('goal-work-resume').hidden = effectFence || !['paused', 'waiting_user', 'review_required'].includes(goal.status);
  el('goal-work-cancel').hidden = !live;
  el('goal-work-quick-pause').hidden = goal.status !== 'active';
  el('goal-work-quick-resume').hidden = effectFence || !['paused', 'waiting_user', 'review_required'].includes(goal.status);
  el('goal-work-quick-cancel').hidden = !live;
  el('goal-mode-status-toggle').hidden = true;
  if (goal.status === 'completed') window.__odysseusSetGoalMode?.(false);
}

function renderEffectInbox() {
  const node = el('wait-unknown-effects');
  if (!node) return;
  node.hidden = waitSnapshot?.wait_reason !== 'unknown_side_effect' && !effectInbox.length;
  node.replaceChildren();
  if (node.hidden) return;
  const heading = document.createElement('strong');
  heading.textContent = t('Unknown tool effects');
  node.appendChild(heading);
  if (!effectInboxLoaded) {
    const note = document.createElement('span');
    note.textContent = t('Effect inbox unavailable; refresh before continuing.');
    node.appendChild(note);
    return;
  }
  if (!effectInbox.length) {
    const note = document.createElement('span');
    note.textContent = t('No unresolved effects. You may resume the goal explicitly.');
    node.appendChild(note);
    return;
  }
  for (const effect of effectInbox) {
    const row = document.createElement('div'); row.className = 'wait-effect-entry';
    const label = document.createElement('span');
    label.textContent = `${effect.tool_name || 'tool'} · ${t(`Effect ${effect.status || 'unknown'}`)} · ${effect.run_id || '—'} · ${effect.tool_call_id || '—'}`;
    label.title = `${effect.action_hash || ''}`;
    row.appendChild(label);
    const addAction = (action, text) => {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = t(text);
      button.dataset.intentId = effect.id;
      button.dataset.effectAction = action;
      row.appendChild(button);
    };
    if (effect.status === 'unknown') addAction('verify', 'Verify effect');
    if (effect.status === 'verified_not_applied') addAction('authorize-retry', 'Authorize one exact retry');
    addAction('no-retry', effect.status === 'retry_authorized' ? 'Revoke retry authorization' : 'Do not retry');
    node.appendChild(row);
  }
}

function renderWait() {
  const node = el('wait-mode-status');
  if (!node) return;
  const state = waitSnapshot || {};
  const activeGoal = state?.goal_status && !['completed', 'cancelled'].includes(state.goal_status);
  const activeRun = state?.run_id && ['running', 'interrupted', 'stopping'].includes(state.run_status);
  node.hidden = !sessionId || !(activeRun || state?.current_child || activeGoal || state?.error || effectInbox.length);
  if (node.hidden) {
    node.classList?.remove('expanded');
    node.querySelector?.('.chat-work-card-toggle')?.setAttribute('aria-expanded', 'false');
    return;
  }
  const put = (id, value) => { const target = el(id); if (target) target.textContent = value == null || value === '' ? '—' : String(value); };
  put('wait-phase', t(state.phase === 'review' ? 'Review required' : state.phase || 'unavailable'));
  put('wait-duration', `${Math.max(0, Math.round(Number(state.phase_seconds) || 0))} ${t('seconds')}`);
  put('wait-run-id', state.run_id);
  put('wait-child-id', state.current_child?.child_id);
  put('wait-model', state.current_child?.model || state.model);
  const actualEndpoint = state.current_child?.endpoint_id || state.endpoint_label || state.endpoint_id;
  put('wait-endpoint', actualEndpoint || (state.selected_endpoint_label
    ? `${t('Selected now')}: ${state.selected_endpoint_label}` : null));
  put('wait-tool', state.tool);
  put('wait-lease', state.lease?.held ? `${t('Held until')} ${state.lease.expires_at || '—'}` : t('No active lease'));
  const checkpoint = state.checkpoint || {};
  put('wait-checkpoint', `seq ${Number.isInteger(checkpoint.durable_seq) ? checkpoint.durable_seq : '—'} · rev ${Number.isInteger(checkpoint.context_revision) ? checkpoint.context_revision : '—'} · ${checkpoint.ledger_hash || '—'}`);
  put('wait-recovery', state.wait_reason === 'repeated_premature_stop'
    ? t('Goal stalled after repeated responses; review and resume.')
    : state.wait_reason === 'repeated_action_observation'
      ? t('Repeated tool evidence cycle detected; review the blocker before resuming.')
    : state.wait_reason === 'provider_failure'
      ? t('Model endpoint failed repeatedly; check it before resuming the goal.')
      : state.wait_reason === 'context_compaction'
        ? `${t('Context checkpoint failed; check the summarizer or policy before resuming.')} ${state.failure_code ? `[${state.failure_code}]` : ''}`.trim()
      : state.wait_reason === 'dispatch_failure'
        ? t('Goal continuation did not start; check the endpoint and retry explicitly.')
      : state.wait_reason === 'resource_budget'
        ? `${t(state.budget?.resource === 'model_rounds' ? 'Model-round budget reached:' : state.budget?.resource === 'model_tokens' ? 'Model-token budget reached:' : state.budget?.resource === 'model_requests' ? 'Model-request budget reached:' : state.budget?.resource === 'wall_seconds' ? 'Wall-time budget reached:' : state.budget?.resource === 'children' ? 'Child-agent budget reached:' : 'Tool-call budget reached:')} ${Number(state.budget?.used) || 0}/${Number(state.budget?.limit) || 0}. ${state.budget?.resource === 'model_tokens' && state.budget?.usage_source && state.budget.usage_source !== 'real' ? `${t('Estimated usage')}. ` : ''}${t('Review the limit before resuming.')}`
      : state.wait_reason === 'unknown_side_effect'
        ? t(Number(state.unknown_effect_count) > 0
          ? 'A tool outcome is unknown. Verify its outcome or forbid a repeat.'
          : Number(state.blocking_effect_count) > 0
            ? 'The effect was verified as not applied. Authorize one exact retry or forbid a repeat before resuming.'
            : Number(state.pending_effect_count) > 0
              ? 'One exact retry is authorized. Resume explicitly; only the matching action can consume it.'
              : 'No unresolved effects. You may resume the goal explicitly.')
      : t(`Recovery: ${state.recovery_action || 'none'}`));
  renderEffectInbox();
  const action = el('wait-action');
  if (action) {
    action.hidden = !['answer', 'resume_goal', 'reconnect', 'inspect', 'inspect_effect', 'inspect_context'].includes(state.recovery_action);
    action.textContent = t(`Action: ${state.recovery_action || 'none'}`);
  }
}

function render() { renderPlan(); renderGoal(); renderWait(); }

async function refreshWait(targetSession = sessionId) {
  if (!targetSession) { waitSnapshot = null; renderWait(); return; }
  const generation = refreshGeneration;
  try {
    const state = await json(`${api}/api/chat/work/${encodeURIComponent(targetSession)}/why-waiting`);
    if (sessionId !== targetSession || generation !== refreshGeneration) return;
    waitSnapshot = state;
  } catch (_) {
    if (sessionId !== targetSession || generation !== refreshGeneration) return;
    waitSnapshot = { phase: 'unavailable', recovery_action: 'none', error: true,
      goal_status: snapshot.goal?.status || null };
  }
  renderWait();
}

async function refreshEffects(targetSession = sessionId) {
  if (!targetSession) { effectInbox = []; effectInboxLoaded = false; render(); return; }
  const generation = refreshGeneration;
  try {
    const data = await json(`${api}/api/chat/work/${encodeURIComponent(targetSession)}/unknown-effects`);
    if (sessionId !== targetSession || generation !== refreshGeneration) return;
    effectInbox = Array.isArray(data.effects) ? data.effects : [];
    effectInboxLoaded = true;
  } catch (_) {
    if (sessionId !== targetSession || generation !== refreshGeneration) return;
    effectInbox = []; effectInboxLoaded = false;
  }
  render();
}

async function refreshRunHealth(targetSession = sessionId) {
  if (!targetSession || snapshot.goal?.status !== 'active') {
    runHealthSnapshot = null;
    renderGoal();
    return;
  }
  const generation = refreshGeneration;
  try {
    const run = await json(`${api}/api/chat/run/${encodeURIComponent(targetSession)}`);
    if (sessionId !== targetSession || generation !== refreshGeneration) return;
    runHealthSnapshot = run;
  } catch (_) {
    if (sessionId !== targetSession || generation !== refreshGeneration) return;
    runHealthSnapshot = null;
  }
  renderGoal();
}

async function refresh(id = window.sessionModule?.getCurrentSessionId?.()) {
  const targetSession = id || '';
  const myGeneration = ++refreshGeneration;
  const switched = targetSession !== sessionId;
  sessionId = targetSession;
  if (switched) {
    closeEventStream();
    snapshot = { plan: null, goal: null, cursor: 0 };
    runHealthSnapshot = null;
    waitSnapshot = null;
    effectInbox = []; effectInboxLoaded = false;
    render();
  }
  if (!targetSession) return snapshot;
  try {
    const next = await json(`${api}/api/chat/work/${encodeURIComponent(targetSession)}`);
    if (myGeneration !== refreshGeneration || sessionId !== targetSession) return snapshot;
    snapshot = next;
  } catch (error) {
    if (myGeneration !== refreshGeneration || sessionId !== targetSession) return snapshot;
    if (error.message !== 'Chat not found') console.warn('[chat-work]', error);
  }
  if (myGeneration !== refreshGeneration || sessionId !== targetSession) return snapshot;
  render();
  connectEventStream();
  void refreshRunHealth(targetSession);
  void refreshWait(targetSession);
  void refreshEffects(targetSession);
  return snapshot;
}

function handleEvent(event) {
  if (event?.type === 'effect_unknown' || event?.type === 'effect_reconciled') {
    void refreshEffects(sessionId); void refreshWait(sessionId); return;
  }
  if (event?.type === 'budget_warning') { void refreshRunHealth(sessionId); return; }
  if (event?.type === 'plan_update') { snapshot.plan = event.data || null; render(); return; }
  if (event?.type === 'goal_update') { snapshot.goal = event.data || null; runHealthSnapshot = null; render(); void refreshRunHealth(sessionId); void refreshWait(sessionId); void refreshEffects(sessionId); return; }
  if (event?.type === 'goal_guidance') {
    if (event.data?.goal) snapshot.goal = event.data.goal;
    window.chatModule?.appendGoalGuidance?.(event.data?.guidance);
    window.sessionModule?.refreshSessionMessageCount?.(sessionId);
    render(); return;
  }
  if (event?.type?.startsWith('plan_') || event?.type?.startsWith('goal_')) void refresh(sessionId);
}

export function mayPreviewNewGoal(goal) {
  return !goal || ['completed', 'cancelled'].includes(goal.status);
}

function beginGoal(objective) {
  const text = String(objective || '').trim();
  if (!text || !mayPreviewNewGoal(snapshot.goal)) return;
  // Show the submitted objective immediately. The durable goal_update from
  // the server replaces this provisional record as soon as the run starts.
  snapshot.goal = { objective: text, status: 'starting', attempt: 1, progress: '' };
  runHealthSnapshot = null;
  waitSnapshot = null;
  render();
}

function prepareNewPlan() {
  if (['done', 'cancelled'].includes(snapshot.plan?.status)) snapshot.plan = null;
  renderPlan();
}

function prepareNewGoal() {
  // Terminal rows remain durable for audit, but they must not immediately
  // switch off a newly selected Goal mode.  The next submitted objective will
  // replace the terminal row server-side via ensure_goal().
  if (['completed', 'cancelled'].includes(snapshot.goal?.status)) snapshot.goal = null;
  renderGoal();
}

async function mutate(kind, action) {
  const record = snapshot[kind];
  if (!sessionId || !record) return;
  if (kind === 'plan' && action === 'execute' && record.status === 'executing') {
    // A model can exhaust its run budget while a durable plan remains in
    // progress. Do not POST /execute again (that transition already happened),
    // and never replace a run still generating in another browser.
    try {
      if (window.chatModule?.hasActiveStream?.(sessionId)) {
        toast(t('A run is already active.'));
        return;
      }
      const status = await fetch(`${api}/api/chat/stream_status/${encodeURIComponent(sessionId)}`, {
        credentials: 'same-origin', cache: 'no-store',
      });
      if (status.ok) { toast(t('A run is already active.')); return; }
      if (status.status !== 404) throw new Error(`Run status HTTP ${status.status}`);
      const input = el('message');
      if (!input) return;
      input.value = t('Continue the current approved plan. Use only its latest durable steps and update each step after verification.');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      el('chat-form')?.requestSubmit?.();
    } catch (error) { toast(error.message, true); }
    return;
  }
  try {
    snapshot[kind] = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/${kind}/${action}`, { expected_revision: record.revision });
    if (kind === 'goal') { runHealthSnapshot = null; void refreshWait(sessionId); }
    if (kind === 'plan' && action === 'cancel') { snapshot.plan = null; window.__odysseusSetPlanMode?.(false); }
    if (kind === 'goal' && action === 'cancel') { snapshot.goal = null; window.__odysseusSetGoalMode?.(false); }
    render();
    if (kind === 'goal' && action === 'pause') window.refreshChatContextHeader?.('goal-paused');
    if (kind === 'plan' && action === 'execute') {
      window.__odysseusSetPlanMode?.(false);
      const input = el('message');
      if (input) { input.value = t('Execute the approved plan and update each step after verification.'); input.dispatchEvent(new Event('input', { bubbles: true })); el('chat-form')?.requestSubmit?.(); }
    }
  } catch (error) {
    await refresh(sessionId);
    const desiredStatus = { pause: 'paused', resume: 'active', cancel: 'cancelled' }[action];
    const current = snapshot[kind];
    if (kind === 'goal' && error.status === 409 && desiredStatus
        && current?.id === record.id && current.status === desiredStatus) return;
    if (kind === 'goal' && action === 'resume' && error.status === 409) {
      await refreshEffects(sessionId);
      if (effectInboxLoaded && effectInbox.some(effect =>
        ['unknown', 'verified_not_applied'].includes(effect.status))) {
        await refreshWait(sessionId);
        openEffectRecovery();
        toast(t('Review the tool effect before resuming.'), true);
        return;
      }
    }
    toast(error.message, true);
  }
}

async function continueGoal() {
  if (continuationPending || !sessionId || snapshot.goal?.status !== 'active') return false;
  if (window.chatModule?.hasActiveStream?.(sessionId)) return false;
  continuationPending = true;
  try {
    const lease = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/goal-lease`, {});
    const fd = new FormData();
    fd.append('session', sessionId);
    fd.append('message', 'Continue the active goal from its durable checkpoint. Change approach after repeated failures; complete it only with verified evidence.');
    fd.append('mode', 'agent'); fd.append('goal_continuation', 'true'); fd.append('goal_lease_token', lease.lease_token);
    fd.append('access_mode', window.accessModeModule?.getMode?.() || 'ask_important');
    fd.append('allow_bash', el('bash-toggle')?.checked ? 'true' : 'false');
    fd.append('allow_web_search', el('web-toggle')?.checked ? 'true' : 'false');
    const res = await fetch(`${api}/api/chat_stream`, { method: 'POST', body: fd, credentials: 'same-origin' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    await res.body?.cancel?.();
    setTimeout(() => window.chatModule?.resumeStream?.(sessionId), 80);
    return true;
  } catch (error) {
    if (!/not ready|still running/i.test(error.message)) toast(error.message, true);
    return false;
  } finally { continuationPending = false; await refresh(sessionId); }
}

async function onRunEnded(id) {
  await refresh(id);
  // Reconcile the header from the owner-scoped database count, not the
  // number of transient DOM bubbles (which can differ on two devices during
  // replay). This keeps desktop/mobile badges identical after completion.
  await window.sessionModule?.refreshSessionMessageCount?.(id);
  // The durable server controller owns automatic continuation. A browser may
  // disappear at this boundary, so UI completion must never be the scheduler.
}

async function pauseActiveGoal() {
  if (snapshot.goal?.status === 'active') await mutate('goal', 'pause');
}

async function addGuidance(message) {
  const text = String(message || '').trim();
  // The server owns Goal state.  A reconnecting browser may still have a
  // paused snapshot while the controller has already resumed it; let the
  // owner-scoped endpoint decide instead of dropping into the Stop path.
  if (!sessionId || !text) return false;
  const result = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/goal-guidance`, { message: text });
  if (result?.goal) snapshot.goal = result.goal;
  window.chatModule?.appendGoalGuidance?.(result?.guidance);
  render();
  return true;
}

function armCollapse(node) {
  clearTimeout(collapseTimer);
  // Recovery decisions must remain visible until the owner chooses one.
  // The ordinary four-second floating-card timeout is too short for an
  // unknown side effect and made Goal resume look permanently broken.
  if (node.id === 'wait-mode-status' && effectInbox.length) return;
  collapseTimer = setTimeout(() => {
    node.classList.remove('expanded');
    node.querySelector('.chat-work-card-toggle')?.setAttribute('aria-expanded', 'false');
    if (node.id === 'plan-mode-status') {
      el('subagents-status')?.style.removeProperty('top');
    }
  }, 4000);
}

function openEffectRecovery() {
  const node = el('wait-mode-status');
  node?.classList?.add?.('expanded');
  node?.querySelector?.('.chat-work-card-toggle')?.setAttribute?.('aria-expanded', 'true');
  clearTimeout(collapseTimer);
  node?.scrollIntoView?.({ block: 'nearest' });
  el('wait-unknown-effects')?.querySelector?.('button')?.focus?.();
}

function placeSubagentsBelowPlan(plan, expanded) {
  const subagents = el('subagents-status');
  if (!subagents) return;
  if (!expanded) {
    subagents.style.removeProperty('top');
    return;
  }
  requestAnimationFrame(() => {
    if (!plan.classList.contains('expanded') || subagents.hidden) return;
    const bottom = plan.getBoundingClientRect().bottom;
    subagents.style.top = `${Math.ceil(bottom + 8)}px`;
  });
}

async function pollEvents() {
  if (!sessionId || document.visibilityState === 'hidden') return;
  try {
    const data = await json(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/events?after=${Number(snapshot.cursor || 0)}&limit=100`);
    for (const event of data.events || []) handleEvent(event);
    snapshot.cursor = Number(data.next_cursor || snapshot.cursor || 0);
  } catch (_) { /* reconnect on next tick/focus */ }
}

function closeEventStream() {
  clearTimeout(eventReconnectTimer); eventReconnectTimer = null;
  eventSource?.close?.(); eventSource = null; eventSourceSession = '';
}

function connectEventStream() {
  if (!sessionId || document.visibilityState === 'hidden' || typeof EventSource === 'undefined') return;
  if (eventSource && eventSourceSession === sessionId) return;
  closeEventStream();
  const targetSession = sessionId;
  const source = new EventSource(`${api}/api/chat/work/${encodeURIComponent(targetSession)}/events/stream?after=${Number(snapshot.cursor || 0)}`);
  eventSource = source; eventSourceSession = targetSession;
  source.onmessage = message => {
    if (targetSession !== sessionId || source !== eventSource) return;
    try {
      const event = JSON.parse(message.data);
      if (Number(event.seq || 0) <= Number(snapshot.cursor || 0)) return;
      handleEvent(event); snapshot.cursor = Number(event.seq);
    } catch (_) { /* malformed events never mutate the current snapshot */ }
  };
  source.onerror = () => {
    if (source !== eventSource) return;
    closeEventStream();
    eventReconnectTimer = setTimeout(async () => { await pollEvents(); connectEventStream(); }, 1200);
  };
}

async function reviseGoal() {
  const goal = snapshot.goal;
  const objective = el('goal-work-objective')?.value?.trim();
  if (!goal || !objective || objective === goal.objective) return;
  try {
    snapshot.goal = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/goal-revise`, {
      objective, expected_revision: goal.revision,
      run_id: window.chatModule?.getActiveRunId?.(sessionId) || '',
    });
    render();
  } catch (error) { toast(error.message, true); await refresh(sessionId); }
}

async function runWaitAction() {
  const action = waitSnapshot?.recovery_action;
  if (!sessionId || !action) return;
  if (action === 'resume_goal') {
    if (['paused', 'waiting_user', 'review_required'].includes(snapshot.goal?.status)) await mutate('goal', 'resume');
    return;
  }
  if (action === 'reconnect') {
    const attached = await window.chatModule?.resumeStream?.(sessionId);
    if (!attached) window.location.reload?.();
    await refreshWait(sessionId);
    return;
  }
  if (action === 'inspect_effect') {
    await refreshEffects(sessionId);
    openEffectRecovery();
    return;
  }
  if (action === 'inspect_context') {
    const pill = el('chat-context-pill');
    if (pill && !pill.hidden) pill.click?.();
    else {
      const label = el('wait-recovery');
      if (label) label.textContent = t('Context settings unavailable; reload chat.');
    }
    return;
  }
  const selector = action === 'answer' ? '.ask-user-card' : action === 'inspect' ? '.agent-thread-node' : null;
  if (!selector) return;
  const cards = document.querySelectorAll?.(selector) || [];
  const target = cards[cards.length - 1];
  if (target) target.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
  else {
    const message = action === 'answer' ? 'Question card unavailable; reload chat.' : 'Latest event unavailable; reload chat.';
    const label = el('wait-recovery');
    if (label) label.textContent = t(message);
  }
}

async function chooseNoRetry(effect) {
  if (!effect?.id || !sessionId) return;
  const confirmation = effect.status === 'retry_authorized'
    ? 'Revoke the one-shot retry authorization? No action will run.'
    : effect.status === 'verified_not_applied'
      ? 'Do not retry this action even though it was verified as not applied?'
      : 'Do not retry this tool action? This does not verify whether it already happened.';
  if (!window.confirm(t(confirmation))) return;
  const targetSession = sessionId;
  try {
    await post(`${api}/api/chat/work/${encodeURIComponent(targetSession)}/unknown-effects/${encodeURIComponent(effect.id)}/no-retry`, {
      expected_revision: effect.revision,
    });
    if (targetSession !== sessionId) return;
    toast('Effect marked no-retry. No action was replayed.');
    await refreshEffects(targetSession);
    await refreshWait(targetSession);
  } catch (error) {
    toast(error.message, true);
    await refreshEffects(targetSession);
  }
}

async function verifyEffect(effect) {
  if (!effect?.id || !sessionId || effect.status !== 'unknown') return;
  const evidence = window.prompt?.(t('Briefly describe how you checked the external effect. Only a SHA-256 digest is stored.'));
  if (!evidence?.trim()) return;
  const outcome = window.confirm(t('Did the side effect occur? Choose OK for yes, Cancel for no.'))
    ? 'applied' : 'not_applied';
  const targetSession = sessionId;
  try {
    await post(`${api}/api/chat/work/${encodeURIComponent(targetSession)}/unknown-effects/${encodeURIComponent(effect.id)}/verify`, {
      expected_revision: effect.revision, outcome, evidence,
    });
    if (targetSession !== sessionId) return;
    toast('Verification receipt recorded. No action was replayed.');
    await refreshEffects(targetSession);
    await refreshWait(targetSession);
  } catch (error) {
    toast(error.message, true);
    await refreshEffects(targetSession);
  }
}

async function authorizeEffectRetry(effect) {
  if (!effect?.id || !sessionId || effect.status !== 'verified_not_applied') return;
  if (!window.confirm(t('You verified the effect did not occur. Authorize one future tool call only if its action hash matches exactly. This does not replay the saved payload.'))) return;
  const targetSession = sessionId;
  try {
    await post(`${api}/api/chat/work/${encodeURIComponent(targetSession)}/unknown-effects/${encodeURIComponent(effect.id)}/authorize-retry`, {
      expected_revision: effect.revision,
    });
    if (targetSession !== sessionId) return;
    toast('One exact-hash retry was authorized; no action ran now.');
    await refreshEffects(targetSession);
    await refreshWait(targetSession);
  } catch (error) {
    toast(error.message, true);
    await refreshEffects(targetSession);
  }
}

function bind() {
  for (const id of ['plan-run-inspector', 'goal-run-inspector', 'wait-run-inspector']) {
    el(id)?.addEventListener('click', () => document.dispatchEvent(new CustomEvent('odysseus:run-inspector', {
      detail: {
        runId: waitSnapshot?.run_id || window.chatModule?.getActiveRunId?.(sessionId) || '',
        eventSeq: Number.isSafeInteger(waitSnapshot?.checkpoint?.durable_seq)
          ? waitSnapshot.checkpoint.durable_seq : null,
      },
    })));
  }
  el('plan-work-execute')?.addEventListener('click', () => mutate('plan', 'execute'));
  el('plan-work-cancel')?.addEventListener('click', () => mutate('plan', 'cancel'));
  el('plan-work-edit')?.addEventListener('click', async () => {
    const plan = snapshot.plan; if (!plan) return;
    const value = prompt(t('Edit plan'), plan.steps.map(step => `- [${step.status === 'done' ? 'x' : ' '}] ${step.text}`).join('\n'));
    if (value == null) return;
    try { snapshot.plan = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/plan`, { title: plan.title, steps: value, expected_revision: plan.revision }); render(); }
    catch (error) { toast(error.message, true); await refresh(); }
  });
  el('goal-work-pause')?.addEventListener('click', () => mutate('goal', 'pause'));
  el('goal-work-resume')?.addEventListener('click', () => mutate('goal', 'resume'));
  el('goal-work-cancel')?.addEventListener('click', () => mutate('goal', 'cancel'));
  el('goal-work-quick-pause')?.addEventListener('click', () => mutate('goal', 'pause'));
  el('goal-work-quick-resume')?.addEventListener('click', () => mutate('goal', 'resume'));
  el('goal-work-quick-cancel')?.addEventListener('click', () => mutate('goal', 'cancel'));
  el('goal-work-save')?.addEventListener('click', reviseGoal);
  el('wait-refresh')?.addEventListener('click', () => refreshWait());
  el('wait-action')?.addEventListener('click', runWaitAction);
  el('wait-unknown-effects')?.addEventListener('click', event => {
    const button = event.target?.closest?.('button[data-intent-id]');
    const id = button?.dataset.intentId;
    if (!id) return;
    const effect = effectInbox.find(item => item.id === id);
    if (!effect) return;
    const action = button.dataset.effectAction;
    if (action === 'verify') void verifyEffect(effect);
    else if (action === 'authorize-retry') void authorizeEffectRetry(effect);
    else if (action === 'no-retry') void chooseNoRetry(effect);
  });
  document.querySelectorAll('#plan-mode-status, #goal-mode-status, #wait-mode-status').forEach(node => {
    const toggle = node.querySelector('.chat-work-card-toggle');
    toggle?.addEventListener('click', () => {
      const open = node.classList.toggle('expanded');
      toggle.setAttribute('aria-expanded', String(open));
      if (node.id === 'plan-mode-status') placeSubagentsBelowPlan(node, open);
      if (open) {
        const subagents = document.getElementById('subagents-status');
        subagents?.classList.remove('expanded');
        subagents?.querySelector('#subagents-toggle')?.setAttribute('aria-expanded', 'false');
        const other = el(node.id === 'wait-mode-status' ? 'plan-mode-status' : 'wait-mode-status');
        other?.classList.remove('expanded');
        other?.querySelector('.chat-work-card-toggle')?.setAttribute('aria-expanded', 'false');
        if (node.id === 'wait-mode-status') subagents?.style.removeProperty('top');
        armCollapse(node);
      }
    });
    ['pointermove', 'focusin', 'input'].forEach(type => node.addEventListener(type, () => {
      if (node.classList.contains('expanded')) armCollapse(node);
    }));
  });
  document.querySelectorAll('#plan-mode-status strong, #plan-mode-status summary, #plan-mode-status button, #goal-mode-status strong, #goal-mode-status summary, #goal-mode-status button, #wait-mode-status strong, #wait-mode-status button, #wait-mode-status .wait-grid > span:nth-child(odd)').forEach(node => {
    const source = node.textContent.trim();
    if (source) bindUiText(node, source);
  });
  for (const [id, label] of [['goal-work-quick-pause', 'Pause goal'], ['goal-work-quick-resume', 'Resume goal'], ['goal-work-quick-cancel', 'Delete goal']]) {
    bindUiText(el(id), label, 'aria-label'); bindUiText(el(id), label, 'title');
  }
  bindUiText(el('wait-toggle'), 'Why is the agent waiting?', 'aria-label');
  bindUiText(el('wait-toggle'), 'Why is the agent waiting?', 'title');
  if (typeof EventSource === 'undefined' && !eventTimer) eventTimer = setInterval(pollEvents, 1200);
  if (!healthTimer) healthTimer = setInterval(() => {
    if (document.visibilityState !== 'hidden') {
      void refreshRunHealth();
      if (snapshot.goal?.status === 'active' || ['running', 'interrupted'].includes(waitSnapshot?.run_status) || window.chatModule?.hasActiveStream?.(sessionId)) void refreshWait();
      if (waitSnapshot?.wait_reason === 'unknown_side_effect') void refreshEffects();
    }
  }, 15000);
  ['focus', 'online', 'pageshow'].forEach(type => window.addEventListener(type, () => { pollEvents(); connectEventStream(); void refreshRunHealth(); void refreshWait(); void refreshEffects(); }));
  window.addEventListener('odysseus:chat-busy-change', () => { void refreshWait(); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') { closeEventStream(); uiLongTasks.stop(); }
    else { renderGoal(); pollEvents(); connectEventStream(); void refreshRunHealth(); void refreshWait(); void refreshEffects(); }
  });
}

const chatWork = {
  bind, refresh, render, handleEvent, beginGoal, prepareNewPlan, prepareNewGoal,
  onRunEnded, pauseActiveGoal, addGuidance, continueGoal, refreshRunHealth,
  refreshWait, refreshEffects, runWaitAction, chooseNoRetry, verifyEffect,
  authorizeEffectRetry, mutate,
  getSnapshot: () => snapshot,
};
export default chatWork;
