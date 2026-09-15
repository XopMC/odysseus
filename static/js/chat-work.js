import { bindUiText, t } from './i18n.js';

const api = window.location.origin;
let snapshot = { plan: null, goal: null, cursor: 0 };
let sessionId = '';
let continuationPending = false;

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
  node.hidden = !draftEnabled && !plan;
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
  node.hidden = !draftEnabled && !goal;
  if (!goal) {
    el('goal-work-state').textContent = draftEnabled ? t('Waiting for a goal') : '';
    el('goal-work-objective').textContent = draftEnabled ? t('Your next message becomes the active goal.') : '';
    el('goal-work-progress').textContent = '';
    ['goal-work-pause', 'goal-work-resume', 'goal-work-cancel'].forEach(id => { if (el(id)) el(id).hidden = true; });
    if (el('goal-mode-status-toggle')) el('goal-mode-status-toggle').hidden = !draftEnabled;
    return;
  }
  el('goal-work-state').textContent = `${t(goal.status)} · ${t('attempt')} ${goal.attempt || 1}`;
  el('goal-work-objective').textContent = goal.objective || '';
  el('goal-work-progress').textContent = goal.progress || '';
  el('goal-work-pause').hidden = goal.status !== 'active';
  el('goal-work-resume').hidden = !['paused', 'waiting_user'].includes(goal.status);
  el('goal-work-cancel').hidden = !live;
  el('goal-mode-status-toggle').hidden = true;
  if (goal.status === 'completed') window.__odysseusSetGoalMode?.(false);
}

function render() { renderPlan(); renderGoal(); }

async function refresh(id = window.sessionModule?.getCurrentSessionId?.()) {
  sessionId = id || '';
  if (!sessionId) { snapshot = { plan: null, goal: null, cursor: 0 }; render(); return snapshot; }
  try { snapshot = await json(`${api}/api/chat/work/${encodeURIComponent(sessionId)}`); }
  catch (error) { if (error.message !== 'Chat not found') console.warn('[chat-work]', error); }
  render();
  return snapshot;
}

function handleEvent(event) {
  if (event?.type === 'plan_update') snapshot.plan = event.data || null;
  if (event?.type === 'goal_update') snapshot.goal = event.data || null;
  render();
}

async function mutate(kind, action) {
  const record = snapshot[kind];
  if (!sessionId || !record) return;
  try {
    snapshot[kind] = await post(`${api}/api/chat/work/${encodeURIComponent(sessionId)}/${kind}/${action}`, { expected_revision: record.revision });
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
  document.querySelectorAll('.chat-work-card strong, .chat-work-card summary, .chat-work-card button').forEach(node => {
    const source = node.textContent.trim();
    if (source) bindUiText(node, source);
  });
}

const chatWork = { bind, refresh, render, handleEvent, onRunEnded, pauseActiveGoal, continueGoal, getSnapshot: () => snapshot };
export default chatWork;
