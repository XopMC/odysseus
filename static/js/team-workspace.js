// Team has its own request lifecycle. It never delegates Start to chat_stream.
import { mountEngineeringWorkspace } from './engineering-workspace.js';
import { bindUiText, uiTextSource, t } from './i18n.js';
const encode = encodeURIComponent;
const TABS = ['Tasks', 'Team', 'Terminals', 'Files & Changes', 'Resources'];
const TERMINAL_TEXT_LIMIT = 200000;

export function terminalPlainText(value) {
  return String(value ?? '').replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, '')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
}

export function createTeamEventCursor(teamId, initial = 0) {
  let id = teamId, seq = Number(initial) || 0, gap = false;
  return {
    get afterSeq() { return seq; }, get needsSnapshot() { return gap; },
    reset(nextId, nextSeq = 0) { id = nextId; seq = Number(nextSeq) || 0; gap = false; },
    accept(event) {
      if (event.team_id && event.team_id !== id) return false;
      if (!Number.isSafeInteger(event.seq) || event.seq <= seq) return false;
      if (event.seq !== seq + 1) { gap = true; return false; }
      seq = event.seq; return true;
    },
  };
}

export function createTeamNotificationTracker() {
  let team = null, previous = new Map();
  return {
    reset() { team = null; previous = new Map(); },
    observe(teamId, snapshot) {
      const statuses = new Map([['team', snapshot.status || snapshot.task?.status || '']]);
      for (const worker of snapshot.workers || snapshot.tasks || []) if (worker.id) statuses.set(`worker:${worker.id}`, worker.status);
      if (!teamId || team !== teamId) { team = teamId; previous = statuses; return []; }
      const changes = [];
      for (const [subject, status] of statuses) {
        const meaningful = ['blocked', 'waiting_approval', 'failed'].includes(status)
          || (subject === 'team' && ['done', 'completed', 'accepted'].includes(status));
        if (meaningful && previous.get(subject) !== status) changes.push({ subject, status });
      }
      previous = statuses; return changes;
    },
  };
}

const safeInteger = value => Number.isSafeInteger(value) && value >= 0;
export function reviewedGitFiles(diff) {
  if (diff?.truncated || !Array.isArray(diff?.files) || diff.files.length > 1000) throw new Error('A complete changed-file list is required before integration. Refresh the diff.');
  for (const path of diff.files) {
    if (typeof path !== 'string' || !path || path.startsWith('/') || path.startsWith(':')
      || /[\\*?\[\]\0\r\n]/.test(path) || path.split('/').some(part => ['', '.', '..'].includes(part))) throw new Error('Changed files must be literal repository-relative paths.');
  }
  return [...new Set(diff.files)];
}
export function fileRollbackArguments(checkpoint) {
  if (!checkpoint?.id || !['applied', 'partial'].includes(checkpoint.status)) throw new Error('Only a known applied file checkpoint can be restored.');
  if (!Array.isArray(checkpoint.files) || !checkpoint.files.length || checkpoint.files.length > 32) throw new Error('Checkpoint requires an exact file list.');
  const expected = {};
  for (const file of checkpoint.files) {
    if (typeof file.path !== 'string' || !file.path.startsWith('/') || /[\0\r\n]/.test(file.path) || Object.hasOwn(expected, file.path)) throw new Error('Checkpoint requires distinct absolute paths.');
    if (file.after_exists === true ? !/^[a-f0-9]{64}$/i.test(file.after_sha256 || '') : file.after_exists !== false || file.after_sha256 !== null) throw new Error('Checkpoint is missing an exact after-state hash.');
    expected[file.path] = file.after_sha256;
  }
  return { checkpoint_id: checkpoint.id, expected_sha256: expected };
}
export function normalizeTeamStart(draft, models) {
  if (draft.setup_mode === 'basic') {
    if (!Array.isArray(draft.worker_pool) || !draft.worker_pool.length) throw new Error('Select at least one worker model.');
    const seen = new Set();
    draft = { ...draft, config: { ...draft.config, auto_dispatch: true, auto_continue: true },
      workers: draft.worker_pool.filter(item => {
        const key = JSON.stringify([item?.endpoint_id, item?.model]);
        if (seen.has(key)) return false; seen.add(key); return true;
      }).map(item => ({ endpoint_id: item.endpoint_id, model: item.model, role: 'executor' })) };
  }
  const route = value => {
    const found = models.find(m => m.endpoint_id === value?.endpoint_id && m.model === value?.model);
    if (!found) throw new Error('Select a known endpoint and model for every role.');
    return { endpoint_id: found.endpoint_id, model: found.model };
  };
  if (!String(draft.goal || '').trim()) throw new Error('Describe the team goal.');
  if (!String(draft.project_path || '').trim()) throw new Error('Enter the project directory on the host.');
  if (!String(draft.project_path).startsWith('/')) throw new Error('Project directory must be an absolute host path.');
  const config = Object.fromEntries(['auto_dispatch', 'auto_continue', 'reviewer', 'web', 'external', 'trusted_host'].map(k => [k, draft.config?.[k] === true]));
  config.preset = String(draft.config?.preset || 'coding');
  config.project_profile = Object.fromEntries(['install_command', 'run_command', 'test_command', 'build_command', 'constraints'].map(k => [k, String(draft.config?.project_profile?.[k] || '')]));
  const workers = (draft.workers || []).map(w => ({
    ...route(w), name: String(w.name || ''), role: String(w.role || 'executor'),
    objective: String(w.objective || ''), acceptance: String(w.acceptance || ''),
    depends_on: Array.isArray(w.depends_on) ? w.depends_on.map(String) : [],
    write_scope: Array.isArray(w.write_scope) ? w.write_scope.map(String) : [],
  }));
  const payload = { title: String(draft.title || 'Team task'), goal: String(draft.goal).trim(),
    project_path: String(draft.project_path).trim(), leader: route(draft.leader), workers, config,
    budget_microusd: Number(draft.budget_microusd || 0), external_approvals: [] };
  if (!safeInteger(payload.budget_microusd)) throw new Error('Budget must be a nonnegative integer in micro-USD.');
  for (const approval of draft.external_approvals || []) {
    if (!approval.endpoint_id || approval.consent !== true || !safeInteger(approval.limit_microusd) || !approval.limit_microusd) throw new Error('External approval needs explicit consent, an endpoint, and a positive budget.');
    if (!safeInteger(approval.input_rate_per_million) || !safeInteger(approval.output_rate_per_million)) throw new Error('Unknown external price: provide both token rates.');
    if (!String(approval.approved_context || '').trim()) throw new Error('Describe which context may be sent externally.');
    if (!['goal_only', 'assigned_context'].includes(approval.data_scope)) throw new Error('Choose an explicit external data scope.');
    payload.external_approvals.push({ endpoint_id: String(approval.endpoint_id), limit_microusd: approval.limit_microusd,
      input_rate_per_million: approval.input_rate_per_million,
      output_rate_per_million: approval.output_rate_per_million,
      approved_context: String(approval.approved_context), data_scope: approval.data_scope, consent: true });
  }
  for (const [index, selected] of [payload.leader, ...workers].entries()) {
    const model = models.find(m => m.endpoint_id === selected.endpoint_id && m.model === selected.model);
    if (model.local !== true) {
      if (!config.external) throw new Error('External models are disabled.');
      if (!payload.budget_microusd || !payload.external_approvals.some(a => a.endpoint_id === selected.endpoint_id)) throw new Error('External model needs explicit approval, known prices, and a task budget.');
      if (index > 0 && payload.external_approvals.find(a => a.endpoint_id === selected.endpoint_id)?.data_scope !== 'assigned_context') throw new Error('External worker data scope must be assigned context; goal only authorizes planning.');
    }
  }
  return payload;
}

function element(tag, text = '', className = '') {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function uiElement(tag, label, className = '') { return bindUiText(element(tag, label, className), label); }
const TEAM_STATUSES = new Set(['pending', 'planned', 'queued', 'running', 'paused', 'waiting_approval', 'blocked', 'recovering', 'cancelled', 'failed', 'done', 'accepted', 'rejected', 'Ready', 'No task']);
const TEAM_ROLES = new Set(['executor', 'researcher', 'reviewer', 'planner', 'worker']);
function teamStatus(tag, value, className = '') {
  return TEAM_STATUSES.has(value) ? uiElement(tag, value, className) : element(tag, value, className);
}
function uiOption(label, value) { return bindUiText(new Option(label, value), label); }
function button(label, action, className = 'memory-toolbar-btn', translate = true) {
  const node = element('button', label, className); if (translate) bindUiText(node, label); node.type = 'button';
  node.addEventListener('click', action); return node;
}
function artifactButton(path, action) {
  const node = button('', action); node.append(uiElement('span', 'Open artifact:'), document.createTextNode(' ' + path)); return node;
}
function input(label, type = 'text', value = '') {
  const wrap = element('label', '', 'team-field'); wrap.append(bindUiText(element('span', label), label));
  const control = element(type === 'textarea' ? 'textarea' : 'input');
  if (type !== 'textarea') control.type = type;
  control.value = value; wrap.append(control); return { wrap, control };
}
function checkbox(label, checked = false) {
  const wrap = element('label', '', 'team-check'); const control = element('input');
  control.type = 'checkbox'; control.checked = checked; wrap.append(control, document.createTextNode(label));
  bindUiText(wrap, label);
  return { wrap, control };
}
const list = text => String(text || '').split(',').map(s => s.trim()).filter(Boolean);

export function createTeamWorkspace({ getSessionId, fetchImpl = globalThis.fetch.bind(globalThis),
  EventSourceImpl = globalThis.EventSource, storage = globalThis.sessionStorage,
  NotificationImpl = globalThis.Notification,
  confirmImpl = message => globalThis.confirm(message), root = document.getElementById('team-workspace'),
  modeButton = document.getElementById('mode-team-btn') } = {}) {
  let enabled = false, active = false, hostEnabled = false, sid = null, generation = 0;
  let engineeringEnabled = false, destroyEngineering = null;
  let selectedEngineeringProject = null;
  let teamId = null, snapshot = null, models = [], presets = [], profiles = [], source = null, retryTimer = null, refreshTimer = null;
  let sessionTimer = null, terminalTimer = null, resourceTimer = null, selectedTab = 'Tasks', selectedTerminal = '', terminalBusy = false;
  let pending = null, disposed = false, fileHash = null, fileLoadedPath = '', diffState = null, snapshotRequestSeq = 0, evidenceRequestSeq = 0, terminalListRequestSeq = 0;
  let cursor = createTeamEventCursor(null), terminalState = new Map(), manualWorkers = [], intentSignature = '';
  let previousMode = null;
  let diffRequestSeq = 0;
  let notificationsEnabled = false;
  const notificationTracker = createTeamNotificationTracker();
  const ui = {}, panels = new Map();
  const cursorKey = id => `odysseus-team-after-seq:${id}`;
  const notice = (message, error = false) => {
    if (!ui.notice) return;
    ui.notice.textContent = message; ui.notice.classList.toggle('team-error', error);
  };
  function updateNotificationControl() {
    if (!ui.notifications) return;
    ui.notifications.textContent = t(notificationsEnabled ? 'Notifications on (this tab)' : 'Enable browser notifications');
    ui.notifications.setAttribute('aria-pressed', String(notificationsEnabled));
    ui.notifications.disabled = !NotificationImpl;
  }
  async function toggleNotifications() {
    if (notificationsEnabled) { notificationsEnabled = false; updateNotificationControl(); return; }
    if (!NotificationImpl) { notice(t('Browser notifications are unavailable here.'), true); return; }
    // Permission is requested only from this explicit button click, never init/SSE.
    try {
      const permission = NotificationImpl.permission === 'default' ? await NotificationImpl.requestPermission() : NotificationImpl.permission;
      if (disposed) return;
      notificationsEnabled = permission === 'granted'; updateNotificationControl();
      notice(t(notificationsEnabled ? 'Local notifications enabled while this Team view remains open. No external push is used.' : 'Notifications were not allowed. Check browser permissions or use HTTPS/localhost.'), !notificationsEnabled);
    } catch (_) { notice(t('Notifications are unavailable in this browser or connection.'), true); }
  }
  async function request(path, options = {}) {
    const res = await fetchImpl(path, { credentials: 'same-origin', cache: 'no-store', ...options });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data?.ok === false) {
      const error = new Error(typeof data?.detail === 'string' ? data.detail : data?.error || `Request failed (${res.status})`);
      error.status = res.status; throw error;
    }
    return data;
  }
  const post = (path, body = {}) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const host = async (op, args = {}) => {
    if (!hostEnabled || !teamId) throw new Error('Host access is unavailable for this team.');
    const data = await post(`/api/team/${encode(teamId)}/host`, { op, args, ...(ui.hostScope?.value ? { worker_id: ui.hostScope.value } : {}) });
    return data.result ?? data;
  };
  async function hostCreate(op, args) {
    const key = `odysseus-team-intent:${teamId}:${ui.hostScope?.value || 'team'}:${op}`;
    let identity; try { identity = storage?.getItem(key); } catch (_) {}
    identity ||= crypto.randomUUID(); try { storage?.setItem(key, identity); } catch (_) {}
    const result = await host(op, { ...args, idempotency_key: identity });
    try { storage?.removeItem(key); } catch (_) {}
    return result;
  }
  function disconnect() {
    source?.close(); source = null; clearTimeout(retryTimer); clearTimeout(refreshTimer);
    clearInterval(terminalTimer); terminalTimer = null;
    clearInterval(resourceTimer); resourceTimer = null;
  }
  async function act(action, target) {
    if (pending?.generation === generation) return;
    const token = generation, lease = { generation }; pending = lease; if (target) target.disabled = true;
    try { await action(); if (token === generation) notice('Saved.'); }
    catch (error) { if (token === generation) notice(error.message, true); }
    finally { if (pending === lease) pending = null; if (target) target.disabled = false; }
  }
  function routeSelect(selected = null) {
    const select = element('select'); bindUiText(select, 'Endpoint and model', 'aria-label');
    select.append(uiOption('Choose endpoint / model', ''));
    for (const model of models) {
      const value = JSON.stringify([model.endpoint_id, model.model]);
      const option = bindUiText(new Option('', value), model.local ? 'Local model' : 'External model', 'text', ' · ' + (model.label || model.model));
      option.selected = selected?.endpoint_id === model.endpoint_id && selected?.model === model.model;
      select.append(option);
    }
    return select;
  }
  function selectedRoute(select) {
    if (!select.value) return null;
    const [endpoint_id, model] = JSON.parse(select.value); return { endpoint_id, model };
  }
  function projectPath() {
    const worker = (snapshot?.workers || []).find(w => w.id === ui.hostScope?.value);
    return worker?.profile?.cwd || snapshot?.metadata?.project_path || snapshot?.task?.metadata?.project_path || snapshot?.task?.project_path || snapshot?.project_path || ui.project.control.value;
  }
  function current(token, id = sid) { return !disposed && active && token === generation && sid === id && getSessionId() === sid; }

  function build() {
    destroyEngineering?.(); destroyEngineering = null;
    panels.clear();
    root.replaceChildren(); bindUiText(root, 'Team workspace', 'aria-label');
    const heading = element('div', '', 'team-heading'); heading.append(uiElement('h3', 'Team workspace'));
    ui.status = element('span', '', 'team-status'); ui.status.append(teamStatus('span', 'No task')); heading.append(ui.status,
      button('Refresh', () => act(() => loadSnapshot())), button('Back to chat', () => setActive(false)));
    root.append(heading);
    root.append(uiElement('p', 'Team tasks retain checkpoints. Review results before applying changes; uncertain command outcomes require inspection before resuming.', 'team-notice'));
    ui.notifications = button('Enable browser notifications', toggleNotifications); updateNotificationControl(); root.append(ui.notifications);
    ui.hostScope = element('select'); bindUiText(ui.hostScope, 'Host scope', 'aria-label'); ui.hostScope.append(bindUiText(new Option('Team host scope', ''), 'Team host scope'));
    ui.hostScope.disabled = !hostEnabled;
    ui.hostScope.addEventListener('change', () => { selectedTerminal = ''; terminalState = new Map(); fileHash = null; fileLoadedPath = ''; clearDiffReview(); ui.fileCheckpointList.replaceChildren(); ui.fileCheckpointStatus.textContent = ''; ui.editor.control.value = ''; renderTerminal(); if (selectedTab === 'Terminals') act(refreshTerminals); });
    const scopeLabel = uiElement('label', 'Host scope', 'team-field'); scopeLabel.append(ui.hostScope); root.append(scopeLabel);
    ui.notice = element('div', '', 'team-notice'); ui.notice.setAttribute('role', 'status'); root.append(ui.notice);
    const tabs = element('div', '', 'team-tabs'); tabs.setAttribute('role', 'tablist');
    const visibleTabs = engineeringEnabled ? [...TABS, 'Engineering'] : TABS;
    for (const label of visibleTabs) {
      const tab = button(label, () => showTab(label)); tab.setAttribute('role', 'tab');
      tab.id = 'team-tab-' + visibleTabs.indexOf(label); tab.setAttribute('aria-controls', tab.id + '-panel'); tabs.append(tab);
      const panel = element('section', '', 'team-panel'); panel.id = tab.id + '-panel'; panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id); panels.set(label, { tab, panel });
    }
    root.append(tabs); for (const { panel } of panels.values()) root.append(panel);
    buildTasks(); buildTeam(); buildTerminals(); buildFiles();
    if (engineeringEnabled) destroyEngineering = mountEngineeringWorkspace(panels.get('Engineering').panel, {
      onProjectSelected: project => {
        selectedEngineeringProject = project;
        if (project) ui.project.control.value = project.root;
        ui.project.control.disabled = Boolean(project);
        notice(project ? `${t('New Team runs:')} ${project.name} ${t('on host')} ${project.host_id}. ${t('Execution mode:')} ${t(project.access_mode || 'not approved')}. ${t('Existing runs are unchanged.')}` : t('New Team runs use the legacy project settings.'));
      },
      request: (path, options = {}) => options.body === undefined ? request(path, options) : request(path, {
        ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, body: JSON.stringify(options.body)
      })
    });
    ui.resources = element('pre', '', 'team-output'); panels.get('Resources').panel.append(button('Refresh resources', () => act(loadResources)), ui.resources);
    showTab(selectedTab);
  }
  function buildTasks() {
    const panel = panels.get('Tasks').panel;
    ui.start = element('div', '', 'team-config');
    ui.setupMode = element('select'); bindUiText(ui.setupMode, 'Team setup', 'aria-label');
    ui.setupMode.append(uiOption('Basic setup', 'basic'), uiOption('Advanced setup', 'advanced'));
    ui.start.append(ui.setupMode);
    ui.advanced = element('div'); ui.advanced.hidden = true;
    ui.setupMode.addEventListener('change', updateSetupMode);
    ui.title = input('Task title', 'text', 'Team task'); ui.goal = input('Goal and completion criteria', 'textarea');
    ui.project = input('Absolute project directory on the host', 'text');
    ui.installCommand = input('Install command (optional)', 'text'); ui.runCommand = input('Run command (optional)', 'text');
    ui.testCommand = input('Test command', 'text'); ui.buildCommand = input('Build command (optional)', 'text');
    ui.constraints = input('Project constraints', 'textarea');
    ui.leader = routeSelect(); const leader = uiElement('label', 'Leader endpoint / model', 'team-field'); leader.append(ui.leader);
    ui.preset = element('select'); bindUiText(ui.preset, 'Team preset', 'aria-label');
    for (const preset of presets) ui.preset.append(preset.id === 'coding' && preset.label === 'Coding' ? uiOption('Coding', preset.id) : new Option(preset.label || preset.id, preset.id));
    if (!presets.length) ui.preset.append(uiOption('Coding', 'coding'));
    ui.preset.addEventListener('change', () => {
      const config = presets.find(p => p.id === ui.preset.value)?.config || {};
      for (const [key, item] of Object.entries(ui.toggles)) if (key in config && key !== 'external') item.control.checked = config[key] === true;
    });
    ui.start.append(ui.goal.wrap, ui.project.wrap, leader);
    ui.basicPool = element('fieldset'); ui.basicPool.append(uiElement('legend', 'Worker models'));
    ui.poolChoices = models.map(model => {
      const choice = checkbox('', false);
      choice.wrap.style.overflowWrap = 'anywhere'; choice.wrap.style.whiteSpace = 'normal';
      choice.wrap.append(document.createTextNode(`${model.label || model.model} · ${model.endpoint_id}`));
      choice.control.setAttribute('aria-label', model.label || `${model.endpoint_id} / ${model.model}`);
      ui.basicPool.append(choice.wrap); return { model, control: choice.control };
    });
    ui.basicPool.append(uiElement('p', 'The leader plans the goal and assigns work only to the selected models. Permissions and budgets are never granted automatically.'));
    ui.start.append(ui.basicPool);
    ui.advanced.append(ui.title.wrap, ui.installCommand.wrap, ui.runCommand.wrap,
      uiElement('p', 'Install and run commands are project instructions. Saving or loading them does not execute them or grant tool access.'),
      ui.testCommand.wrap, ui.buildCommand.wrap, ui.constraints.wrap, ui.preset);
    const profileBox = element('fieldset'); profileBox.append(uiElement('legend', 'Saved project setup'));
    ui.profileSelect = element('select'); bindUiText(ui.profileSelect, 'Saved project profile', 'aria-label');
    ui.profileSelect.append(uiOption('Choose saved project profile', ''));
    ui.profileName = input('Profile name');
    profileBox.append(ui.profileSelect, ui.profileName.wrap,
      button('Load project profile', () => act(async () => {
        if (teamId) throw new Error('Project profiles apply only before Start.');
        const selected = profiles.find(item => item.name === ui.profileSelect.value);
        if (!selected?.profile) throw new Error('Choose a saved project profile.');
        const profile = selected.profile;
        for (const [field, control] of [['project_path', ui.project], ['install_command', ui.installCommand], ['run_command', ui.runCommand], ['test_command', ui.testCommand], ['build_command', ui.buildCommand], ['constraints', ui.constraints]]) {
          control.control.value = typeof profile[field] === 'string' ? profile[field] : '';
        }
        ui.profileName.control.value = selected.name;
      })),
      button('Save project profile', event => act(async () => {
        if (teamId) throw new Error('Project profiles can be saved only before Start.');
        const name = ui.profileName.control.value.trim(), token = generation;
        if (!name) throw new Error('Enter a profile name.');
        const profile = { project_path: ui.project.control.value.trim(), install_command: ui.installCommand.control.value,
          run_command: ui.runCommand.control.value, test_command: ui.testCommand.control.value,
          build_command: ui.buildCommand.control.value, constraints: ui.constraints.control.value };
        if (!profile.project_path.startsWith('/')) throw new Error('Profile project directory must be absolute.');
        await post('/api/team/profiles', { name, profile });
        if (!current(token) || teamId) return;
        await loadProjectProfiles(); ui.profileSelect.value = name;
      }, event.currentTarget)), button('Refresh project profiles', () => act(loadProjectProfiles)),
      uiElement('p', 'Loading is explicit and changes only the path, project commands and constraints. It never grants host, web or external access.'));
    ui.advanced.append(profileBox);
    loadProjectProfiles().catch(error => notice(error.message, true));
    const toggles = element('div', '', 'team-toggle-grid'); ui.toggles = {};
    for (const [key, label, value] of [['auto_dispatch', 'Auto-dispatch', true], ['auto_continue', 'Continue after completed work', true], ['reviewer', 'Require reviewer', true], ['web', 'Web access', false], ['external', 'Allow explicitly approved external endpoints', false], ['trusted_host', 'Trusted host: Unix user access', false]]) {
      const item = checkbox(label, value); ui.toggles[key] = item; toggles.append(item.wrap);
      if (key === 'trusted_host' && !hostEnabled) item.control.disabled = true;
    }
    const permissions = element('details'); permissions.append(uiElement('summary', 'Access permissions'), toggles);
    ui.start.append(ui.advanced, permissions);
    const approvals = element('details'); approvals.append(uiElement('summary', 'External endpoint consent and budget'));
    ui.budget = input('Task budget (micro-USD; 1 USD = 1000000)', 'number', '0'); approvals.append(ui.budget.wrap); ui.approvals = [];
    for (const endpoint of [...new Map(models.filter(m => !m.local).map(m => [m.endpoint_id, m])).values()]) {
      const box = element('fieldset'); box.append(element('legend', endpoint.label || endpoint.endpoint_id));
      const checked = checkbox('Approve this endpoint for this task');
      const budget = input('Endpoint limit (micro-USD)', 'number', '');
      const inputRate = input('Input price: micro-USD / 1M tokens', 'number', '');
      const outputRate = input('Output price: micro-USD / 1M tokens', 'number', '');
      const context = input('Context explicitly approved for external transmission', 'textarea');
      const scope = { wrap: uiElement('label', 'External data scope', 'team-field'), control: element('select') };
      bindUiText(scope.control, 'External data scope', 'aria-label');
      scope.control.append(uiOption('Goal only — planning; no history, tools or files', 'goal_only'),
        uiOption('Assigned context — own task history and allowed tool/file excerpts', 'assigned_context'));
      scope.wrap.append(scope.control);
      box.append(checked.wrap, budget.wrap, inputRate.wrap, outputRate.wrap, scope.wrap,
        uiElement('p', 'Goal only may plan, but cannot execute, review or finalize. Assigned context allows those roles; the parent chat is never included. Both scopes require your statement below.'), context.wrap);
      box.append(button('Approve for running task', event => act(async () => {
        if (!teamId || !checked.control.checked) throw new Error('Select explicit endpoint consent for an existing team.');
        const limit = Number(budget.control.value), inRate = inputRate.control.value === '' ? null : Number(inputRate.control.value), outRate = outputRate.control.value === '' ? null : Number(outputRate.control.value);
        if (!safeInteger(limit) || !limit || !safeInteger(inRate) || !safeInteger(outRate) || !context.control.value.trim()) throw new Error('Consent requires known prices, a positive limit, and an approved context statement.');
        await post(`/api/team/${encode(teamId)}/approvals`, { endpoint_id: endpoint.endpoint_id, limit_microusd: limit,
          input_rate_per_million: inRate, output_rate_per_million: outRate, approved_context: context.control.value, data_scope: scope.control.value, consent: true });
        await loadSnapshot();
      }, event.currentTarget)), button('Revoke approval', event => act(async () => {
        if (!teamId) throw new Error('No team task is selected.');
        await post(`/api/team/${encode(teamId)}/approvals/revoke`, { endpoint_id: endpoint.endpoint_id }); checked.control.checked = false; await loadSnapshot();
      }, event.currentTarget))); approvals.append(box);
      ui.approvals.push({ endpoint, checked, budget, inputRate, outputRate, context, scope });
    }
    ui.start.append(button('Start team', event => act(start, event.currentTarget))); panel.append(ui.start, approvals);
    ui.controls = element('div', '', 'team-actions');
    for (const action of ['pause', 'resume', 'cancel']) ui.controls.append(button(action, event => act(async () => {
      if (action === 'cancel' && !confirmImpl('Cancel this team task and its workers?')) return;
      await post(`/api/team/${encode(teamId)}/${action}`); await loadSnapshot();
    }, event.currentTarget)));
    panel.append(ui.controls); ui.guidance = input('Additional guidance while the team is running', 'textarea');
    ui.runningConfig = element('details'); ui.runningConfig.append(uiElement('summary', 'Running task permissions and scheduling'));
    ui.runningToggles = {}; ui.configDirty = false;
    for (const [key, source] of Object.entries(ui.toggles)) {
      const item = checkbox(uiTextSource(source.wrap), false); ui.runningToggles[key] = item; ui.runningConfig.append(item.wrap);
      item.control.addEventListener('change', () => { ui.configDirty = true; });
      if (key === 'trusted_host' && !hostEnabled) item.control.disabled = true;
    }
    ui.runningConfig.append(button('Apply task permissions', event => act(async () => {
      if (!teamId) throw new Error('No team task is selected.');
      const config = Object.fromEntries(Object.entries(ui.runningToggles).map(([key, item]) => [key, item.control.checked]));
      await post(`/api/team/${encode(teamId)}/config`, config); ui.configDirty = false; await loadSnapshot();
    }, event.currentTarget))); panel.append(ui.runningConfig);
    panel.append(ui.guidance.wrap, button('Send guidance', event => act(async () => {
      if (!teamId || !ui.guidance.control.value.trim()) throw new Error('Enter guidance for an existing team.');
      await post(`/api/team/${encode(teamId)}/guidance`, { message: ui.guidance.control.value }); ui.guidance.control.value = '';
    }, event.currentTarget)));
    ui.summary = element('pre', '', 'team-output'); bindUiText(ui.summary, 'Team result and integration workspace', 'aria-label'); panel.append(ui.summary);
    ui.evidence = element('div'); ui.evidence.append(uiElement('h4', 'Results and recovery'),
      button('Refresh results and uncertain actions', () => act(loadEvidence)));
    ui.intents = element('div', '', 'team-task-list'); ui.artifacts = element('div', '', 'team-task-list');
    ui.evidence.append(ui.intents, ui.artifacts); panel.append(ui.evidence); intentSignature = '';
    ui.taskList = element('div', '', 'team-task-list'); panel.append(ui.taskList);
  }
  function updateSetupMode() {
    const basic = ui.setupMode.value === 'basic';
    ui.advanced.hidden = basic;
    ui.basicPool.hidden = !basic;
    if (ui.workerAdvanced) ui.workerAdvanced.hidden = basic;
    if (ui.workerBasicHint) ui.workerBasicHint.hidden = !basic;
    // Hide scheduling controls only. Permission switches remain reachable and
    // changing presentation never grants access or discards advanced drafts.
    for (const key of ['auto_dispatch', 'auto_continue']) ui.toggles[key].wrap.hidden = basic;
  }
  async function loadProjectProfiles() {
    const token = generation, select = ui.profileSelect;
    const data = await request('/api/team/profiles');
    if (disposed || token !== generation || ui.profileSelect !== select) return;
    profiles = data.profiles || [];
    const selected = select.value; select.replaceChildren(uiOption('Choose saved project profile', ''));
    for (const profile of profiles) select.append(new Option(profile.name, profile.name));
    select.value = selected;
  }
  function buildTeam() {
    const panel = panels.get('Team').panel;
    ui.workerBasicHint = uiElement('p', 'Choose worker models in Tasks. The leader will plan and distribute the work. Switch to Advanced setup to configure individual assignments.');
    ui.workerAdvanced = element('div');
    panel.append(ui.workerBasicHint, ui.workerAdvanced);
    ui.workerAdvanced.append(uiElement('p', 'Choose workers explicitly, or let the leader dispatch them after Start. External routes always require prior consent and known prices.'));
    ui.workerName = input('Worker name'); ui.workerRole = { wrap: uiElement('label', 'Role', 'team-field'), control: element('select') };
    bindUiText(ui.workerRole.control, 'Role', 'aria-label');
    for (const role of ['executor', 'reviewer', 'researcher']) ui.workerRole.control.append(bindUiText(new Option(role, role), role));
    ui.workerRole.wrap.append(ui.workerRole.control); ui.workerRoute = routeSelect();
    ui.workerGoal = input('Objective', 'textarea'); ui.workerAcceptance = input('Acceptance criteria', 'textarea');
    ui.workerDependencies = input('Dependency worker IDs (comma separated)'); ui.workerScope = input('Writable paths (comma separated)');
    ui.workerAdvanced.append(ui.workerName.wrap, ui.workerRole.wrap, ui.workerRoute, ui.workerGoal.wrap, ui.workerAcceptance.wrap, ui.workerDependencies.wrap, ui.workerScope.wrap,
      button('Add worker', event => act(async () => {
        const worker = { ...selectedRoute(ui.workerRoute), name: ui.workerName.control.value, role: ui.workerRole.control.value,
          objective: ui.workerGoal.control.value, acceptance: ui.workerAcceptance.control.value,
          depends_on: list(ui.workerDependencies.control.value), write_scope: list(ui.workerScope.control.value) };
        if (!worker.model || !worker.objective.trim()) throw new Error('Worker needs a model and objective.');
        if (teamId) { await post(`/api/team/${encode(teamId)}/workers`, worker); await loadSnapshot(); }
        else { manualWorkers.push(worker); renderWorkers(); }
      }, event.currentTarget)));
    ui.workerList = element('div', '', 'team-task-list'); panel.append(ui.workerList);
    updateSetupMode();
  }
  function buildTerminals() {
    const panel = panels.get('Terminals').panel;
    panel.append(uiElement('p', hostEnabled ? 'Interactive host terminal. Output is displayed as plain text; commands run only when submitted.' : 'Host execution is disabled by the server.'));
    ui.terminalSelect = element('select'); bindUiText(ui.terminalSelect, 'Terminal', 'aria-label');
    ui.terminalSelect.addEventListener('change', () => { selectedTerminal = ui.terminalSelect.value; renderTerminal(); pollTerminal(); });
    panel.append(ui.terminalSelect, button('New terminal', event => act(async () => {
      const token = generation, scope = ui.hostScope.value;
      const result = await hostCreate('terminal.create', { cwd: projectPath(), cols: 100, rows: 30 });
      if (!current(token) || ui.hostScope.value !== scope) return;
      selectedTerminal = result.id; await refreshTerminals(); await pollTerminal();
    }, event.currentTarget)), button('Refresh terminals', () => act(refreshTerminals)));
    ui.terminalOutput = element('pre', '', 'team-output team-terminal'); bindUiText(ui.terminalOutput, 'Terminal output', 'aria-label'); panel.append(ui.terminalOutput);
    ui.terminalInput = input('Terminal input', 'textarea'); panel.append(ui.terminalInput.wrap,
      button('Send input', event => act(async () => { await host('terminal.input', { id: selectedTerminal, data: ui.terminalInput.control.value + '\n' }); ui.terminalInput.control.value = ''; await pollTerminal(); }, event.currentTarget)));
    const actions = element('div', '', 'team-actions');
    for (const [label, op] of [['Ctrl+C', 'terminal.interrupt'], ['Close terminal', 'terminal.stop']]) actions.append(button(label, event => act(async () => { await host(op, { id: selectedTerminal }); await pollTerminal(); }, event.currentTarget)));
    ui.cols = input('Columns', 'number', '100'); ui.rows = input('Rows', 'number', '30');
    actions.append(ui.cols.wrap, ui.rows.wrap, button('Resize', () => act(() => host('terminal.resize', { id: selectedTerminal, cols: Number(ui.cols.control.value), rows: Number(ui.rows.control.value) })))); panel.append(actions);
  }
  function buildFiles() {
    const panel = panels.get('Files & Changes').panel;
    ui.filePath = input('Path relative to project'); panel.append(ui.filePath.wrap,
      button('List directory', () => act(listFiles)), button('Open file', () => act(openFile)), button('Download', () => act(downloadFile)));
    ui.fileTree = element('div', '', 'team-file-tree'); panel.append(ui.fileTree);
    ui.editor = input('File content (UTF-8)', 'textarea'); ui.editor.control.classList.add('team-editor'); panel.append(ui.editor.wrap,
      button('Save file', event => act(async () => {
        const path = ui.filePath.control.value;
        if (!fileLoadedPath || path !== fileLoadedPath || !fileHash) throw new Error('Open this file first to obtain its revision before saving.');
        const bytes = new TextEncoder().encode(ui.editor.control.value);
        clearDiffReview();
        const result = await host('file.upload', { path, cwd: projectPath(), data_base64: toBase64(bytes), expected_sha256: fileHash });
        fileHash = result.sha256 || null;
        if (result.checkpoint_id) ui.fileCheckpointStatus.textContent = `File checkpoint: ${result.checkpoint_id}. Use List file checkpoints to inspect and restore.`;
      }, event.currentTarget)));
    ui.upload = input('Upload file', 'file'); panel.append(ui.upload.wrap, button('Upload to selected path', event => act(async () => {
      const file = ui.upload.control.files?.[0]; if (!file) throw new Error('Choose a file.');
      if (!ui.filePath.control.value) throw new Error('Enter an explicit destination path.');
      if (file.size > 2 * 1024 * 1024) throw new Error('This UI accepts files up to 2 MiB.');
      if (!confirmImpl('Upload to the selected host path? Existing content may be replaced.')) return;
      clearDiffReview();
      const result = await host('file.upload', { path: ui.filePath.control.value, cwd: projectPath(), data_base64: toBase64(new Uint8Array(await file.arrayBuffer())),
        ...(fileLoadedPath === ui.filePath.control.value && fileHash ? { expected_sha256: fileHash } : {}) });
      if (result.checkpoint_id) ui.fileCheckpointStatus.textContent = `File checkpoint: ${result.checkpoint_id}. Use List file checkpoints to inspect and restore.`;
      ui.upload.control.value = ''; await listFiles();
    }, event.currentTarget)));
    panel.append(uiElement('p', 'For an existing upload target, open the file first so replacement is revision-checked.'));
    ui.fileCheckpointStatus = element('p'); bindUiText(ui.fileCheckpointStatus, 'Last file checkpoint', 'aria-label');
    ui.fileCheckpointList = element('div', '', 'team-task-list');
    panel.append(uiElement('h4', 'File checkpoints'), ui.fileCheckpointStatus,
      uiElement('p', 'Direct and non-Git edits can be restored from saved checkpoints. Restore uses the recorded after-state hashes; a later file change is refused, never silently overwritten.'),
      button('List file checkpoints', () => act(loadFileCheckpoints)), ui.fileCheckpointList);
    ui.worktree = input('Worker worktree ID'); ui.checkpoint = input('Rollback checkpoint ID'); panel.append(ui.worktree.wrap,
      button('Create isolated worktree', () => act(async () => {
        const token = generation, scope = ui.hostScope.value;
        clearDiffReview();
        const result = await hostCreate('git.worktree.create', { source: projectPath() });
        if (current(token) && ui.hostScope.value === scope) ui.worktree.control.value = result.id || '';
      })),
      button('Show diff', () => act(showReviewedDiff)),
      button('Integrate reviewed diff', () => act(async () => {
        const review = diffState, token = generation, scope = ui.hostScope.value;
        if (!review?.source_tree || !review?.worktree_tree || review.reviewed_id !== ui.worktree.control.value || review.reviewed_scope !== scope) throw new Error('Fetch a diff with source/worktree revision tokens first.');
        const paths = review.files.filter(path => ui.diffChoices.get(path)?.control.checked);
        if (!paths.length) throw new Error('Select at least one reviewed file to integrate.');
        if (!confirmImpl(`Integrate ${paths.length} selected reviewed file(s) into the source project? Unselected files stay in the worktree.`)) return;
        const result = await host('git.integrate', { id: review.reviewed_id, paths, expected_source_tree: review.source_tree, expected_worktree_tree: review.worktree_tree });
        if (!current(token) || ui.hostScope.value !== scope || ui.worktree.control.value !== review.reviewed_id) return;
        ui.checkpoint.control.value = result.checkpoint_id || ''; clearDiffReview();
      })), ui.checkpoint.wrap,
      button('Rollback checkpoint', () => act(async () => {
        if (!ui.checkpoint.control.value || !diffState?.source_tree) throw new Error('Enter the checkpoint and refresh the diff to obtain the current source revision.');
        if (!confirmImpl('Rollback the source project to this checkpoint?')) return;
        await host('git.rollback', { checkpoint_id: ui.checkpoint.control.value, expected_source_tree: diffState.source_tree }); clearDiffReview();
      })));
    ui.diff = element('pre', '', 'team-output'); ui.diffFiles = element('fieldset', '', 'team-file-tree'); ui.diffFiles.hidden = true;
    ui.diffChoices = new Map(); panel.append(ui.diff, ui.diffFiles);
    for (const control of [ui.worktree.control, ui.filePath.control, ui.editor.control]) control.addEventListener('input', clearDiffReview);
  }
  function clearDiffReview() {
    diffRequestSeq++; diffState = null; ui.diffChoices = new Map();
    if (ui.diff) ui.diff.textContent = '';
    if (ui.diffFiles) { ui.diffFiles.replaceChildren(); ui.diffFiles.hidden = true; }
  }
  async function showReviewedDiff() {
    const token = generation, scope = ui.hostScope.value, id = ui.worktree.control.value;
    clearDiffReview(); const requestSeq = diffRequestSeq;
    const data = await host('git.diff', { id });
    if (!current(token) || ui.hostScope.value !== scope || ui.worktree.control.value !== id || diffRequestSeq !== requestSeq) return;
    const files = reviewedGitFiles(data);
    diffState = { ...data, files, reviewed_id: id, reviewed_scope: scope };
    ui.diff.textContent = terminalPlainText(data.patch ?? data.diff ?? data.output ?? '');
    ui.diffFiles.hidden = false; ui.diffFiles.append(uiElement('legend', 'Files to integrate'));
    ui.diffFiles.append(uiElement('p', files.length ? 'All changed files are selected initially. Renames appear as deletion and addition; select both to move a file.' : 'No changed files in this worktree.'));
    for (const path of files) { const choice = checkbox('', true); choice.wrap.append(uiElement('span', 'Include'), document.createTextNode(' ' + path)); ui.diffChoices.set(path, choice); ui.diffFiles.append(choice.wrap); }
  }
  async function loadFileCheckpoints() {
    const token = generation, scope = ui.hostScope.value, container = ui.fileCheckpointList;
    const data = await host('file.checkpoint.list');
    if (!current(token) || ui.hostScope.value !== scope || ui.fileCheckpointList !== container) return;
    container.replaceChildren();
    if (!data.checkpoints?.length) container.append(uiElement('p', 'No file checkpoints in this host scope.'));
    for (const checkpoint of data.checkpoints || []) {
      const card = element('article', '', 'team-card'); card.setAttribute('aria-label', `File checkpoint ${checkpoint.id}`);
      card.append(element('strong', `${checkpoint.id} · ${checkpoint.status}`),
        element('pre', terminalPlainText(JSON.stringify(checkpoint.files, null, 2)), 'team-output'));
      let pinned; try { pinned = fileRollbackArguments(checkpoint); } catch (error) { card.append(element('p', error.message)); }
      if (pinned) card.append(button('Restore file checkpoint', event => act(async () => {
        if (!current(token) || ui.hostScope.value !== scope) throw new Error('Host scope changed. Inspect checkpoints in the selected scope.');
        if (!confirmImpl(`Restore the ${checkpoint.files.length} exact file(s) shown in checkpoint ${checkpoint.id}? Files created by that change will be removed. Later edits cause refusal.`)) return;
        // Deliberately use the displayed checkpoint hashes, never fetch current
        // file hashes to make a stale rollback pass its concurrency guard.
        await host('file.rollback', pinned);
        if (!current(token) || ui.hostScope.value !== scope) return;
        clearDiffReview(); fileHash = null; fileLoadedPath = ''; ui.editor.control.value = '';
        ui.fileCheckpointStatus.textContent = `Restored file checkpoint ${checkpoint.id}. Reopen files before editing.`;
        await loadFileCheckpoints();
      }, event.currentTarget)));
      container.append(card);
    }
  }
  function renderWorkers() {
    if (!ui.workerList) return;
    ui.workerList.replaceChildren(); ui.taskList.replaceChildren();
    const workers = snapshot?.workers || snapshot?.tasks || manualWorkers;
    const previousScope = ui.hostScope.value;
    ui.hostScope.replaceChildren(uiOption('Team host scope', ''));
    for (const worker of workers) if (worker.id) ui.hostScope.append(new Option(worker.name || worker.id, worker.id));
    ui.hostScope.value = previousScope;
    for (const rawWorker of workers) {
      const worker = { ...(rawWorker.profile || {}), ...rawWorker };
      const card = element('article', '', 'team-card');
      const heading = element('strong'), role = worker.role || 'worker';
      heading.append(worker.name || worker.id ? element('span', worker.name || worker.id) : uiElement('span', 'Worker'),
        document.createTextNode(' · '), TEAM_ROLES.has(role) ? uiElement('span', role) : element('span', role));
      const state = element('p'); state.append(teamStatus('span', worker.status || 'planned'), document.createTextNode(' · '), element('span', worker.model || ''));
      card.append(heading, element('p', worker.objective || worker.title || ''), state);
      if (worker.acceptance) { const acceptance = element('p'); acceptance.append(uiElement('span', 'Acceptance:'), document.createTextNode(' ' + worker.acceptance)); card.append(acceptance); }
      if (worker.error || worker.result || worker.blocked_reason) card.append(element('pre', terminalPlainText(worker.error || worker.blocked_reason || (typeof worker.result === 'string' ? worker.result : JSON.stringify(worker.result))), 'team-output'));
      if (worker.status === 'waiting_approval') {
        card.append(uiElement('p', 'Blocked pending human action. No root, external, or destructive authorization is granted by Resume.'));
        const access = uiElement('a', 'Open host access'); access.href = '/host-access'; access.target = '_blank'; access.rel = 'noopener'; card.append(access);
      }
      if (worker.checkpoint_id) { const checkpoint = element('p'); checkpoint.append(uiElement('span', 'Checkpoint:'), document.createTextNode(' ' + worker.checkpoint_id)); card.append(checkpoint); }
      for (const artifact of worker.artifacts || []) {
        const path = typeof artifact === 'string' ? artifact : artifact.path;
        if (path) card.append(artifactButton(path, () => { showTab('Files & Changes'); ui.filePath.control.value = path; act(openFile); }));
      }
      if (teamId && worker.id) {
        const actions = element('div', '', 'team-actions');
        const checkpointOutput = element('pre', '', 'team-output'); checkpointOutput.hidden = true;
        actions.append(button('View checkpoint', event => act(async () => {
          const token = generation, id = teamId;
          const data = await request(`/api/team/${encode(id)}/workers/${encode(worker.id)}/checkpoint`);
          if (!current(token) || teamId !== id) return;
          checkpointOutput.hidden = false; checkpointOutput.textContent = terminalPlainText(JSON.stringify(data.checkpoint || { status: 'No checkpoint yet' }, null, 2));
        }, event.currentTarget)));
        for (const action of ['pause', 'resume', 'cancel', 'accept', 'reject']) actions.append(button(action, event => act(async () => {
          const reason = action === 'reject' ? ui.guidance.control.value.trim() : '';
          if (action === 'reject' && !reason) throw new Error('Enter the rejection reason in the guidance field before rejecting.');
          await post(`/api/team/${encode(teamId)}/workers/${encode(worker.id)}/${action}`, { reason }); await loadSnapshot();
        }, event.currentTarget)));
        const select = routeSelect(worker); actions.append(select, button('Reassign', event => act(async () => {
          const route = selectedRoute(select); if (!route) throw new Error('Choose a replacement endpoint/model.');
          await post(`/api/team/${encode(teamId)}/workers/${encode(worker.id)}/reassign`, route); await loadSnapshot();
        }, event.currentTarget))); card.append(actions, checkpointOutput);
      }
      ui.workerList.append(card);
      const task = element('article', '', 'team-card'); task.append(worker.name || worker.id ? element('strong', worker.name || worker.id) : uiElement('strong', 'Worker'), element('p', worker.objective || ''), teamStatus('span', worker.status || 'planned', 'team-status')); ui.taskList.append(task);
    }
    if (!workers.length) ui.taskList.append(uiElement('p', 'No workers yet. Add them in Team or allow the leader to dispatch.'));
  }
  function applySnapshot(data) {
    snapshot = data; teamId = data.team_id || data.task?.id || null;
    destroyEngineering?.setTaskContext?.(teamId ? { id: teamId, name: data.task?.title || data.title || '',
      workers: (data.workers || data.tasks || []).map(worker => ({ id: worker.id, name: worker.name })) } : null);
    const changes = notificationTracker.observe(teamId, data);
    if (notificationsEnabled && NotificationImpl?.permission === 'granted' && changes.length) {
      const body = [...new Set(changes.map(change => `${change.subject === 'team' ? 'Team' : 'Worker'}: ${change.status.replaceAll('_', ' ')}`))].join('\n');
      try { new NotificationImpl('Odysseus Team', { body, tag: `odysseus-team:${teamId}` }); }
      catch (_) { notice('This browser cannot display notifications; Team status remains available here.', true); }
    }
    ui.status.replaceChildren(teamStatus('span', data.status || data.task?.status || (teamId ? 'Ready' : 'No task')));
    ui.start.hidden = !!teamId; ui.controls.hidden = !teamId;
    ui.runningConfig.hidden = !teamId; ui.evidence.hidden = !teamId;
    const meta = data.metadata || data.task?.metadata || {};
    ui.summary.textContent = terminalPlainText([meta.final_summary || '', meta.integration_path ? `Integration workspace: ${meta.integration_path}` : '',
      meta.workspace?.integration?.id ? `Integration worktree ID: ${meta.workspace.integration.id}` : ''].filter(Boolean).join('\n'));
    if (!ui.configDirty) for (const [key, item] of Object.entries(ui.runningToggles)) item.control.checked = data.config?.[key] === true;
    ui.resources.textContent = JSON.stringify(data.resources || {}, null, 2); renderWorkers();
  }
  async function loadSnapshot() {
    if (!sid) return;
    const token = generation, id = sid, requestSeq = ++snapshotRequestSeq;
    const data = await request(`/api/team/session/${encode(id)}`);
    if (!current(token, id) || requestSeq !== snapshotRequestSeq) return;
    const previousId = teamId; applySnapshot(data);
    if (previousId !== teamId) { source?.close(); source = null; }
    let last = Number(data.last_seq);
    if (!Number.isSafeInteger(last) || last < 0) { try { last = Number(storage?.getItem(cursorKey(teamId))) || 0; } catch (_) { last = 0; } }
    if (teamId && (previousId !== teamId || cursor.needsSnapshot || last > cursor.afterSeq)) {
      cursor.reset(teamId, last); try { storage?.setItem(cursorKey(teamId), String(last)); } catch (_) {}
    }
    if (teamId && !source) connect();
    if (teamId) loadEvidence().catch(error => { if (current(token, id)) notice(error.message, true); });
  }
  async function loadEvidence() {
    if (!teamId) return;
    const token = generation, id = teamId, requestSeq = ++evidenceRequestSeq;
    const [intentData, artifactData] = await Promise.all([request(`/api/team/${encode(id)}/intents`), request(`/api/team/${encode(id)}/artifacts`)]);
    if (!current(token) || teamId !== id || requestSeq !== evidenceRequestSeq) return;
    const unknown = (intentData.intents || []).filter(item => item.status === 'unknown');
    const signature = JSON.stringify(unknown.map(item => [item.id, item.status, item.updated_at]));
    // Preserve a human's inspection draft while ordinary stream snapshots arrive.
    if (signature !== intentSignature) {
      intentSignature = signature; ui.intents.replaceChildren();
      if (unknown.length) ui.intents.append(uiElement('p', 'An action has an uncertain outcome. Inspect its terminal and files first. These controls record what happened; they never retry the action.'));
      for (const intent of unknown) {
        const card = element('article', '', 'team-card');
        const title = element('strong'); title.append(uiElement('span', 'Uncertain action:'), document.createTextNode(' ' + (intent.name || intent.id)));
        card.append(title, element('pre', terminalPlainText(JSON.stringify(intent.payload || {}, null, 2)), 'team-output'));
        card.append(button('Inspect worker terminal', () => { ui.hostScope.value = intent.worker_id || ''; selectedTerminal = ''; terminalState = new Map(); showTab('Terminals'); }));
        const evidence = input('Observed outcome and evidence', 'textarea'), exitCode = input('Observed exit code (required for completed actions)', 'number');
        const confirmed = checkbox('I inspected the outcome; this is not permission to retry');
        card.append(evidence.wrap, exitCode.wrap, confirmed.wrap);
        for (const [label, status] of [['Record completed outcome', 'done'], ['Record that action did not run', 'not_run']]) card.append(button(label, event => act(async () => {
          if (!confirmed.control.checked || !evidence.control.value.trim()) throw new Error(t('Inspect the outcome, record evidence, and confirm before reconciliation.'));
          if (status === 'done' && exitCode.control.value === '') throw new Error(t('Record the actual exit code before marking an action completed.'));
          const result = { output: evidence.control.value.trim() };
          if (exitCode.control.value !== '') {
            const code = Number(exitCode.control.value); if (!Number.isSafeInteger(code)) throw new Error(t('Exit code must be an integer.')); result.exit_code = code;
          }
          if (!confirmImpl(t(status === 'done' ? 'Record this action as completed based on your inspection? No command is retried.' : 'Record this action as not run based on your inspection? No command is retried.'))) return;
          await post(`/api/team/${encode(id)}/intents/${encode(intent.id)}/resolve`, { status, result, confirmation: true }); await loadSnapshot();
        }, event.currentTarget)));
        ui.intents.append(card);
      }
    }
    ui.artifacts.replaceChildren();
    for (const artifact of artifactData.artifacts || []) {
      const card = element('article', '', 'team-card'), data = artifact.data || {};
      card.append(artifact.name ? element('strong', artifact.name) : uiElement('strong', 'Artifact'), element('pre', terminalPlainText(JSON.stringify(data, null, 2)), 'team-output'));
      if (data.path) card.append(artifactButton(data.path, () => {
        ui.hostScope.value = artifact.worker_id || ''; ui.filePath.control.value = data.path; showTab('Files & Changes'); act(openFile);
      }));
      const integration = data.workspace?.integration;
      if (integration?.id) card.append(button('Review integration workspace', () => {
        ui.hostScope.value = ''; ui.worktree.control.value = integration.id; clearDiffReview(); showTab('Files & Changes');
      }));
      ui.artifacts.append(card);
    }
  }
  function connect() {
    if (!teamId || !active || !EventSourceImpl) return;
    source?.close(); const token = generation, id = teamId;
    const stream = new EventSourceImpl(`/api/team/${encode(id)}/events?after_seq=${cursor.afterSeq}`); source = stream;
    const receive = event => {
      if (!current(token) || source !== stream || teamId !== id) return;
      let data; try { data = JSON.parse(event.data); } catch (_) { return; }
      if (!cursor.accept(data)) {
        if (cursor.needsSnapshot) { stream.close(); source = null; loadSnapshot().catch(error => notice(error.message, true)); }
        return;
      }
      try { storage?.setItem(cursorKey(id), String(cursor.afterSeq)); } catch (_) {}
      if (data.type === 'terminal.output') {
        const payload = data.data || data.payload || {}; appendTerminal(payload.id || payload.terminal_id, payload);
      }
      if (data.type === 'worker_metrics') destroyEngineering?.refreshContextObservation?.();
      if (data.type === 'host_changed' && selectedTab === 'Terminals') {
        const payload = data.payload || data.data || {}, scope = ui.hostScope.value || id;
        if (payload.scope === scope && ['terminal.create', 'terminal.stop'].includes(payload.op)) {
          refreshTerminals().catch(error => {
            if (current(token) && teamId === id && (ui.hostScope.value || id) === scope) notice(error.message, true);
          });
        }
      }
      if (data.type === 'host_changed') {
        const payload = data.payload || data.data || {};
        if (payload.scope === (ui.hostScope.value || id) && (String(payload.op).startsWith('file.') || ['git.integrate', 'git.rollback'].includes(payload.op))) clearDiffReview();
      }
      if (!refreshTimer) refreshTimer = setTimeout(() => { refreshTimer = null; loadSnapshot().catch(e => notice(e.message, true)); }, 150);
    };
    stream.onmessage = receive; stream.addEventListener?.('team', receive);
    stream.onopen = () => { if (source === stream && current(token) && teamId === id) destroyEngineering?.refreshContextObservation?.(); };
    stream.onerror = () => {
      if (source !== stream) return; stream.close(); source = null; notice('Reconnecting to team events…');
      retryTimer = setTimeout(() => { if (current(token) && teamId === id) connect(); }, 2000);
    };
  }
  async function start() {
    if (!sid || sid !== getSessionId()) throw new Error('Select or create a chat first.');
    if (teamId) throw new Error('This chat already has a team task.');
    const external_approvals = ui.approvals.filter(x => x.checked.control.checked).map(x => ({ endpoint_id: x.endpoint.endpoint_id,
      limit_microusd: Number(x.budget.control.value), input_rate_per_million: x.inputRate.control.value === '' ? null : Number(x.inputRate.control.value),
      output_rate_per_million: x.outputRate.control.value === '' ? null : Number(x.outputRate.control.value), approved_context: x.context.control.value, data_scope: x.scope.control.value, consent: true }));
    const payload = normalizeTeamStart({ title: ui.title.control.value, goal: ui.goal.control.value, project_path: ui.project.control.value,
      setup_mode: ui.setupMode.value, worker_pool: ui.poolChoices.filter(item => item.control.checked).map(item => item.model),
      leader: selectedRoute(ui.leader), workers: manualWorkers, config: { ...Object.fromEntries(Object.entries(ui.toggles).map(([k, v]) => [k, v.control.checked])), preset: ui.preset.value,
        project_profile: { install_command: ui.installCommand.control.value, run_command: ui.runCommand.control.value,
          test_command: ui.testCommand.control.value, build_command: ui.buildCommand.control.value, constraints: ui.constraints.control.value } },
      budget_microusd: Number(ui.budget.control.value), external_approvals }, models);
    if (selectedEngineeringProject) {
      payload.project_id = selectedEngineeringProject.id;
      payload.project_revision = selectedEngineeringProject.revision;
      payload.project_path = selectedEngineeringProject.root;
    }
    await post(`/api/team/session/${encode(sid)}/start`, payload); await loadSnapshot();
  }
  function showTab(label) {
    selectedTab = label;
    if (label === 'Engineering') destroyEngineering?.refreshContextObservation?.();
    for (const [name, { tab, panel }] of panels) { panel.hidden = name !== label; tab.setAttribute('aria-selected', String(name === label)); tab.classList.toggle('active', name === label); }
    clearInterval(terminalTimer); terminalTimer = null;
    clearInterval(resourceTimer); resourceTimer = null;
    if (label === 'Terminals' && teamId) { act(refreshTerminals); terminalTimer = setInterval(pollTerminal, 1000); }
    if (label === 'Resources' && teamId) { act(loadResources); resourceTimer = setInterval(() => { if (!document.hidden) loadResources().catch(error => notice(error.message, true)); }, 5000); }
  }
  async function loadResources() {
    if (!teamId) return;
    const token = generation, id = teamId; const data = await request(`/api/team/${encode(id)}/resources`);
    if (current(token) && teamId === id) ui.resources.textContent = JSON.stringify(data, null, 2);
  }
  async function refreshTerminals() {
    const token = generation, scope = ui.hostScope.value, requestSeq = ++terminalListRequestSeq;
    const result = await host('terminal.list');
    if (!current(token) || ui.hostScope.value !== scope || requestSeq !== terminalListRequestSeq) return;
    const terminals = Array.isArray(result) ? result : result.terminals || result.jobs || [];
    ui.terminalSelect.replaceChildren(uiOption('Choose terminal', ''));
    for (const terminal of terminals) ui.terminalSelect.append(new Option(`${terminal.id} · ${terminal.status}`, terminal.id));
    ui.terminalSelect.value = selectedTerminal; renderTerminal();
  }
  function appendTerminal(id, data) {
    if (!id) return;
    const state = terminalState.get(id) || { offset: 0, text: '' };
    if (Number.isFinite(data.next_offset) && data.next_offset <= state.offset) return;
    state.text = (state.text + (data.truncated ? '\n[Earlier terminal output truncated]\n' : '') + terminalPlainText(data.output || '')).slice(-TERMINAL_TEXT_LIMIT);
    state.offset = Number(data.next_offset ?? state.offset); state.status = data.status;
    terminalState.set(id, state); if (selectedTerminal === id) renderTerminal();
  }
  function renderTerminal() {
    if (!ui.terminalOutput) return;
    const state = terminalState.get(selectedTerminal); ui.terminalOutput.textContent = state?.text || '';
    ui.terminalOutput.scrollTop = ui.terminalOutput.scrollHeight;
  }
  async function pollTerminal() {
    if (terminalBusy || !active || selectedTab !== 'Terminals' || !selectedTerminal || document.hidden) return;
    const token = generation, id = selectedTerminal; terminalBusy = true;
    try { const data = await host('terminal.poll', { id, offset: terminalState.get(id)?.offset || 0, limit: 60000 }); if (current(token)) appendTerminal(id, data); }
    catch (error) { if (current(token)) notice(error.message, true); }
    finally { terminalBusy = false; }
  }
  const toBase64 = bytes => { let text = ''; for (let i = 0; i < bytes.length; i += 32768) text += String.fromCharCode(...bytes.subarray(i, i + 32768)); return btoa(text); };
  const fromBase64 = value => Uint8Array.from(atob(value), char => char.charCodeAt(0));
  async function listFiles() {
    const token = generation, scope = ui.hostScope.value; const data = await host('file.call', { tool: 'ls', content: { path: ui.filePath.control.value || '.' }, cwd: projectPath() });
    if (!current(token) || ui.hostScope.value !== scope) return; ui.fileTree.replaceChildren(element('pre', terminalPlainText(data.output || JSON.stringify(data)), 'team-output'));
    for (const entry of data.entries || []) ui.fileTree.append(button(entry.name, () => { ui.filePath.control.value = entry.path || entry.name; act(entry.is_dir ? listFiles : openFile); }, 'memory-toolbar-btn', false));
  }
  async function openFile() {
    clearDiffReview();
    const token = generation, path = ui.filePath.control.value, scope = ui.hostScope.value;
    const data = await host('file.download', { path, cwd: projectPath() }); if (!current(token) || ui.hostScope.value !== scope || ui.filePath.control.value !== path) return;
    const bytes = fromBase64(data.data_base64 || '');
    ui.editor.control.value = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    fileHash = data.sha256 || null; fileLoadedPath = path;
    if (!fileHash) notice('File opened read-only: server did not supply a revision token.', true);
  }
  async function downloadFile() {
    const token = generation, path = ui.filePath.control.value;
    const data = await host('file.download', { path, cwd: projectPath() }); if (!current(token)) return;
    const url = URL.createObjectURL(new Blob([fromBase64(data.data_base64 || '')]));
    const link = element('a'); link.href = url; link.download = path.split('/').pop() || 'download'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function syncSession() {
    if (!active) return;
    const next = getSessionId() || null; if (sid === next) return;
    generation++; disconnect(); sid = next; teamId = null; snapshot = null; manualWorkers = [];
    notificationTracker.reset();
    terminalState = new Map(); selectedTerminal = ''; fileHash = null; fileLoadedPath = ''; clearDiffReview();
    cursor = createTeamEventCursor(null); build();
    destroyEngineering?.setSessionId?.(sid);
    if (!sid) { notice('Select an existing chat before starting a team.'); return; }
    await loadSnapshot().catch(error => notice(error.message, true));
  }
  function setActive(value, restoreMode = true) {
    if (!enabled && value) return;
    if (active === !!value) return;
    if (value) {
      previousMode = ['mode-agent-btn', 'mode-chat-btn'].find(id => document.getElementById(id)?.classList.contains('active')) || 'mode-agent-btn';
      for (const id of ['mode-agent-btn', 'mode-chat-btn']) {
        const node = document.getElementById(id); node?.classList.remove('active'); node?.setAttribute('aria-pressed', 'false');
      }
    } else if (restoreMode && previousMode) {
      const node = document.getElementById(previousMode); node?.classList.add('active'); node?.setAttribute('aria-pressed', 'true');
    }
    active = !!value; root.hidden = !active; modeButton.setAttribute('aria-pressed', String(active)); modeButton.classList.toggle('active', active);
    document.body.classList.toggle('team-workspace-active', active);
    modeButton.closest('.mode-toggle')?.classList.toggle('mode-third', active);
    if (active) { sid = null; syncSession(); sessionTimer = setInterval(syncSession, 500); }
    else { generation++; disconnect(); clearInterval(sessionTimer); sessionTimer = null; }
  }
  function blockChatSubmit(event) {
    if (!active) return false;
    event?.preventDefault(); event?.stopImmediatePropagation(); notice('Use Start team or Send guidance in the Team panel.'); return true;
  }
  async function init() {
    if (!root || !modeButton) return false;
    modeButton.hidden = true; root.hidden = true;
    try {
      const capability = await request('/api/team/capabilities'); if (capability.enabled !== true || disposed) return false;
      const [modelData, presetData] = await Promise.all([request('/api/team/models'), request('/api/team/presets')]);
      if (disposed) return false; models = modelData.models || []; presets = presetData.presets || [];
      enabled = true; hostEnabled = capability.host_enabled === true; modeButton.hidden = false;
      engineeringEnabled = capability.engineering_enabled === true;
      modeButton.closest('.mode-toggle')?.classList.add('mode-toggle-three');
      modeButton.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); setActive(!active); });
      for (const id of ['mode-agent-btn', 'mode-chat-btn']) document.getElementById(id)?.addEventListener('click', () => setActive(false, false));
      document.getElementById('chat-form')?.addEventListener('submit', blockChatSubmit, true);
      build(); return true;
    } catch (_) { return false; } // Fail closed; ordinary chat remains available.
  }
  function dispose() { destroyEngineering?.(); destroyEngineering = null; disposed = true; setActive(false); enabled = false; if (modeButton) modeButton.hidden = true; }
  return { init, dispose, isActive: () => active, setActive, blockChatSubmit, syncSession, loadSnapshot,
    getState: () => ({ enabled, active, session_id: sid, team_id: teamId, after_seq: cursor.afterSeq }) };
}
