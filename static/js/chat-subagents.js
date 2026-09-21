import { t } from './i18n.js';

const api = window.location.origin;
const activeStates = new Set(['queued', 'running', 'waiting_user', 'stopping']);
let sessionId = '';
let rows = [];
let cursor = 0;
let source = null;
let selectedId = '';
let collapseTimer = null;
let refreshTimer = null;
let detailTimer = null;
let generation = 0;

const el = id => document.getElementById(id);
async function json(url, options = {}) {
  const res = await fetch(url, { credentials:'same-origin', cache:'no-store', ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}
const post = (url, body={}) => json(url, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });

function armCollapse() {
  clearTimeout(collapseTimer);
  collapseTimer = setTimeout(() => {
    const card = el('subagents-status');
    if (!card?.matches(':hover') && !card?.contains(document.activeElement)) {
      card?.classList.remove('expanded');
      el('subagents-toggle')?.setAttribute('aria-expanded', 'false');
    }
  }, 4000);
}

function render() {
  const card = el('subagents-status');
  if (!card) return;
  card.hidden = !sessionId || rows.length === 0;
  const active = rows.filter(row => activeStates.has(row.status)).length;
  el('subagents-badge').textContent = String(active || rows.length);
  el('subagents-summary').textContent = `${active} ${t('subagents active')} · ${rows.length} ${t('subagents total')}`;
  const list = el('subagents-list');
  list.replaceChildren();
  for (const row of rows) {
    const item = document.createElement('div'); item.className = 'subagent-row'; item.dataset.childId = row.child_id;
    const main = document.createElement('div'); main.className = 'subagent-main';
    const name = document.createElement('div'); name.className = 'subagent-name'; name.textContent = row.name || `Subagent ${row.ordinal || ''}`;
    const model = document.createElement('div'); model.className = 'subagent-model'; model.textContent = row.model || '';
    const objective = document.createElement('div'); objective.className = 'subagent-objective'; objective.textContent = row.objective || '';
    const status = document.createElement('span'); status.className = `subagent-status ${row.status}`; status.textContent = t(`Subagent ${row.status}`);
    main.append(name, model, objective, status);
    const actions = document.createElement('div'); actions.className = 'subagent-actions';
    const view = document.createElement('button'); view.type='button'; view.textContent=t('View'); view.dataset.action='view';
    actions.appendChild(view);
    if (activeStates.has(row.status)) { const stop=document.createElement('button'); stop.type='button'; stop.textContent=t('Stop'); stop.dataset.action='stop'; actions.appendChild(stop); }
    else { const remove=document.createElement('button'); remove.type='button'; remove.textContent=t('Remove'); remove.dataset.action='remove'; actions.appendChild(remove); }
    item.append(main, actions); list.appendChild(item);
  }
}

async function showDetail(childId) {
  selectedId = childId;
  const detail = await json(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}/${encodeURIComponent(childId)}`);
  el('subagent-detail').hidden = false;
  el('subagent-detail-title').textContent = `${detail.name} · ${detail.status}`;
  const events = await json(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}/events?after=0&limit=1000&tail=true&child_id=${encodeURIComponent(childId)}`);
  const text = [];
  for (const event of events.events || []) {
    if (event.kind === 'thinking') text.push(`[thinking] ${event.payload?.text || ''}`);
    else if (event.kind === 'delta') text.push(event.payload?.text || '');
    else if (event.kind === 'tool_start') text.push(`\n▶ ${event.payload?.tool || 'tool'} ${event.payload?.command || ''}\n`);
    else if (event.kind === 'tool_output') text.push(`\n${event.payload?.output || event.payload?.result?.output || ''}\n`);
    else if (event.kind === 'status' && event.payload?.ask_user) {
      const ask=event.payload.ask_user;
      text.push(`\n? ${ask.question || 'Input required'}\n${(ask.options || []).map(option => `- ${option.label || option}`).join('\n')}\n`);
    }
  }
  el('subagent-output').textContent = text.join('') || detail.result || detail.error || 'No output yet.';
  const messageRow = el('subagent-message')?.closest('.subagent-message-row');
  if (messageRow) messageRow.hidden = !activeStates.has(detail.status);
  armCollapse();
}

async function refresh(id = window.sessionModule?.getCurrentSessionId?.()) {
  const myGeneration = ++generation;
  sessionId = id || '';
  if (source) { source.close(); source = null; }
  rows = []; cursor = 0; selectedId = '';
  if (el('subagent-detail')) el('subagent-detail').hidden = true;
  if (!sessionId) { render(); return; }
  try {
    const data = await json(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}`);
    if (myGeneration !== generation || sessionId !== (id || '')) return;
    rows = data.subagents || []; cursor = Number(data.latest_cursor || 0); render(); connect();
  } catch (error) { console.warn('[subagents]', error); render(); }
}

function connect() {
  if (!sessionId || document.visibilityState === 'hidden') return;
  const expected = sessionId;
  source = new EventSource(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}/events/stream?after=${cursor}`);
  source.onmessage = event => {
    if (sessionId !== expected) return;
    try { const data=JSON.parse(event.data); cursor=Math.max(cursor, Number(data.seq||0)); } catch (_) {}
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => void refreshKeepStream(expected), 180);
  };
  source.onerror = () => { source?.close(); source=null; setTimeout(() => { if (sessionId===expected) connect(); }, 1200); };
}
async function refreshKeepStream(expected) {
  try {
    const data=await json(`${api}/api/chat/subagents/${encodeURIComponent(expected)}`);
    if (sessionId!==expected) return; rows=data.subagents||[]; render();
    if (selectedId) {
      clearTimeout(detailTimer);
      detailTimer = setTimeout(() => void showDetail(selectedId).catch(() => {}), 300);
    }
  } catch (_) {}
}

function bind() {
  const card=el('subagents-status'), toggle=el('subagents-toggle');
  const label=card?.querySelector('.subagents-label'); if(label) label.textContent=t('Agents');
  const title=card?.querySelector('.subagents-title strong'); if(title) title.textContent=t('Subagents');
  if(el('subagent-send')) el('subagent-send').textContent=t('Send');
  if(el('subagent-message')) { el('subagent-message').placeholder=t('Message subagent…'); el('subagent-message').setAttribute('aria-label', t('Message subagent…')); }
  toggle?.addEventListener('click', () => {
    const open=card.classList.toggle('expanded'); toggle.setAttribute('aria-expanded', String(open));
    if(open) {
      const plan=document.getElementById('plan-mode-status'); plan?.classList.remove('expanded'); plan?.querySelector('.chat-work-card-toggle')?.setAttribute('aria-expanded','false'); card.style.removeProperty('top');
      armCollapse();
    }
  });
  card?.addEventListener('pointermove', armCollapse);
  el('subagents-list')?.addEventListener('click', async event => {
    const button=event.target.closest('button[data-action]'), row=event.target.closest('.subagent-row'); if(!button||!row) return;
    const childId=row.dataset.childId, action=button.dataset.action;
    try {
      if(action==='view') await showDetail(childId);
      else if(action==='stop') await post(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}/${encodeURIComponent(childId)}/stop`);
      else if(action==='remove') { await json(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}/${encodeURIComponent(childId)}`, {method:'DELETE'}); if(selectedId===childId){selectedId='';el('subagent-detail').hidden=true;} }
      await refreshKeepStream(sessionId);
    } catch(error) { window.uiModule?.showError?.(error.message); }
  });
  el('subagent-send')?.addEventListener('click', async () => {
    const input=el('subagent-message'), message=input?.value.trim(); if(!selectedId||!message) return;
    try { await post(`${api}/api/chat/subagents/${encodeURIComponent(sessionId)}/${encodeURIComponent(selectedId)}/message`, {message}); input.value=''; }
    catch(error) { window.uiModule?.showError?.(error.message); }
  });
  document.addEventListener('visibilitychange', () => { if(document.visibilityState==='visible' && sessionId) void refresh(sessionId); else source?.close(); });
}

export default { bind, refresh };
