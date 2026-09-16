import { bindUiText, t, unbindUiText } from './i18n.js';

const api = window.location.origin;
let snapshot = { plan: null, goal: null, cursor: 0 };
let sessionId = '';
let continuationPending = false;
let eventTimer = null;
let eventSource = null;
let eventSourceSession = '';
let eventReconnectTimer = null;
let collapseTimer = null;

const el = id => document.getElementById(id);
const json = async (url, options = {}) => {
  const res = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
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
  node.hidden = !draftEnabled && (!plan || plan.status === 'cancelled');
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
  el('plan-work-execute').hidden = !['draft', 'approved'].includes(plan.status);
  el('plan-work-edit').hidden = ['executing', 'done', 'cancelled'].includes(plan.status);
  el('plan-work-cancel').hidden = ['done', 'cancelled'].includes(plan.status);
  el('plan-mode-status-toggle').hidden = true;
}

function renderGoal() {
  const node = el('goal-mode-status');
  if (!node) return;
  const draftEnabled = !!window.__odysseusGoalModeActive?.();
  const goal = snapshot.goal;
  const live = goal && !['completed', 'cancelled'].includes(goal.status);
  // A cancelled goal remains in the durable audit log, but it is no longer
  // active UI state and must disappear after cancel, reload, or reconnect.
  node.hidden = !draftEnabled && (!goal || goal.status === 'cancelled');
  if (!goal) {
    const state = el('goal-work-state');
    const objective = el('goal-work-objective');
    if (draftEnabled) {
      bindUiText(state, 'Waiting for a goal');
      bindUiText(objective, 'Your next message becomes the active goal.');
    } else {
      unbindUiText(state); unbindUiText(objective);
      state.textContent = ''; objective.textContent = '';
    }
    el('goal-work-progress').textContent = '';
    ['goal-work-pause', 'goal-work-resume', 'goal-work-cancel', 'goal-work-quick-pause', 'goal-work-quick-resume', 'goal-work-quick-cancel'].forEach(id => { if (el(id)) el(id).hidden = true; });
    if (el('goal-mode-status-toggle')) el('goal-mode-status-toggle').hidden = !draftEnabled;
    return;
  }
  unbindUiText(el('goal-work-state'));
  unbindUiText(el('goal-work-objective'));
  el('goal-work-state').textContent = `${t(goal.status)} · ${t('attempt')} ${goal.attempt || 1}`;
  el('goal-work-objective').value = goal.objective || '';
  el('goal-work-progress').textContent = goal.progress || '';
  el('goal-work-pause').hidden = goal.status !== 'active';
  el('goal-work-resume').hidden = !['paused', 'waiting_user'].includes(goal.status);
  el('goal-work-cancel').hidden = !live;
  el('goal-work-quick-pause').hidden = goal.status !== 'active';
  el('goal-work-quick-resume').hidden = !['paused', 'waiting_user'].includes(goal.status);
  el('goal-work-quick-cancel').hidden = !live;
  el('goal-mode-status-toggle').hidden = true;
  if (goal.status === 'completed') window.__odysseusSetGoalMode?.(false);
}

function render() { renderPlan(); renderGoal(); }

async function refresh(id = window.sessionModule?.getCurrentSessionId?.()) {
  sessionId = id || '';
  if (!sessionId) { closeEventStream(); snapshot = { plan: null, goal: null, cursor: 0 }; render(); return snapshot; }
  try { snapshot = await json(`${api}/api/chat/work/${encodeURIComponent(sessionId)}`); }
  catch (error) { if (error.message !== 'Chat not found') console.warn('[chat-work]', error); }
  render();
  connectEventStream();
  return snapshot;
}

function handleEvent(event) {
  if (event?.type === 'plan_update') { snapshot.plan = event.data || null; render(); return; }
  if (event?.type === 'goal_update') { snapshot.goal = event.data || null; render(); return; }
  if (event?.type?.startsWith('plan_') || event?.type?.startsWith('goal_')) void refresh(sessionId);
}

function beginGoal(objective) {
  const text = String(objective || '').trim();
  if (!text) return;
  // Show the submitted objective immediately. The durable goal_update from
  // the server replaces this provisional record as soon as the run starts.
  snapshot.goal = { objective: text, status: 'starting', attempt: 1, progress: '' };
  render();
}

async function mutate(kind, action) {
  const record = snapshot[kind];
  if (!sessionId || !record) return;
  try {
    snapshot[kind] = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/${kind}/${action}`, { expected_revision: record.revision });
    if (kind === 'plan' && action === 'cancel') { snapshot.plan = null; window.__odysseusSetPlanMode?.(false); }
    if (kind === 'goal' && action === 'cancel') snapshot.goal = null;
    render();
    if (kind === 'plan' && action === 'execute') {
      window.__odysseusSetPlanMode?.(false);
      const input = el('message');
      if (input) { input.value = t('Execute the approved plan and update each step after verification.'); input.dispatchEvent(new Event('input', { bubbles: true })); el('chat-form')?.requestSubmit?.(); }
    }
    if (kind === 'goal' && action === 'resume') void continueGoal();
  } catch (error) { toast(error.message, true); await refresh(sessionId); }
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
  if (id === sessionId && snapshot.goal?.status === 'active') setTimeout(continueGoal, 300);
}

async function pauseActiveGoal() {
  if (snapshot.goal?.status === 'active') await mutate('goal', 'pause');
}

function armCollapse(node) {
  clearTimeout(collapseTimer);
  collapseTimer = setTimeout(() => {
    node.classList.remove('expanded');
    node.querySelector('.chat-work-card-toggle')?.setAttribute('aria-expanded', 'false');
  }, 4000);
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
    setTimeout(continueGoal, 350);
  } catch (error) { toast(error.message, true); await refresh(sessionId); }
}

function bind() {
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
  document.querySelectorAll('.chat-work-card').forEach(node => {
    const toggle = node.querySelector('.chat-work-card-toggle');
    toggle?.addEventListener('click', () => {
      const open = node.classList.toggle('expanded');
      toggle.setAttribute('aria-expanded', String(open));
      if (open) armCollapse(node);
    });
    ['pointermove', 'focusin', 'input'].forEach(type => node.addEventListener(type, () => {
      if (node.classList.contains('expanded')) armCollapse(node);
    }));
  });
  document.querySelectorAll('.chat-work-card strong, .chat-work-card summary, .chat-work-card button').forEach(node => {
    const source = node.textContent.trim();
    if (source) bindUiText(node, source);
  });
  for (const [id, label] of [['goal-work-quick-pause', 'Pause goal'], ['goal-work-quick-resume', 'Resume goal'], ['goal-work-quick-cancel', 'Delete goal']]) {
    bindUiText(el(id), label, 'aria-label'); bindUiText(el(id), label, 'title');
  }
  if (typeof EventSource === 'undefined' && !eventTimer) eventTimer = setInterval(pollEvents, 1200);
  ['focus', 'online', 'pageshow'].forEach(type => window.addEventListener(type, () => { pollEvents(); connectEventStream(); }));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') closeEventStream();
    else { pollEvents(); connectEventStream(); }
  });
}

const chatWork = { bind, refresh, render, handleEvent, beginGoal, onRunEnded, pauseActiveGoal, continueGoal, getSnapshot: () => snapshot };
export default chatWork;
