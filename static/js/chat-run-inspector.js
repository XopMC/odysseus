import { t } from './i18n.js';

const el = id => document.getElementById(id);
let sessionId = '';
let runId = '';
let beforeSeq = null;
let beforeRunId = null;
let generation = 0;

async function get(url) {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

function field(parent, label, value) {
  const row = document.createElement('div'); row.className = 'run-inspector-field';
  const name = document.createElement('span'); name.textContent = t(label);
  const data = document.createElement('span'); data.textContent = value == null || value === '' ? '—' : String(value);
  row.append(name, data); parent.appendChild(row);
}

function renderRuns(runs, preferredChildId = '', append = false) {
  const list = el('run-inspector-runs');
  if (!append) list.replaceChildren();
  for (const run of runs) {
    const section = document.createElement('section'); section.className = 'run-inspector-run';
    const select = document.createElement('button'); select.type = 'button';
    select.dataset.runId = run.run_id;
    select.textContent = `${run.run_id} · ${t(run.status)} · seq ${run.durable_seq}`;
    section.appendChild(select);
    field(section, 'Started', run.started_at);
    field(section, 'Finished', run.terminal_at);
    for (const child of run.children || []) {
      const row = document.createElement('div'); row.className = 'run-inspector-child';
      row.dataset.childId = child.child_run_id;
      row.textContent = `${t('Child')} ${child.child_run_id} · ${t(child.status)} · ${child.model || '—'}`;
      field(row, 'Started', child.started_at); field(row, 'Finished', child.finished_at);
      field(row, 'Event cursor', child.event_cursor);
      for (const artifact of child.artifacts || []) {
        const button = document.createElement('button'); button.type = 'button';
        button.dataset.artifactId = artifact.id;
        button.textContent = `${t('Artifact')} ${artifact.id} · ${artifact.kind} · ${t(artifact.status)} · ${artifact.created_at || '—'}`;
        row.appendChild(button);
      }
      section.appendChild(row);
    }
    for (const tool of run.tool_calls || []) {
      field(section, 'Tool', `${tool.tool_name} · ${tool.tool_call_id} · ${t(tool.status)} · ${tool.created_at || '—'}`);
    }
    list.appendChild(section);
  }
  if (preferredChildId) {
    const child = [...list.querySelectorAll('[data-child-id]')].find(node => node.dataset.childId === preferredChildId);
    child?.classList.add('selected'); child?.scrollIntoView?.({ block: 'nearest' });
  }
}

function renderEvents(events, append = false) {
  const list = el('run-inspector-event-list');
  if (!append) list.replaceChildren();
  const fragment = document.createDocumentFragment();
  for (const event of events) {
    const button = document.createElement('button'); button.type = 'button';
    button.dataset.eventSeq = String(event.seq);
    button.dataset.toolCallId = event.tool_call_id || '';
    button.dataset.eventKind = event.kind;
    button.dataset.eventTime = event.created_at == null ? '' : String(event.created_at);
    button.dataset.segmentId = event.segment_id || '';
    button.textContent = `#${event.seq} · ${event.kind}${event.tool_name ? ` · ${event.tool_name}` : ''}`;
    fragment.appendChild(button);
  }
  if (append) list.insertBefore(fragment, list.firstChild);
  else list.appendChild(fragment);
}

async function loadEvents(append = false) {
  if (!sessionId || !runId) return;
  const myGeneration = generation;
  const url = `/api/chat/work/${encodeURIComponent(sessionId)}/run-inspector/${encodeURIComponent(runId)}/events`
    + (append && beforeSeq != null ? `?before_seq=${beforeSeq}` : '');
  const page = await get(url);
  if (generation !== myGeneration) return;
  renderEvents(page.events || [], append);
  beforeSeq = page.previous_cursor;
  el('run-inspector-older').hidden = !page.has_more_before;
  el('run-inspector-events').hidden = false;
  el('run-inspector-event-title').textContent = `${t('Events')} · ${runId}`;
  if (!page.replay_available) el('run-inspector-status').textContent = t('Replay unavailable');
}

function showEventDetail(event) {
  const detail = el('run-inspector-event-detail');
  const stamp = Number(event.created_at);
  detail.textContent = `${t('Run')} ${runId} · ${t('Event')} #${event.seq} · ${event.kind}`
    + (event.tool_call_id ? ` · ${t('Tool call')} ${event.tool_call_id}` : '')
    + (event.segment_id ? ` · ${t('Segment')} ${event.segment_id}` : '')
    + (event.created_at != null && Number.isFinite(stamp) ? ` · ${new Date(stamp * 1000).toLocaleString()}` : '');
  detail.hidden = false; detail.focus();
  const rendered = event.tool_call_id && [...document.querySelectorAll('.agent-thread-node[data-tool-call-id]')]
    .find(node => node.dataset.toolCallId === event.tool_call_id);
  rendered?.scrollIntoView?.({ block: 'nearest' });
}

async function open({ childId = '', runId: preferredRunId = '', eventSeq = null } = {}) {
  const targetSession = window.sessionModule?.getCurrentSessionId?.() || '';
  if (!targetSession) return;
  const dialog = el('run-inspector-dialog');
  if (!dialog) return;
  sessionId = targetSession; runId = ''; beforeSeq = null; beforeRunId = null;
  const myGeneration = ++generation;
  el('run-inspector-runs').replaceChildren();
  el('run-inspector-events').hidden = true;
  el('run-inspector-artifact').hidden = true;
  el('run-inspector-event-detail').hidden = true;
  el('run-inspector-older-runs').hidden = true;
  el('run-inspector-status').textContent = t('Loading…');
  if (!dialog.open) dialog.showModal();
  try {
    const data = await get(`/api/chat/work/${encodeURIComponent(targetSession)}/run-inspector`);
    if (generation !== myGeneration || sessionId !== targetSession) return;
    renderRuns(data.runs || [], childId);
    beforeRunId = data.next_cursor || null;
    el('run-inspector-older-runs').hidden = !beforeRunId;
    el('run-inspector-status').textContent = data.runs?.length ? '' : t('No runs');
    const selected = (data.runs || []).find(run => run.run_id === preferredRunId)
      || (data.runs || []).find(run => (run.children || []).some(child => child.child_run_id === childId))
      || data.runs?.[0];
    if (selected) {
      runId = selected.run_id;
      await loadEvents();
      if (generation === myGeneration && Number.isSafeInteger(eventSeq) && eventSeq >= 0
          && selected.run_id === preferredRunId) {
        const exact = await get(`/api/chat/work/${encodeURIComponent(targetSession)}/run-inspector/${encodeURIComponent(runId)}/events?before_seq=${eventSeq + 1}&limit=1`);
        if (generation === myGeneration) {
          const event = (exact.events || []).find(item => item.seq === eventSeq);
          if (event) showEventDetail(event);
        }
      }
    }
  } catch (error) {
    if (generation === myGeneration) el('run-inspector-status').textContent = error.message;
  }
}

function bind() {
  for (const id of ['plan-run-inspector', 'goal-run-inspector', 'wait-run-inspector', 'subagent-run-inspector', 'run-inspector-title']) {
    if (el(id)) el(id).textContent = t('Run inspector');
  }
  if (el('run-inspector-older')) el('run-inspector-older').textContent = t('Load earlier events');
  if (el('run-inspector-older-runs')) el('run-inspector-older-runs').textContent = t('Load older runs');
  el('run-inspector-close')?.setAttribute('aria-label', t('Close'));
  el('run-inspector-close')?.addEventListener('click', () => el('run-inspector-dialog')?.close());
  el('run-inspector-dialog')?.addEventListener('close', () => { ++generation; });
  document.addEventListener('odysseus:run-inspector', event => { void open(event.detail || {}); });
  el('run-inspector-older-runs')?.addEventListener('click', () => {
    if (!sessionId || !beforeRunId) return;
    const targetSession = sessionId;
    const myGeneration = generation;
    const button = el('run-inspector-older-runs'); button.disabled = true;
    void get(`/api/chat/work/${encodeURIComponent(targetSession)}/run-inspector?before_run_id=${encodeURIComponent(beforeRunId)}`)
      .then(data => {
        if (generation !== myGeneration || sessionId !== targetSession) return;
        renderRuns(data.runs || [], '', true);
        beforeRunId = data.next_cursor || null;
        button.hidden = !beforeRunId;
      })
      .catch(error => { el('run-inspector-status').textContent = error.message; })
      .finally(() => { button.disabled = false; });
  });
  el('run-inspector-runs')?.addEventListener('click', event => {
    const artifact = event.target.closest('button[data-artifact-id]');
    if (artifact) {
      const targetSession = sessionId;
      const myGeneration = generation;
      void get(`/api/chat/work/${encodeURIComponent(targetSession)}/run-inspector/artifacts/${encodeURIComponent(artifact.dataset.artifactId)}`)
        .then(data => {
          if (generation !== myGeneration || sessionId !== targetSession) return;
          el('run-inspector-artifact-title').textContent = `${t('Artifact')} ${data.id} · ${data.kind}`;
          el('run-inspector-artifact-body').textContent = data.body || '';
          el('run-inspector-artifact').hidden = false;
          el('run-inspector-artifact').scrollIntoView?.({ block: 'nearest' });
        })
        .catch(error => { el('run-inspector-status').textContent = error.message; });
      return;
    }
    const button = event.target.closest('button[data-run-id]');
    if (!button) return;
    runId = button.dataset.runId; beforeSeq = null; ++generation;
    el('run-inspector-event-detail').hidden = true;
    el('run-inspector-artifact').hidden = true;
    void loadEvents().catch(error => { el('run-inspector-status').textContent = error.message; });
  });
  el('run-inspector-older')?.addEventListener('click', () => {
    void loadEvents(true).catch(error => { el('run-inspector-status').textContent = error.message; });
  });
  el('run-inspector-event-list')?.addEventListener('click', event => {
    const button = event.target.closest('button[data-event-seq]');
    if (!button) return;
    showEventDetail({
      seq: Number(button.dataset.eventSeq), kind: button.dataset.eventKind,
      tool_call_id: button.dataset.toolCallId || null,
      segment_id: button.dataset.segmentId || null,
      created_at: button.dataset.eventTime ? Number(button.dataset.eventTime) : null,
    });
  });
}

export default { bind, open };
