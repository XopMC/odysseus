// Engineering settings and explicitly confirmed, bounded synthetic model probes.
// Explicit approved-check launches are separate from synthetic model probes;
// probe results never grant execution permissions.
import { bindUiText, t as translateUiText } from './i18n.js';
import { parseContextProfile, serializeContextProfile, CONTEXT_PROFILE_MAX_BYTES } from './context-profile.js';
const API = '/api/team/engineering';
// UI metadata mirrors the existing ContextPolicy fields; server validation is
// authoritative. Unknown future settings are not invented or submitted.
const CONTEXT_FIELDS = [
  ['auto_compact', 'Auto compact context', true],
  ['requested_window', 'Requested context window', 0, 0, 2097152],
  ['output_reserve', 'Output reserve', 4096, 256, 131072],
  ['trigger_percent', 'Trigger percentage', 75, 10, 95],
  ['target_percent', 'Target percentage', 50, 5, 90],
  ['safety_tokens', 'Safety reserve in tokens', 1024, 0, 131072, true],
  ['safety_percent', 'Safety reserve percentage', 5, 0, 50, true],
  ['recent_groups', 'Recent message groups to retain', 4, 0, 100, true],
  ['recent_tokens', 'Recent tokens to retain', 2048, 0, 131072, true],
  ['summary_tokens', 'Summary token limit', 1200, 128, 32768, true],
  ['summary_timeout_seconds', 'Summary timeout in seconds', 600, 600, 1800, true],
];
let mountSequence = 0;

export function mountEngineeringWorkspace(root, { request, onProjectSelected = () => {}, contextOnly = false, sessionId = '' }) {
  if (!root?.ownerDocument || typeof request !== 'function' || typeof onProjectSelected !== 'function') throw new TypeError('Engineering requires a root and request/selection callback functions');
  const doc = root.ownerDocument, prefix = `engineering-${++mountSequence}`;
  let disposed = false, capabilities = null, hosts = [], projects = [], selectedId = '', taskContext = null, chatId = sessionId;
  let loading = false, creating = false, applying = false, toolsGeneration = 0, discoveryGeneration = 0;
  const staleProjects = new Set();
  const el = (tag, text, key, className) => {
    const node = doc.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (key) node.dataset.engineering = key;
    if (className) node.className = className;
    return node;
  };
  const button = (label, key) => {
    const node = bindUiText(el('button', label, key, 'memory-toolbar-btn'), label); node.type = 'button'; return node;
  };
  const option = (value, label, translate = false) => { const node = el('option', label); node.value = value; return translate ? bindUiText(node, label) : node; };
  const uiEl = (tag, label, key, className) => bindUiText(el(tag, label, key, className), label);
  const field = (label, control, key) => {
    const wrap = el('div', undefined, undefined, 'team-field');
    control.id = `${prefix}-${key}`;
    const title = bindUiText(el('label', label), label); title.htmlFor = control.id; wrap.append(title, control); return wrap;
  };
  const feature = name => capabilities?.enabled === true && capabilities.features?.[name] === true;
  const current = () => projects.find(project => String(project.id) === selectedId);
  const message = error => typeof error?.detail === 'string' ? error.detail : error?.message || 'Request failed. Try refreshing after checking the connection.';
  const notice = (text, error = false, raw = false) => { status.replaceChildren(raw ? el('span', text) : uiEl('span', text)); status.className = `team-notice${error ? ' team-error' : ''}`; };

  const section = el('section', undefined, 'workspace', 'engineering-workspace team-panel');
  const heading = el('div', undefined, undefined, 'team-heading');
  const title = uiEl('h3', 'Engineering'); title.id = `${prefix}-title`; section.setAttribute('aria-labelledby', title.id);
  heading.append(title, uiEl('span', 'Foundation', 'stage', 'team-status'));
  const description = uiEl('p', 'Engineering workspace: register projects, choose a host policy, inspect tools and language servers, configure verified checks and context policy. Isolated execution is limited to approved checks in a verification copy.', undefined, 'engineering-description');
  const bindingNotice = uiEl('p', 'The selected Engineering project applies to NEW Team runs only. Existing and legacy tasks keep their current settings. Choose Legacy / no Engineering project to start without this binding.', 'binding-notice');
  const status = el('p', undefined, 'notice', 'team-notice'); status.append(uiEl('span', 'Loading engineering capabilities…')); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const refresh = button('Refresh projects and hosts', 'refresh');
  const content = el('div', undefined, 'content', 'team-panel'); content.hidden = true;

  const createForm = el('form', undefined, 'create-form', 'team-card team-panel');
  createForm.append(uiEl('h4', 'Create a project'), uiEl('p', 'Choose an existing folder on a configured host. Registration does not create a folder or grant execution access.'));
  const nameInput = el('input', undefined, 'name'), rootInput = el('input', undefined, 'root'), hostSelect = el('select', undefined, 'host');
  nameInput.type = 'text'; nameInput.required = true; nameInput.maxLength = 200;
  rootInput.type = 'text'; rootInput.required = true; rootInput.placeholder = '/absolute/project/path';
  hostSelect.required = true; hostSelect.append(option('', 'Choose a host', true));
  const fields = el('div', undefined, undefined, 'team-config');
  fields.append(field('Project name', nameInput, 'name'), field('Host', hostSelect, 'host'), field('Absolute project folder', rootInput, 'root'));
  const createButton = button('Create read-only project', 'create'); createButton.type = 'submit';
  createForm.append(fields, createButton);

  const projectCard = el('div', undefined, undefined, 'team-card team-panel');
  const projectSelect = el('select', undefined, 'project');
  const projectDetails = el('div', undefined, 'project-details', 'engineering-project-details');
  projectCard.append(field('Selected project', projectSelect, 'project'), projectDetails);

  const policyForm = el('form', undefined, 'policy-form', 'team-card team-panel');
  policyForm.append(uiEl('h4', 'Host access policy'), uiEl('p', 'Trusted-host access permits tools to act on the real host under its configured permissions. It is not a sandbox. Review the selected host and folder before confirming.'));
  const policyMode = el('select', undefined, 'policy-mode');
  policyMode.append(option('', 'Choose an access mode', true), option('trusted_host', 'Trusted host — explicit confirmation required', true));
  const isolated = option('isolated', 'Isolated — verified runner required', true); isolated.disabled = true; isolated.dataset.engineering = 'isolated-option'; policyMode.append(isolated);
  const consent = el('input', undefined, 'consent'); consent.type = 'checkbox'; consent.id = `${prefix}-consent`;
  const consentLabel = el('label', undefined, undefined, 'team-check'); consentLabel.htmlFor = consent.id;
  consentLabel.append(consent, uiEl('span', 'I trust this host and approve access for the selected project.'));
  const policyButton = button('Confirm trusted-host access', 'apply-policy'); policyButton.type = 'submit';
  policyForm.append(field('Access mode', policyMode, 'policy-mode'), consentLabel, policyButton);

  const toolCard = el('div', undefined, undefined, 'team-card team-panel');
  const toolList = el('div', undefined, 'tools', 'engineering-tools'); toolList.setAttribute('aria-live', 'polite');
  toolCard.append(uiEl('h4', 'Tool catalog'), uiEl('p', 'Availability is reported by the server for the selected project. This catalog does not execute tools.'), toolList);
  const probe = createProbePanel();
  const contextPolicy = createContextPolicyPanel();
  let checkRuns = null;
  const checkProfiles = createCheckProfilesPanel();
  checkRuns = createCheckRunsPanel();
  const requirements = createRequirementsPanel();
  const baselineComparison = createBaselineComparisonPanel();
  const projectMemory = createProjectMemoryPanel();
  const lsp = createLspPanel();
  const mcpReviews = createMcpReviewsPanel();
  content.append(createForm, projectCard, lsp.panel, checkProfiles.panel, checkRuns.panel, requirements.panel, projectMemory.panel, baselineComparison.panel, policyForm, toolCard, mcpReviews.panel, probe.panel, contextPolicy.panel);
  section.append(heading, description, bindingNotice, status, refresh, content); root.append(section);
  if (contextOnly) {
    content.replaceChildren(contextPolicy.panel);
    heading.hidden = true; description.hidden = true; bindingNotice.hidden = true;
    bindUiText(refresh, 'Reload context policy');
  }

  function createLspPanel() {
    let enabled = false, projectId = '', generation = 0, loadingLsp = false;
    const panel = el('section', undefined, 'lsp', 'team-card team-panel'); panel.hidden = true;
    const details = el('div', undefined, 'lsp-results', 'engineering-tools');
    const status = el('p', undefined, 'lsp-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const refreshLsp = button('Inspect language servers', 'lsp-refresh');
    panel.append(uiEl('h4', 'Code navigation'), uiEl('p', 'Checks the selected execution host for language servers. Discovery is read-only; starting a server or requesting code data requires the project execution policy.'), refreshLsp, status, details);
    const controls = () => { refreshLsp.disabled = !enabled || !projectId || loadingLsp; };
    const say = (text, error = null) => { status.replaceChildren(uiEl('span', text)); if (error) status.append(el('span', `: ${message(error)}`)); status.className = `team-notice${error ? ' team-error' : ''}`; };
    async function load() {
      if (disposed || !enabled || !projectId || loadingLsp) return;
      const token = ++generation, id = projectId; loadingLsp = true; controls(); say('Inspecting language servers…'); details.replaceChildren();
      try {
        const data = await request(`${API}/projects/${encodeURIComponent(id)}/lsp/discover`, { method: 'POST', body: {} });
        if (disposed || token !== generation || id !== projectId) return;
        if (!Array.isArray(data?.languages)) throw new Error('Invalid language-server response');
        for (const item of data.languages) {
          if (typeof item?.language !== 'string' || typeof item?.available !== 'boolean') continue;
          const row = el('article', undefined, undefined, 'team-card');
          row.append(el('strong', item.language), uiEl('p', item.available ? 'Available' : 'Unavailable'));
          if (!item.available) row.append(el('p', typeof item.reason === 'string' ? item.reason : 'No language-server availability reason was provided.'));
          details.append(row);
        }
        if (!details.children.length) details.append(uiEl('p', 'No language-server information was returned by the host.'));
        say('Language-server discovery completed.');
      } catch (error) { if (!disposed && token === generation && id === projectId) say('Unable to inspect language servers.', error); }
      finally { if (!disposed && token === generation) { loadingLsp = false; controls(); } }
    }
    refreshLsp.addEventListener('click', () => void load());
    return {
      panel,
      setEnabled(value) { enabled = value === true; panel.hidden = !enabled; if (!enabled) { ++generation; details.replaceChildren(); status.replaceChildren(); } controls(); },
      selectionChanged() { projectId = String(current()?.id || ''); ++generation; details.replaceChildren(); status.replaceChildren(); controls(); },
      destroy() { ++generation; },
    };
  }

  function createProjectMemoryPanel() {
    let enabled = false, projectId = '', loading = false, saving = false, generation = 0, items = [];
    let selected = null;
    const panel = el('section', undefined, 'project-memory', 'team-card team-panel'); panel.hidden = true;
    const select = el('select', undefined, 'memory-select');
    const kind = el('select', undefined, 'memory-kind');
    for (const value of ['architecture', 'verified_command', 'known_problem', 'hypothesis', 'rejected_hypothesis', 'constraint', 'preference']) kind.append(option(value, value.replace(/_/g, ' ')));
    const state = el('select', undefined, 'memory-state');
    for (const value of ['proposed', 'verified', 'stale']) state.append(option(value, value));
    const source = el('input', undefined, 'memory-source'); source.type = 'text';
    const text = el('textarea', undefined, 'memory-text'); text.rows = 5;
    const confirm = el('input', undefined, 'memory-confirm'); confirm.type = 'checkbox'; confirm.id = `${prefix}-memory-confirm`;
    const confirmation = el('label', undefined, undefined, 'team-check'); confirmation.htmlFor = confirm.id; confirmation.append(confirm, uiEl('span', 'I confirm this exact project memory and source.'));
    const refreshMemory = button('Refresh project memory', 'memory-refresh'), saveMemory = button('Save project memory', 'memory-save'), deleteMemory = button('Forget project memory', 'memory-delete'); saveMemory.type = 'button'; deleteMemory.type = 'button';
    const status = el('p', '', 'memory-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    panel.append(uiEl('h4', 'Project memory'), uiEl('p', 'Store project-specific facts with a source and review state. This list is not automatically sent to external models.'), field('Saved memory', select, 'memory-select'), field('Kind', kind, 'memory-kind'), field('State', state, 'memory-state'), field('Source', source, 'memory-source'), field('Memory', text, 'memory-text'), confirmation, el('div', undefined, undefined, 'team-actions', refreshMemory, saveMemory, deleteMemory), status);
    const say = (label, error = null) => { status.replaceChildren(uiEl('span', label)); if (error) status.append(el('span', `: ${message(error)}`)); status.className = `team-notice${error ? ' team-error' : ''}`; };
    const controls = () => { const unavailable = !enabled || !projectId || loading || saving; [select, kind, state, source, text, confirm].forEach(node => { node.disabled = unavailable; }); refreshMemory.disabled = unavailable; saveMemory.disabled = unavailable || !confirm.checked || !text.value.trim() || !source.value.trim(); deleteMemory.disabled = unavailable || !selected || !confirm.checked; };
    const newDraft = () => { selected = null; kind.value = 'architecture'; state.value = 'proposed'; source.value = ''; text.value = ''; confirm.checked = false; };
    function render() { select.replaceChildren(option('', 'New project memory', true), ...items.map(item => option(item.id, `${item.kind}: ${item.text.slice(0, 96)}`))); select.value = selected?.id || ''; if (selected) { kind.value = selected.kind; state.value = selected.state; source.value = selected.source; text.value = selected.text; } controls(); }
    async function load() { if (disposed || !enabled || !projectId || loading || saving) return; const token = ++generation, id = projectId; loading = true; controls(); say('Loading project memory…'); try { const data = await request(`${API}/projects/${encodeURIComponent(id)}/memory?limit=100`, { method: 'GET' }); if (disposed || token !== generation || id !== projectId) return; if (!Array.isArray(data?.items)) throw new Error('Invalid project-memory response'); items = data.items.filter(item => item && typeof item.id === 'string' && typeof item.text === 'string' && Number.isInteger(item.revision)); selected = selected && items.find(item => item.id === selected.id) || null; render(); say('Project memory loaded.'); } catch (error) { if (!disposed && token === generation) say('Unable to load project memory.', error); } finally { if (!disposed && token === generation) { loading = false; controls(); } } }
    select.addEventListener('change', () => { selected = items.find(item => item.id === select.value) || null; if (!selected) newDraft(); confirm.checked = false; render(); });
    [kind, state, source, text].forEach(node => node.addEventListener('input', () => { confirm.checked = false; controls(); })); confirm.addEventListener('change', controls);
    refreshMemory.addEventListener('click', () => void load());
    saveMemory.addEventListener('click', async () => { if (saveMemory.disabled) return; saving = true; controls(); say('Saving project memory…'); try { const saved = await request(`${API}/projects/${encodeURIComponent(projectId)}/memory`, { method: 'POST', body: { memory_id: selected?.id || '', kind: kind.value, state: state.value, source: source.value.trim(), text: text.value.trim(), expected_revision: selected?.revision || 0, confirmation: true } }); selected = saved; items = [...items.filter(item => item.id !== saved.id), saved]; confirm.checked = false; render(); say('Project memory saved.'); } catch (error) { say('Unable to save project memory. Refresh and review the current revision.', error); } finally { saving = false; controls(); } });
    deleteMemory.addEventListener('click', async () => { if (deleteMemory.disabled || !selected) return; saving = true; controls(); say('Forgetting project memory…'); try { await request(`${API}/projects/${encodeURIComponent(projectId)}/memory/${encodeURIComponent(selected.id)}`, { method: 'DELETE', body: { expected_revision: selected.revision, confirmation: true } }); items = items.filter(item => item.id !== selected.id); newDraft(); render(); say('Project memory removed.'); } catch (error) { say('Unable to remove project memory. Refresh and review the current revision.', error); } finally { saving = false; controls(); } });
    return { panel, setEnabled(value) { enabled = value === true; panel.hidden = !enabled; if (enabled && projectId) void load(); controls(); }, selectionChanged() { projectId = String(current()?.id || ''); ++generation; items = []; newDraft(); if (enabled && projectId) void load(); else { status.replaceChildren(); render(); } }, destroy() { ++generation; } };
  }

  function createMcpReviewsPanel() {
    let enabled = false, loadingReviews = false, saving = false, generation = 0;
    const panel = el('section', undefined, 'mcp-reviews', 'team-card team-panel'); panel.hidden = true;
    const refreshReviews = button('Refresh reviewed MCP tools', 'mcp-refresh');
    const status = el('p', '', 'mcp-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const rows = el('div', undefined, 'mcp-tools', 'engineering-tools');
    panel.append(uiEl('h4', 'Reviewed MCP tools'), uiEl('p', 'Review one exact tool schema before enabling it for selected Team roles. Only public or brokered network reads are supported; this does not grant host access or execute a tool.'), refreshReviews, status, rows);
    const say = (label, error = null) => { status.replaceChildren(uiEl('span', label)); if (error) status.append(el('span', `: ${message(error)}`)); status.className = `team-notice${error ? ' team-error' : ''}`; };
    const controls = () => { refreshReviews.disabled = !enabled || loadingReviews || saving; };
    const valid = row => row && typeof row.tool_id === 'string' && typeof row.schema_digest === 'string' && /^[a-f0-9]{64}$/.test(row.schema_digest) && typeof row.available === 'boolean' && row.schema && typeof row.schema === 'object';
    function render(data) {
      rows.replaceChildren();
      const policies = new Map((Array.isArray(data.reviews) ? data.reviews : []).filter(item => item && typeof item.tool_id === 'string').map(item => [item.tool_id, item]));
      const catalogue = Array.isArray(data.catalogue) ? data.catalogue : [];
      if (!catalogue.length) { rows.append(uiEl('p', 'No connected MCP tools are available to review.')); return; }
      for (const item of catalogue) {
        if (!valid(item)) continue;
        const policy = policies.get(item.tool_id), card = el('article', undefined, undefined, 'team-card');
        card.append(el('strong', item.tool_id), uiEl('p', item.available ? 'Available' : 'Unavailable'));
        const schema = el('details'), summary = uiEl('summary', 'Show exact schema'), code = el('pre', JSON.stringify(item.schema, null, 2), undefined, 'team-output'); code.setAttribute('data-i18n-ignore', ''); schema.append(summary, code); card.append(schema);
        if (policy) { const state = el('p'); state.append(uiEl('span', policy.enabled ? 'MCP access enabled' : 'MCP access revoked'), el('span', `: ${policy.revision}`)); card.append(state); }
        const roles = el('div', undefined, undefined, 'team-actions'), checks = [];
        const selected = new Set(policy?.enabled === true && Array.isArray(policy.roles) ? policy.roles : []);
        for (const role of ['lead', 'executor', 'researcher', 'reviewer']) {
          const input = el('input'); input.type = 'checkbox'; input.checked = selected.has(role); input.id = `${prefix}-mcp-${item.tool_id.replace(/[^a-zA-Z0-9]/g, '-')}-${role}`;
          const label = el('label', undefined, undefined, 'team-check'); label.htmlFor = input.id; label.append(input, uiEl('span', role)); roles.append(label); checks.push({ role, input });
        }
        const effect = el('select'); effect.append(option('brokered_network_read', 'Brokered network read', true), option('read_public', 'Public read', true));
        if (policy?.enabled && Array.isArray(policy.effects) && policy.effects.length === 1 && ['brokered_network_read', 'read_public'].includes(policy.effects[0])) effect.value = policy.effects[0];
        const confirm = el('input'); confirm.type = 'checkbox'; confirm.id = `${prefix}-mcp-confirm-${item.tool_id.replace(/[^a-zA-Z0-9]/g, '-')}`;
        const confirmation = el('label', undefined, undefined, 'team-check'); confirmation.htmlFor = confirm.id; confirmation.append(confirm, uiEl('span', 'I reviewed this exact schema and approve this read-only Team access.'));
        const save = button(policy?.enabled ? 'Update reviewed MCP access' : 'Enable reviewed MCP access', 'mcp-save'), revoke = button('Revoke MCP access', 'mcp-revoke'); revoke.hidden = !policy?.enabled;
        const update = () => { save.disabled = saving || !item.available || !confirm.checked || !checks.some(check => check.input.checked); revoke.disabled = saving; };
        confirm.addEventListener('change', update); checks.forEach(check => check.input.addEventListener('change', update)); update();
        save.addEventListener('click', async () => {
          if (save.disabled || saving) return;
          saving = true; controls(); update(); say('Saving reviewed MCP access…');
          try {
            await request(`${API}/mcp/reviews`, { method: 'POST', body: { tool_id: item.tool_id, schema_digest: item.schema_digest, effects: [effect.value], roles: checks.filter(check => check.input.checked).map(check => check.role), expected_revision: Number.isInteger(policy?.revision) ? policy.revision : 0, confirmation: true } });
            if (!disposed) { say('Reviewed MCP access saved.'); void load(); }
          } catch (error) { if (!disposed) say('Unable to save reviewed MCP access. Refresh and review the exact schema again.', error); }
          finally { saving = false; controls(); update(); }
        });
        revoke.addEventListener('click', async () => {
          if (revoke.disabled || saving || !policy?.enabled) return;
          saving = true; controls(); update(); say('Revoking MCP access…');
          try {
            await request(`${API}/mcp/reviews/revoke`, { method: 'POST', body: { tool_id: item.tool_id, expected_revision: policy.revision } });
            if (!disposed) { say('Reviewed MCP access revoked.'); void load(); }
          } catch (error) { if (!disposed) say('Unable to revoke MCP access. Refresh before trying again.', error); }
          finally { saving = false; controls(); update(); }
        });
        card.append(field('Allowed read effect', effect, `mcp-effect-${item.tool_id}`), roles, confirmation, save, revoke); rows.append(card);
      }
      if (!rows.children.length) rows.append(uiEl('p', 'No valid MCP review entries were returned.'));
    }
    async function load() {
      if (disposed || !enabled || loadingReviews) return;
      const token = ++generation; loadingReviews = true; controls(); say('Loading reviewed MCP tools…');
      try { const data = await request(`${API}/mcp/reviews`, { method: 'GET' }); if (!disposed && token === generation) { render(data); say('Reviewed MCP tools loaded.'); } }
      catch (error) { if (!disposed && token === generation) { rows.replaceChildren(); say('Unable to load reviewed MCP tools.', error); } }
      finally { if (!disposed && token === generation) { loadingReviews = false; controls(); } }
    }
    refreshReviews.addEventListener('click', () => void load());
    return { panel, setEnabled(value) { enabled = value === true; panel.hidden = !enabled; if (enabled) void load(); else { ++generation; rows.replaceChildren(); } controls(); }, destroy() { ++generation; } };
  }

  function createCheckProfilesPanel() {
    let enabled = false, projectId = '', generation = 0, listGeneration = 0, profiles = [], cursor = null;
    let fetching = false, saving = false, pagingBlocked = false, seenCursors = new Set(), pinnedProfile = null, reviewReady = false;
    const drafts = new Map();
    const blank = () => ({ profileId: null, expectedRevision: null, name: '', command: '', conflict: false });
    let draft = blank();
    const panel = el('section', undefined, 'check-profiles', 'team-card team-panel'); panel.hidden = true;
    panel.append(uiEl('h4', 'Approved check commands'), uiEl('p', 'This panel saves approved command text only. No command is executed, no check result is produced, and project permissions stay unchanged.'));
    const projectScope = el('p', '', 'check-project');
    const profileSelect = el('select', undefined, 'check-profile');
    const refreshProfiles = button('Refresh check profiles', 'check-refresh'), more = button('Load more check profiles', 'check-more');
    const actions = el('div', undefined, undefined, 'team-actions'); actions.append(refreshProfiles, more);
    const status = el('p', undefined, 'check-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const form = el('form', undefined, 'check-form', 'team-panel'); form.noValidate = true;
    const name = el('input', undefined, 'check-name'); name.type = 'text'; name.required = true;
    const command = el('textarea', undefined, 'check-command'); command.required = true; command.rows = 6; command.spellcheck = false;
    const preview = el('pre', '', 'check-preview', 'team-output'); preview.setAttribute('data-i18n-ignore', '');
    const savedDetails = el('div', undefined, 'check-saved-details', 'team-panel');
    const savedCommand = el('pre', '', 'check-saved-command', 'team-output'); savedCommand.setAttribute('data-i18n-ignore', '');
    const useRevision = button('Use reviewed revision for this draft', 'check-use-revision');
    const confirm = el('input', undefined, 'check-confirm'); confirm.type = 'checkbox'; confirm.id = `${prefix}-check-confirm`;
    const confirmLabel = el('label', undefined, undefined, 'team-check'); confirmLabel.htmlFor = confirm.id;
    confirmLabel.append(confirm, uiEl('span', 'I approve saving this exact command for this project. This does not authorize execution.'));
    const save = button('Save approved command', 'check-save'); save.type = 'submit';
    form.append(field('Profile name', name, 'check-name'), field('Exact check command', command, 'check-command'), uiEl('h4', 'Full command preview'), preview,
      uiEl('h4', 'Last loaded saved command'), savedDetails, savedCommand, useRevision, confirmLabel, save);
    panel.append(projectScope, field('Saved check profile', profileSelect, 'check-profile'), actions, status, form);
    const currentLoaded = () => profiles.find(item => item.id === draft.profileId);
    const needsReview = () => draft.conflict || !!(currentLoaded() && currentLoaded().revision !== draft.expectedRevision);
    const say = (label, error = null) => {
      status.replaceChildren(uiEl('span', label)); if (error) status.append(el('span', `: ${message(error)}`));
      status.className = `team-notice${error ? ' team-error' : ''}`;
    };
    function controls() {
      const unavailable = !enabled || !projectId || saving;
      for (const input of [name, command, profileSelect]) input.disabled = unavailable;
      refreshProfiles.disabled = unavailable; more.hidden = !cursor && !pagingBlocked;
      more.disabled = unavailable || fetching || !cursor || pagingBlocked;
      confirm.disabled = unavailable || fetching || needsReview();
      save.disabled = confirm.disabled || !confirm.checked;
      useRevision.hidden = !needsReview(); useRevision.disabled = unavailable || fetching || !currentLoaded() || !reviewReady;
      bindUiText(save, saving ? 'Saving approved command…' : 'Save approved command');
      checkRuns?.refreshSelection();
    }
    function rememberDraft() { if (projectId) drafts.set(projectId, { draft: { ...draft }, pinnedProfile }); }
    function renderSaved() {
      const saved = currentLoaded() || pinnedProfile; savedDetails.replaceChildren(); savedCommand.textContent = saved?.command || '';
      if (saved) {
        savedDetails.append(el('p', saved.id));
        for (const [label, value] of [['Revision', saved.revision], ['Command SHA-256', saved.command_hash]]) {
          const row = el('p'); row.append(uiEl('span', label), el('span', `: ${value}`)); savedDetails.append(row);
        }
      } else savedDetails.append(uiEl('p', 'No saved check profile selected.'));
      if (needsReview()) savedDetails.append(uiEl('p', 'Your draft is preserved. Refresh and review the saved command, explicitly adopt its revision, then approve your draft again.'));
    }
    function renderOptions() {
      const choices = [...profiles]; if (pinnedProfile && !choices.some(item => item.id === pinnedProfile.id)) choices.push(pinnedProfile);
      profileSelect.replaceChildren(option('', 'New check profile', true), ...choices.map(item => option(item.id, `${item.name} — ${item.id}`)));
      profileSelect.value = draft.profileId || '';
    }
    function renderDraft() { name.value = draft.name; command.value = draft.command; preview.textContent = draft.command; confirm.checked = false; renderOptions(); renderSaved(); controls(); }
    function validProfile(value) {
      return value && typeof value.id === 'string' && value.id && String(value.project_id) === projectId && typeof value.name === 'string'
        && typeof value.command === 'string' && Number.isInteger(value.revision) && value.revision > 0 && typeof value.command_hash === 'string' && /^[a-f0-9]{64}$/.test(value.command_hash);
    }
    async function loadProfiles(reset = false) {
      if (disposed || !enabled || !projectId || saving || (!reset && (fetching || pagingBlocked || !cursor))) return;
      const token = generation, listToken = ++listGeneration, id = projectId, after = reset ? '' : cursor;
      fetching = true; say('Loading check profiles…'); controls();
      try {
        const data = await request(`${API}/projects/${encodeURIComponent(id)}/check-profiles?limit=50${after ? `&after_id=${encodeURIComponent(after)}` : ''}`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || listToken !== listGeneration || id !== projectId) return;
        if (!Array.isArray(data?.profiles) || !data.profiles.every(validProfile) || (data.next_cursor != null && (typeof data.next_cursor !== 'string' || !data.next_cursor))) throw new Error('Invalid check profile page');
        const next = data.next_cursor || null;
        if (!reset && next && (next === after || seenCursors.has(next))) { pagingBlocked = true; say('Check profile cursor did not advance. Refresh before loading more.'); return; }
        const merged = new Map((reset ? [] : profiles).map(item => [item.id, item]));
        for (const item of data.profiles) merged.set(item.id, item); profiles = [...merged.values()]; cursor = next;
        if (reset) reviewReady = false;
        if (data.profiles.some(item => item.id === draft.profileId)) reviewReady = true;
        if (reset) { seenCursors = new Set(); pagingBlocked = false; } else seenCursors.add(after);
        renderOptions(); renderSaved(); say(needsReview() ? 'Check profile changed. Review the saved command before approving this draft.' : 'Check profiles loaded.');
      } catch (error) { if (!disposed && token === generation && listToken === listGeneration) say('Unable to load check profiles. Your draft is preserved; retry explicitly.', error); }
      finally { if (!disposed && token === generation && listToken === listGeneration) { fetching = false; controls(); } }
    }
    function selectionChanged(force = false) {
      if (disposed) return;
      const project = current(), nextId = project ? String(project.id) : '';
      projectScope.textContent = project ? `${project.name} — ${project.host_id} — ${project.root} [${project.id}]` : '';
      if (!force && nextId === projectId) return;
      rememberDraft(); projectId = nextId; ++generation; ++listGeneration; profiles = []; cursor = null; seenCursors = new Set(); pagingBlocked = false; fetching = false; saving = false; reviewReady = false;
      const cached = drafts.get(projectId); draft = cached ? { ...cached.draft } : blank(); pinnedProfile = cached?.pinnedProfile || null; renderDraft();
      if (!projectId) say('Select an Engineering project to manage its check commands.');
      else if (enabled) void loadProfiles(true);
    }
    profileSelect.addEventListener('change', () => {
      if (disposed || !enabled || saving || !projectId) return;
      const selected = profiles.find(item => item.id === profileSelect.value) || (pinnedProfile?.id === profileSelect.value ? pinnedProfile : null);
      draft = selected ? { profileId: selected.id, expectedRevision: selected.revision, name: selected.name, command: selected.command, conflict: false } : blank();
      pinnedProfile = selected; reviewReady = !!currentLoaded(); renderDraft(); rememberDraft(); say('Review the full command and explicitly approve saving.');
    });
    for (const input of [name, command]) input.addEventListener('input', () => {
      draft.name = name.value; draft.command = command.value; preview.textContent = command.value; confirm.checked = false; rememberDraft(); controls(); say('Unsaved check command. Review and approve the exact preview.');
    });
    confirm.addEventListener('change', controls);
    refreshProfiles.addEventListener('click', () => void loadProfiles(true)); more.addEventListener('click', () => void loadProfiles(false));
    useRevision.addEventListener('click', () => {
      const saved = currentLoaded(); if (disposed || !enabled || saving || fetching || !saved || !needsReview() || !reviewReady) return;
      draft.expectedRevision = saved.revision; draft.conflict = false; pinnedProfile = saved; confirm.checked = false; rememberDraft(); renderSaved(); controls();
      say('Saved revision reviewed. Your command draft is unchanged; approve its exact preview again.');
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (disposed || !enabled || !projectId || saving || fetching || needsReview() || !confirm.checked) return;
      if (!name.value.trim() || !command.value.trim()) { say('Enter a profile name and command.'); return; }
      if ([...name.value].length > 200 || new TextEncoder().encode(command.value).length > 16384 || command.value.includes('\0')) { say('Use a name of at most 200 characters and a command of at most 16384 UTF-8 bytes without NUL.'); return; }
      const token = generation, id = projectId, body = { name: name.value, command: command.value, confirmation: true, profile_id: draft.profileId, expected_revision: draft.expectedRevision };
      saving = true; confirm.checked = false; ++listGeneration; fetching = false; controls(); say('Saving approved command…');
      try {
        const saved = await request(`${API}/projects/${encodeURIComponent(id)}/check-profiles`, { method: 'POST', body });
        if (disposed || !enabled || token !== generation || id !== projectId) return;
        if (!validProfile(saved) || (body.profile_id && saved.id !== body.profile_id) || saved.command !== body.command || saved.name !== body.name) throw new Error('Saved check profile identity mismatch');
        profiles = profiles.filter(item => item.id !== saved.id).concat(saved); pinnedProfile = saved;
        draft = { profileId: saved.id, expectedRevision: saved.revision, name: saved.name, command: saved.command, conflict: false };
        renderDraft(); rememberDraft(); say('Command profile saved. No command was executed and project permissions are unchanged.');
      } catch (error) {
        if (disposed || token !== generation || id !== projectId) return;
        draft.conflict = Number(error?.status || error?.statusCode || error?.response?.status) === 409; if (draft.conflict) reviewReady = false; rememberDraft(); renderSaved();
        say(draft.conflict ? 'Check profile changed elsewhere. Your draft is preserved. Refresh, review the saved revision and approve again; no automatic retry was made.' : 'Unable to save check profile. Your draft is preserved.', draft.conflict ? null : error);
      } finally { if (!disposed && token === generation && id === projectId) { saving = false; controls(); } }
    });
    renderDraft(); say('Select an Engineering project to manage its check commands.');
    return { panel, selectionChanged,
      launchSnapshot() {
        const profile = currentLoaded() || pinnedProfile, project = current();
        return { project: project ? { ...project } : null, profile: profile ? { ...profile } : null,
          ready: enabled && !!project && String(project.id) === projectId && !!profile && !saving && !fetching && !needsReview()
            && draft.expectedRevision === profile.revision && draft.name === profile.name && draft.command === profile.command };
      },
      setEnabled(value) { const changed = value !== enabled; enabled = value; panel.hidden = !value; if (changed) selectionChanged(true); controls(); },
      destroy() { ++generation; ++listGeneration; },
    };
  }

  function createBaselineComparisonPanel() {
    let enabled = false, projectId = '', generation = 0, listGeneration = 0, compareGeneration = 0, rows = [], cursor = null;
    let fetching = false, comparing = false, blocked = false, seenCursors = new Set();
    const panel = el('section', undefined, 'baseline-comparison', 'team-card team-panel'); panel.hidden = true;
    panel.append(uiEl('h4', 'Baseline command comparison'), uiEl('p', 'Compare saved command outcomes only. This panel never runs commands, compares individual test failures or performance, or proves that the project is free of regressions.'));
    const scope = el('p', '', 'baseline-project'), before = el('select', undefined, 'baseline-before'), after = el('select', undefined, 'baseline-after');
    const refresh = button('Refresh saved runs', 'baseline-refresh'), more = button('Load more saved runs', 'baseline-more');
    const historyNotice = el('p', '', 'baseline-history-notice', 'team-notice'); historyNotice.setAttribute('role', 'status');
    const compare = button('Compare saved outcomes', 'baseline-compare');
    const status = el('p', '', 'baseline-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const result = el('div', undefined, 'baseline-result', 'team-panel');
    panel.append(scope, refresh, more, historyNotice, field('Saved baseline run', before, 'baseline-before'), field('Subsequent saved check', after, 'baseline-after'), compare, status, result);
    const labels = { remained_passing: 'Both commands passed', became_failing: 'Command changed from passing to failing', became_passing: 'Command changed from failing to passing', failure_persists: 'Both commands failed', not_comparable: 'Outcomes are not comparable' };
    const reasons = { profile_id_changed: 'Approved profile changed', profile_revision_changed: 'Approved profile revision changed', command_hash_changed: 'Command hash changed', host_id_changed: 'Execution host changed', project_revision_changed: 'Project revision changed', terminal_evidence_unavailable: 'Terminal command evidence is unavailable', reported_environment_changed: 'Reported environment changed' };
    const statuses = { passed: 'Passed', failed: 'Failed', running: 'Running', stale: 'Stale', timed_out: 'Timed out', cancelled: 'Cancelled', interrupted: 'Interrupted', dispatch_unknown: 'Dispatch unknown' };
    const say = (label, error = null, target = status) => { target.replaceChildren(uiEl('span', label)); if (error) target.append(el('span', `: ${message(error)}`)); target.className = `team-notice${error ? ' team-error' : ''}`; };
    const validRun = row => row && typeof row.id === 'string' && row.id && ['baseline', 'check'].includes(row.kind) && typeof row.profile_id === 'string'
      && typeof row.host_id === 'string' && Number.isInteger(row.profile_revision) && Number.isInteger(row.project_revision) && typeof row.status === 'string'
      && typeof row.command_hash === 'string' && /^[a-f0-9]{64}$/.test(row.command_hash) && typeof row.workspace_hash === 'string' && /^[a-f0-9]{64}$/.test(row.workspace_hash);
    const selected = (control, kind) => rows.find(row => row.id === control.value && row.kind === kind);
    function controls() {
      before.disabled = after.disabled = !enabled || !projectId;
      compare.disabled = !enabled || !projectId || fetching || comparing || !selected(before, 'baseline') || !selected(after, 'check');
      refresh.disabled = !enabled || !projectId; more.hidden = !cursor && !blocked; more.disabled = !enabled || fetching || !cursor || blocked;
    }
    function invalidate() { ++compareGeneration; comparing = false; result.replaceChildren(); say('Choose saved runs and compare explicitly. Selection and refresh do not execute commands.'); controls(); }
    function renderOptions() {
      for (const [control, kind, label] of [[before, 'baseline', 'Choose a saved baseline'], [after, 'check', 'Choose a saved check']]) {
        const value = control.value; control.replaceChildren(option('', label, true), ...rows.filter(row => row.kind === kind).map(row => option(row.id, `${row.profile_id} — ${row.id} · ${row.started_at ?? '—'}`)));
        control.value = rows.some(row => row.id === value && row.kind === kind) ? value : '';
      }
    }
    async function loadHistory(reset = true) {
      if (disposed || !enabled || !projectId || (!reset && (fetching || blocked || !cursor))) return;
      const token = generation, listToken = ++listGeneration, id = projectId, nextPage = reset ? '' : cursor;
      if (reset) invalidate(); fetching = true; say('Loading saved runs…', null, historyNotice); controls();
      try {
        const data = await request(`${API}/projects/${encodeURIComponent(id)}/check-runs?limit=50${nextPage ? `&after_id=${encodeURIComponent(nextPage)}` : ''}`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || listToken !== listGeneration) return;
        if (!Array.isArray(data?.runs) || !data.runs.every(validRun) || (data.next_cursor != null && (typeof data.next_cursor !== 'string' || !data.next_cursor))) throw new Error('Invalid saved check run page');
        const next = data.next_cursor || null;
        if (!reset && next && (next === nextPage || seenCursors.has(next))) { blocked = true; say('Saved run cursor did not advance. Refresh before loading more.', null, historyNotice); return; }
        const pinned = [selected(before, 'baseline'), selected(after, 'check')].filter(Boolean), merged = new Map((reset ? [] : rows).map(row => [row.id, row]));
        for (const row of data.runs) merged.set(row.id, row); for (const row of pinned) if (!merged.has(row.id)) merged.set(row.id, row);
        rows = [...merged.values()]; cursor = next; if (reset) { seenCursors = new Set(); blocked = false; } else seenCursors.add(nextPage);
        renderOptions(); say(rows.length ? 'Saved runs loaded.' : 'No saved runs loaded for this project. Run checks elsewhere before comparing.', null, historyNotice);
      } catch (error) { if (!disposed && token === generation && listToken === listGeneration) say('Unable to load saved runs. Retry explicitly; the selected runs are preserved.', error, historyNotice); }
      finally { if (!disposed && token === generation && listToken === listGeneration) { fetching = false; controls(); } }
    }
    function selectionChanged(force = false) {
      if (disposed) return; const project = current(), nextId = project ? String(project.id) : '';
      scope.textContent = project ? `${project.name} — ${project.host_id} — ${project.root} [${project.id}]` : '';
      if (!force && nextId === projectId) return;
      projectId = nextId; ++generation; ++listGeneration; rows = []; cursor = null; seenCursors = new Set(); blocked = false; fetching = false;
      before.value = after.value = ''; renderOptions(); invalidate();
      if (enabled && projectId) void loadHistory(); else say('Select an Engineering project to compare saved command outcomes.', null, historyNotice);
    }
    before.addEventListener('change', invalidate); after.addEventListener('change', invalidate);
    refresh.addEventListener('click', () => void loadHistory()); more.addEventListener('click', () => void loadHistory(false));
    function renderRun(row, label) {
      const card = el('article', undefined, undefined, 'team-card'); card.append(uiEl('h4', label));
      for (const [name, value] of [['Run ID', row.id], ['Check profile ID', row.profile_id], ['Profile revision', row.profile_revision], ['Command SHA-256', row.command_hash], ['Host', row.host_id], ['Project revision', row.project_revision], ['Workspace SHA-256', row.workspace_hash], ['Started at', row.started_at], ['Finished at', row.finished_at], ['Exit code', row.exit_code]]) {
        const field = el('p'); field.append(uiEl('span', name), el('span', `: ${value == null ? '—' : String(value)}`)); card.append(field);
      }
      card.append(uiEl('p', statuses[row.status] || 'Unknown check state'), uiEl('h4', 'Runner-reported toolchain'));
      if (row.reported_toolchain == null) card.append(uiEl('p', 'No toolchain information was reported.'));
      else { const code = el('pre', typeof row.reported_toolchain === 'string' ? row.reported_toolchain : JSON.stringify(row.reported_toolchain, null, 2), undefined, 'team-output'); code.setAttribute('data-i18n-ignore', ''); card.append(code); }
      return card;
    }
    compare.addEventListener('click', async () => {
      if (disposed || !enabled || !projectId || fetching || comparing || !selected(before, 'baseline') || !selected(after, 'check')) return;
      const token = generation, requestToken = ++compareGeneration, id = projectId, baselineId = before.value, checkId = after.value;
      comparing = true; result.replaceChildren(); say('Comparing saved command outcomes…'); controls();
      try {
        const data = await request(`${API}/projects/${encodeURIComponent(id)}/check-comparison?baseline_run_id=${encodeURIComponent(baselineId)}&check_run_id=${encodeURIComponent(checkId)}`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || requestToken !== compareGeneration || before.value !== baselineId || after.value !== checkId) return;
        if (!Object.hasOwn(labels, data?.classification) || typeof data.comparable !== 'boolean' || data.comparable !== (data.classification !== 'not_comparable') || !Array.isArray(data.reasons)
          || !data.reasons.every(reason => typeof reason === 'string') || data.scope !== 'command_outcome_only' || data.individual_failures_compared !== false
          || !validRun(data.before) || !validRun(data.after) || data.before.id !== baselineId || data.before.kind !== 'baseline' || data.after.id !== checkId || data.after.kind !== 'check') throw new Error('Saved comparison identity or scope mismatch');
        result.append(uiEl('h4', labels[data.classification]), uiEl('p', 'This comparison covers command exit outcomes only. It is not a project completion verdict or evidence that source changes caused the transition.'));
        if (data.classification === 'failure_persists') result.append(uiEl('p', 'Both commands failed; this does not mean the errors are identical. Individual test failures were not compared.'));
        for (const reason of data.reasons) result.append(Object.hasOwn(reasons, reason) ? uiEl('p', reasons[reason]) : el('p', reason));
        result.append(renderRun(data.before, 'Baseline outcome'), renderRun(data.after, 'Subsequent check outcome'));
        say('Saved outcome comparison loaded. Environment identity is limited to what the runner reported.');
      } catch (error) { if (!disposed && token === generation && requestToken === compareGeneration) { result.replaceChildren(); say('Unable to compare saved outcomes. No comparison is claimed; retry explicitly.', error); } }
      finally { if (!disposed && token === generation && requestToken === compareGeneration) { comparing = false; controls(); } }
    });
    renderOptions(); invalidate(); say('Select an Engineering project to compare saved command outcomes.', null, historyNotice);
    return { panel, selectionChanged, setEnabled(value) { const changed = enabled !== value; enabled = value; panel.hidden = !value; if (changed) selectionChanged(true); controls(); }, destroy() { ++generation; ++listGeneration; ++compareGeneration; } };
  }

  function createRequirementsPanel() {
    let enabled = false, projectId = '', generation = 0, saving = false, readinessGeneration = 0, reading = false, pinned = null, reviewReady = false;
    const blank = () => ({ id: null, revision: null, title: '', profileIds: [], mandatory: true, conflict: false });
    let draft = blank(), profileInputs = []; const drafts = new Map();
    const listState = () => ({ rows: [], cursor: null, seen: new Set(), blocked: false, fetching: false, token: 0 });
    const criteria = listState(), profiles = listState();
    const panel = el('section', undefined, 'requirements', 'team-card team-panel'); panel.hidden = true;
    panel.append(uiEl('h4', 'Acceptance criteria'), uiEl('p', 'Criteria link approved check profiles. Saving a criterion does not execute commands, grant host access or mark the project complete.'));
    const scope = el('p', '', 'requirement-project');
    const select = el('select', undefined, 'requirement-select');
    const refreshCriteria = button('Refresh criteria', 'requirement-refresh'), moreCriteria = button('Load more criteria', 'requirement-more');
    const status = el('p', '', 'requirement-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const form = el('form', undefined, 'requirement-form', 'team-panel'); form.noValidate = true;
    const title = el('textarea', undefined, 'requirement-title'); title.rows = 3; title.required = true;
    const mandatory = el('input', undefined, 'requirement-mandatory'); mandatory.type = 'checkbox';
    const profileList = el('div', undefined, 'requirement-profiles', 'team-panel');
    const refreshProfiles = button('Refresh available profiles', 'requirement-profiles-refresh'), moreProfiles = button('Load more available profiles', 'requirement-profiles-more');
    const profileNotice = el('p', '', 'requirement-profiles-notice', 'team-notice'); profileNotice.setAttribute('role', 'status');
    const saved = el('div', undefined, 'requirement-saved', 'team-panel');
    const useRevision = button('Use reviewed criterion revision', 'requirement-use-revision');
    const confirm = el('input', undefined, 'requirement-confirm'); confirm.type = 'checkbox'; confirm.id = `${prefix}-requirement-confirm`;
    const consentLabel = el('label', undefined, undefined, 'team-check'); consentLabel.htmlFor = confirm.id;
    consentLabel.append(confirm, uiEl('span', 'I approve this exact criterion, linked profiles and mandatory/optional setting, including any weakening of acceptance requirements.'));
    const save = button('Save criterion', 'requirement-save'); save.type = 'submit';
    form.append(field('Criterion title', title, 'requirement-title'), field('Mandatory criterion', mandatory, 'requirement-mandatory'),
      uiEl('h4', 'Linked approved profiles'), refreshProfiles, moreProfiles, profileNotice, profileList, uiEl('h4', 'Last loaded saved criterion'), saved, useRevision, consentLabel, save);
    const readinessRefresh = button('Refresh readiness snapshot', 'readiness-refresh');
    const readinessNotice = el('p', '', 'readiness-notice', 'team-notice'); readinessNotice.setAttribute('role', 'status');
    const readinessResult = el('div', undefined, 'readiness-result', 'team-panel');
    panel.append(scope, field('Saved criterion', select, 'requirement-select'), refreshCriteria, moreCriteria, status, form,
      uiEl('h4', 'Project readiness snapshot'), uiEl('p', 'Readiness is checked only when requested, using the runner workspace hash. It does not execute checks or complete the project. File edits elsewhere can make a displayed snapshot stale.'), readinessRefresh, readinessNotice, readinessResult);
    const say = (label, error = null, target = status) => { target.replaceChildren(uiEl('span', label)); if (error) target.append(el('span', `: ${message(error)}`)); target.className = `team-notice${error ? ' team-error' : ''}`; };
    const loaded = () => criteria.rows.find(item => item.id === draft.id);
    const needsReview = () => draft.conflict || !!(loaded() && loaded().revision !== draft.revision);
    const remember = () => { if (projectId) drafts.set(projectId, { draft: { ...draft, profileIds: [...draft.profileIds] }, pinned }); };
    function invalidateReadiness() { ++readinessGeneration; reading = false; readinessResult.replaceChildren(); say('Readiness has not been verified for the current state. Refresh explicitly.', null, readinessNotice); }
    function controls() {
      const unavailable = !enabled || !projectId || saving;
      for (const node of [title, mandatory, select, refreshCriteria, refreshProfiles]) node.disabled = unavailable;
      for (const node of profileInputs) node.disabled = unavailable || profiles.fetching;
      confirm.disabled = unavailable || criteria.fetching || profiles.fetching || needsReview(); save.disabled = confirm.disabled || !confirm.checked;
      useRevision.hidden = !needsReview(); useRevision.disabled = unavailable || criteria.fetching || !loaded() || !reviewReady;
      for (const [state, more] of [[criteria, moreCriteria], [profiles, moreProfiles]]) { more.hidden = !state.cursor && !state.blocked; more.disabled = unavailable || state.fetching || !state.cursor || state.blocked; }
      readinessRefresh.disabled = !enabled || !projectId || saving || reading;
    }
    function renderSaved() {
      const value = loaded() || pinned; saved.replaceChildren();
      if (value) { saved.append(el('p', value.title), el('p', value.id)); const row = el('p'); row.append(uiEl('span', 'Revision'), el('span', `: ${value.revision} · `), uiEl('span', value.mandatory ? 'Mandatory' : 'Optional')); saved.append(row, el('p', value.profile_ids.join(', '))); }
      else saved.append(uiEl('p', 'No saved criterion selected.'));
      if (needsReview()) saved.append(uiEl('p', 'Your criterion draft is preserved. Refresh, review the saved revision and explicitly adopt it before confirming again.'));
    }
    function renderOptions() {
      const rows = [...criteria.rows]; if (pinned && !rows.some(item => item.id === pinned.id)) rows.push(pinned);
      select.replaceChildren(option('', 'New criterion', true), ...rows.map(item => option(item.id, `${item.title} — ${item.id}`))); select.value = draft.id || '';
    }
    function renderProfiles() {
      profileList.replaceChildren(); profileInputs = [];
      for (const profile of profiles.rows) {
        const row = el('label', undefined, undefined, 'team-check'), input = el('input'); input.type = 'checkbox'; input.dataset.requirementProfile = profile.id; input.checked = draft.profileIds.includes(profile.id);
        profileInputs.push(input);
        row.append(input, el('span', `${profile.name} — ${profile.id} [${profile.revision}]`));
        input.addEventListener('change', () => { if (input.checked) draft.profileIds = [...new Set([...draft.profileIds, profile.id])]; else draft.profileIds = draft.profileIds.filter(id => id !== profile.id); edited(); }); profileList.append(row);
      }
      const unresolved = draft.profileIds.filter(id => !profiles.rows.some(profile => profile.id === id));
      if (unresolved.length) { profileList.append(uiEl('p', 'Some linked profiles are not loaded. Load more profiles before approving this criterion.'), el('p', unresolved.join(', '))); }
      if (!profiles.rows.length) profileList.append(uiEl('p', 'No approved profiles loaded. Create a profile or refresh the list.'));
    }
    function renderDraft() { title.value = draft.title; mandatory.checked = draft.mandatory; confirm.checked = false; renderOptions(); renderSaved(); renderProfiles(); controls(); }
    function edited() { draft.title = title.value; draft.mandatory = mandatory.checked; confirm.checked = false; invalidateReadiness(); remember(); controls(); say('Unsaved criterion. Review its linked profiles and mandatory setting before approving.'); }
    const validCriterion = value => value && typeof value.id === 'string' && value.id && value.project_id === projectId && typeof value.title === 'string'
      && typeof value.mandatory === 'boolean' && Number.isInteger(value.revision) && value.revision > 0 && Array.isArray(value.profile_ids)
      && value.profile_ids.length > 0 && value.profile_ids.length <= 32 && value.profile_ids.every(id => typeof id === 'string' && id) && new Set(value.profile_ids).size === value.profile_ids.length;
    const validProfile = value => value && typeof value.id === 'string' && value.id && value.project_id === projectId && typeof value.name === 'string' && Number.isInteger(value.revision) && value.revision > 0;
    async function loadList(isProfiles, reset = true) {
      const state = isProfiles ? profiles : criteria, target = isProfiles ? profileNotice : status;
      if (disposed || !enabled || !projectId || saving || (!reset && (state.fetching || state.blocked || !state.cursor))) return;
      const token = generation, listToken = ++state.token, id = projectId, after = reset ? '' : state.cursor;
      state.fetching = true; say(isProfiles ? 'Loading available profiles…' : 'Loading criteria…', null, target); controls();
      try {
        const key = isProfiles ? 'profiles' : 'requirements', path = isProfiles ? 'check-profiles' : 'requirements';
        const data = await request(`${API}/projects/${encodeURIComponent(id)}/${path}?limit=50${after ? `&after_id=${encodeURIComponent(after)}` : ''}`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || listToken !== state.token) return;
        if (!Array.isArray(data?.[key]) || !data[key].every(isProfiles ? validProfile : validCriterion) || (data.next_cursor != null && (typeof data.next_cursor !== 'string' || !data.next_cursor))) throw new Error('Invalid criterion or profile page');
        const next = data.next_cursor || null;
        if (!reset && next && (next === after || state.seen.has(next))) { state.blocked = true; say('Page cursor did not advance. Refresh this list before loading more.', null, target); return; }
        const rows = new Map((reset ? [] : state.rows).map(item => [item.id, item])); for (const item of data[key]) rows.set(item.id, item); state.rows = [...rows.values()]; state.cursor = next;
        if (reset) { state.seen = new Set(); state.blocked = false; if (!isProfiles) reviewReady = false; } else state.seen.add(after);
        if (!isProfiles && data[key].some(item => item.id === draft.id)) reviewReady = true;
        renderOptions(); renderSaved(); renderProfiles(); say(isProfiles ? 'Available profiles loaded.' : 'Criteria loaded.', null, target);
      } catch (error) { if (!disposed && token === generation && listToken === state.token) say(isProfiles ? 'Unable to load available profiles. Retry explicitly.' : 'Unable to load criteria. Your draft is preserved; retry explicitly.', error, target); }
      finally { if (!disposed && token === generation && listToken === state.token) { state.fetching = false; controls(); } }
    }
    function selectionChanged(force = false) {
      if (disposed) return; const project = current(), nextId = project ? String(project.id) : '';
      scope.textContent = project ? `${project.name} — ${project.host_id} — ${project.root} [${project.id}]` : '';
      if (!force && nextId === projectId) return;
      remember(); projectId = nextId; ++generation; saving = false; reviewReady = false;
      for (const state of [criteria, profiles]) { const token = state.token + 1; Object.assign(state, listState(), { token }); }
      const cached = drafts.get(projectId); draft = cached ? { ...cached.draft, profileIds: [...cached.draft.profileIds] } : blank(); pinned = cached?.pinned || null;
      invalidateReadiness(); renderDraft(); if (enabled && projectId) { void loadList(false); void loadList(true); } else say('Select an Engineering project to manage acceptance criteria.');
    }
    select.addEventListener('change', () => { if (!enabled || saving || !projectId) return; const value = criteria.rows.find(item => item.id === select.value) || (pinned?.id === select.value ? pinned : null);
      draft = value ? { id: value.id, revision: value.revision, title: value.title, profileIds: [...value.profile_ids], mandatory: value.mandatory, conflict: false } : blank(); pinned = value; reviewReady = !!loaded(); invalidateReadiness(); renderDraft(); remember(); });
    title.addEventListener('input', edited); mandatory.addEventListener('change', edited); confirm.addEventListener('change', controls);
    refreshCriteria.addEventListener('click', () => void loadList(false)); moreCriteria.addEventListener('click', () => void loadList(false, false));
    refreshProfiles.addEventListener('click', () => void loadList(true)); moreProfiles.addEventListener('click', () => void loadList(true, false));
    useRevision.addEventListener('click', () => { const value = loaded(); if (!enabled || saving || criteria.fetching || !value || !reviewReady || !needsReview()) return;
      draft.revision = value.revision; draft.conflict = false; pinned = value; confirm.checked = false; invalidateReadiness(); remember(); renderSaved(); controls(); say('Saved criterion revision reviewed. The draft is unchanged; explicitly approve it again.'); });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (disposed || !enabled || !projectId || saving || criteria.fetching || profiles.fetching || needsReview() || !confirm.checked) return;
      if (!title.value.trim() || [...title.value].length > 1000 || title.value.includes('\0') || draft.profileIds.length < 1 || draft.profileIds.length > 32 || draft.profileIds.some(id => !profiles.rows.some(profile => profile.id === id))) { say('Enter a title of 1–1000 characters and select 1–32 loaded approved profiles.'); return; }
      const token = generation, id = projectId, body = { title: title.value, profile_ids: [...draft.profileIds], mandatory: mandatory.checked, requirement_id: draft.id, expected_revision: draft.revision, confirmation: true };
      saving = true; confirm.checked = false; invalidateReadiness(); for (const state of [criteria, profiles]) { ++state.token; state.fetching = false; } controls(); say('Saving criterion…');
      try {
        const result = await request(`${API}/projects/${encodeURIComponent(id)}/requirements`, { method: 'POST', body });
        if (disposed || !enabled || token !== generation) return;
        if (!result || typeof result.id !== 'string' || !result.id || (body.requirement_id && result.id !== body.requirement_id) || result.revision !== (body.expected_revision || 0) + 1) throw new Error('Saved criterion identity or revision mismatch');
        pinned = { id: result.id, project_id: id, title: body.title, profile_ids: body.profile_ids, mandatory: body.mandatory, revision: result.revision };
        criteria.rows = criteria.rows.filter(item => item.id !== pinned.id).concat(pinned); draft = { id: pinned.id, revision: pinned.revision, title: pinned.title, profileIds: [...pinned.profile_ids], mandatory: pinned.mandatory, conflict: false }; reviewReady = true;
        renderDraft(); remember(); say('Criterion saved. No command was executed and the project was not marked complete.');
      } catch (error) { if (!disposed && token === generation) { draft.conflict = Number(error?.status || error?.statusCode || error?.response?.status) === 409; if (draft.conflict) reviewReady = false; remember(); renderSaved(); say(draft.conflict ? 'Criterion changed elsewhere. Your draft is preserved; refresh, review and confirm again. No automatic retry was made.' : 'Unable to save criterion. Your draft is preserved.', draft.conflict ? null : error); } }
      finally { if (!disposed && token === generation) { saving = false; controls(); } }
    });
    readinessRefresh.addEventListener('click', async () => {
      if (disposed || !enabled || !projectId || reading || saving) return; const token = generation, requestToken = ++readinessGeneration, id = projectId;
      reading = true; readinessResult.replaceChildren(); say('Reading readiness snapshot…', null, readinessNotice); controls();
      try {
        const data = await request(`${API}/projects/${encodeURIComponent(id)}/check-readiness`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || requestToken !== readinessGeneration) return;
        if (typeof data?.ready !== 'boolean' || typeof data.workspace_hash !== 'string' || !/^[a-f0-9]{64}$/.test(data.workspace_hash) || !Number.isInteger(data.project_revision) || !Array.isArray(data.requirements)
          || !data.requirements.every(row => typeof row.id === 'string' && typeof row.title === 'string' && Number.isInteger(row.revision) && typeof row.mandatory === 'boolean' && typeof row.passed === 'boolean' && Array.isArray(row.checks)
            && row.checks.every(check => typeof check.profile_id === 'string' && (check.run_id === null || typeof check.run_id === 'string') && typeof check.passed === 'boolean' && typeof check.status === 'string'))) throw new Error('Invalid readiness snapshot');
        readinessResult.append(uiEl('strong', data.ready ? 'Ready for the returned snapshot' : 'Not ready for the returned snapshot'));
        for (const [label, value, key] of [['Workspace SHA-256', data.workspace_hash, 'readiness-hash'], ['Project revision', data.project_revision], ['Snapshot ID', data.snapshot_id], ['Observed at', data.observed_at]]) {
          if (value == null) continue; const row = el('p'); row.append(uiEl('span', label), el('span', ': '), el('span', String(value), key)); readinessResult.append(row);
        }
        const states = { passed: 'Passed', failed: 'Failed', running: 'Running', stale: 'Stale', timed_out: 'Timed out', cancelled: 'Cancelled', interrupted: 'Interrupted', dispatch_unknown: 'Dispatch unknown', missing_or_stale: 'Missing or stale check' };
        for (const requirement of data.requirements) {
          const row = el('article', undefined, undefined, 'team-card'); row.append(el('strong', requirement.title), el('p', `${requirement.id} [${requirement.revision}]`), uiEl('p', requirement.mandatory ? 'Mandatory' : 'Optional'), uiEl('p', requirement.passed ? 'Passed' : 'Not passed'));
          for (const check of requirement.checks) { const item = el('p'); item.append(el('span', `${check.profile_id}${Number.isInteger(check.profile_revision) ? ` [${check.profile_revision}]` : ''} · ${check.run_id || '—'} · `), uiEl('span', states[check.status] || 'Unknown check state')); row.append(item); } readinessResult.append(row);
        }
        say('This evidence applies to the returned workspace snapshot only. Later file, profile or criterion edits may make it stale; refresh explicitly.', null, readinessNotice);
      } catch (error) { if (!disposed && token === generation && requestToken === readinessGeneration) { readinessResult.replaceChildren(); say('Unable to read readiness. No current readiness is claimed; retry explicitly.', error, readinessNotice); } }
      finally { if (!disposed && token === generation && requestToken === readinessGeneration) { reading = false; controls(); } }
    });
    renderDraft(); invalidateReadiness(); say('Select an Engineering project to manage acceptance criteria.');
    return { panel, selectionChanged, setEnabled(value) { const changed = enabled !== value; enabled = value; panel.hidden = !value; if (changed) selectionChanged(true); controls(); }, destroy() { ++generation; ++readinessGeneration; } };
  }

  function createCheckRunsPanel() {
    let enabled = false, snapshot = null, snapshotKey = '', projectId = '', generation = 0, listGeneration = 0, pollGeneration = 0;
    let history = [], cursor = null, seenCursors = new Set(), historyBusy = false, pagingBlocked = false, selectedExplicitly = false;
    let operation = null, observed = null, timer = null, pollInFlight = null, mutating = false, attempt = null, conflictKey = '', stopRequested = false;
    const pendingAttempts = new Map(); // Mount-local only; never shared across authenticated browser users.
    let outputOffset = 0, outputText = '', outputDecoder = new TextDecoder(), outputBounded = false;
    const active = value => ['queued', 'running', 'cancel_requested'].includes(value?.status);
    const checkActive = value => ['running', 'dispatch_unknown', 'stop_requested'].includes(value?.status);
    const names = { queued: 'Queued', running: 'Running', cancel_requested: 'Cancellation requested', cancelled: 'Cancelled', interrupted: 'Interrupted', failed: 'Failed', completed: 'Completed', passed: 'Passed', stale: 'Stale', timed_out: 'Timed out', dispatch_unknown: 'Dispatch unknown', stop_requested: 'Stop requested' };
    const panel = el('section', undefined, 'check-runs', 'team-card team-panel'); panel.hidden = true;
    panel.append(uiEl('h4', 'Run and observe approved checks'), uiEl('p', 'Launching executes the reviewed saved command on the selected host. Unsaved drafts cannot run. Leaving or reloading this panel does not cancel a submitted check.'));
    panel.append(uiEl('p', 'New checks run in a verified file copy, not a sandbox. Absolute paths can still affect the trusted host. Git history is not copied. Copies are limited to 10000 entries and 128 MiB; unsupported workspaces are blocked, not run in the original folder.', 'run-copy-limits'));
    const launchScope = el('div', undefined, 'run-launch-scope', 'team-panel'), launchPreview = el('pre', '', 'run-launch-preview', 'team-output'); launchPreview.setAttribute('data-i18n-ignore', '');
    const kind = el('select', undefined, 'run-kind'); kind.append(option('check', 'Check run', true), option('baseline', 'Baseline run', true));
    const confirm = el('input', undefined, 'run-confirm'); confirm.type = 'checkbox'; confirm.id = `${prefix}-run-confirm`;
    const consentLabel = el('label', undefined, undefined, 'team-check'); consentLabel.htmlFor = confirm.id;
    consentLabel.append(confirm, uiEl('span', 'I authorize execution of this exact saved command and these project/profile revisions.'));
    const launch = button('Launch approved check', 'run-launch');
    const status = el('p', '', 'run-notice', 'team-notice'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const historySelect = el('select', undefined, 'run-history'), refresh = button('Refresh check runs', 'run-refresh'), more = button('Load more check runs', 'run-more');
    const historyNotice = el('p', '', 'run-history-notice', 'team-notice'); historyNotice.setAttribute('role', 'status');
    const operationStatus = el('p', '', 'run-operation-status'), checkStatus = el('p', '', 'run-check-status'), runDetails = el('div', undefined, 'run-details', 'team-panel');
    const cancelOperation = button('Cancel operation only; host command may continue', 'run-cancel-operation');
    const stopCommand = button('Stop host command', 'run-stop-command');
    const output = el('pre', '', 'run-output', 'team-output'); output.setAttribute('data-i18n-ignore', '');
    const outputNotice = el('p', '', 'run-output-notice');
    panel.append(launchScope, uiEl('h4', 'Saved command to execute'), launchPreview, field('Check run kind', kind, 'run-kind'), consentLabel, launch, status,
      field('Saved check operations', historySelect, 'run-history'), refresh, more, historyNotice, uiEl('h4', 'Operation status'), operationStatus,
      uiEl('h4', 'Actual check status'), checkStatus, runDetails, cancelOperation, stopCommand,
      uiEl('p', 'Cancelling the operation does not stop its host command. Stopping the host command is a separate explicit action; wait for a confirmed terminal check status.'),
      uiEl('h4', 'Host command output'), outputNotice, output);
    const say = (label, error = null, target = status) => { target.replaceChildren(uiEl('span', label)); if (error) target.append(el('span', `: ${message(error)}`)); target.className = `team-notice${error ? ' team-error' : ''}`; };
    const ready = () => snapshot?.ready && snapshot.project?.access_mode === 'trusted_host' && Number.isInteger(snapshot.project?.revision) && snapshot.project.revision > 0
      && snapshot.profile?.project_id === snapshot.project?.id && snapshotKey !== conflictKey;
    const sameAttempt = () => !attempt || (attempt.projectId === projectId && attempt.body.profile_id === snapshot?.profile?.id
      && attempt.body.expected_project_revision === snapshot?.project?.revision && attempt.body.expected_profile_revision === snapshot?.profile?.revision && attempt.body.kind === kind.value);
    function rememberAttempt(value) { attempt = value; if (value) pendingAttempts.set(projectId, value); else pendingAttempts.delete(projectId); }
    function controls() {
      kind.disabled = !enabled || mutating || !!attempt;
      confirm.disabled = !enabled || !ready() || !sameAttempt() || mutating || active(operation) || checkActive(observed);
      launch.disabled = confirm.disabled || !confirm.checked;
      bindUiText(launch, attempt ? 'Retry the same check request' : 'Launch approved check');
      historySelect.disabled = !enabled || mutating || !history.length; refresh.disabled = !enabled || !projectId || mutating;
      more.hidden = !cursor && !pagingBlocked; more.disabled = !enabled || mutating || historyBusy || !cursor || pagingBlocked;
      cancelOperation.disabled = !enabled || mutating || !active(operation) || operation?.status === 'cancel_requested';
      stopCommand.disabled = !enabled || mutating || !observed?.job_id || observed.status !== 'running' || stopRequested;
    }
    function renderLaunch() {
      launchScope.replaceChildren(); launchPreview.textContent = snapshot?.profile?.command || '';
      if (snapshot?.project) launchScope.append(el('p', `${snapshot.project.name} — ${snapshot.project.host_id} — ${snapshot.project.root} [${snapshot.project.id}]`));
      if (snapshot?.profile) {
        launchScope.append(el('p', `${snapshot.profile.name} [${snapshot.profile.id}]`));
        for (const [label, value] of [['Project revision', snapshot.project?.revision], ['Profile revision', snapshot.profile.revision], ['Command SHA-256', snapshot.profile.command_hash]]) {
          const row = el('p'); row.append(uiEl('span', label), el('span', `: ${value}`)); launchScope.append(row);
        }
      }
      if (!ready()) launchScope.append(uiEl('p', 'Select an unchanged saved check profile and review current trusted-host access before launching.'));
      if (attempt) launchScope.append(el('p', attempt.body.idempotency_key));
      controls();
    }
    function historyOptions() {
      historySelect.replaceChildren(option('', 'Choose a saved check operation', true), ...history.map(item => option(item.id, `${item.scope.profile_id} — ${item.scope.run_id} [${item.id}]`)));
      historySelect.value = operation?.id || '';
    }
    function renderObserved() {
      operationStatus.replaceChildren(uiEl('span', operation ? names[operation.status] || 'Unknown operation state' : 'No check operation selected'));
      checkStatus.replaceChildren(uiEl('span', observed ? names[observed.status] || 'Unknown check state' : 'Awaiting check dispatch'));
      runDetails.replaceChildren();
      if (operation) runDetails.append(el('p', `${operation.id} — ${operation.scope.run_id}`));
      if (observed?.job_id) runDetails.append(el('p', String(observed.job_id)));
      if (operation?.error) runDetails.append(el('p', typeof operation.error === 'string' ? operation.error : message(operation.error)));
      const exitCode = observed?.run?.evidence?.exit_code;
      if (Number.isInteger(exitCode)) { const row = el('p'); row.append(uiEl('span', 'Exit code'), el('span', `: ${exitCode}`)); runDetails.append(row); }
      controls();
    }
    function validOperation(value) { return value && typeof value.id === 'string' && value.kind === 'check_run' && value.scope?.project_id === projectId && typeof value.scope.run_id === 'string' && value.scope.run_id && typeof value.scope.profile_id === 'string'; }
    function stopPolling() { if (timer !== null) clearTimeout(timer); timer = null; }
    function resetOutput() { outputOffset = 0; outputText = ''; outputDecoder = new TextDecoder(); outputBounded = false; output.textContent = ''; outputNotice.replaceChildren(); }
    function remember(value) { operation = value; history = history.some(item => item.id === value.id) ? history.map(item => item.id === value.id ? value : item) : [value, ...history]; historyOptions(); }
    async function poll(token) {
      const op = operation, scope = op?.scope, project = projectId;
      if (disposed || !enabled || !op || token !== pollGeneration || pollInFlight === token) return;
      stopPolling(); pollInFlight = token;
      const live = () => !disposed && enabled && token === pollGeneration && operation?.id === op.id && projectId === project;
      try {
        const value = await request(`${API}/operations/${encodeURIComponent(op.id)}`, { method: 'GET' });
        if (!live()) return;
        if (!validOperation(value) || value.id !== op.id || value.scope.run_id !== scope.run_id) throw new Error('Check operation identity mismatch');
        remember(value); renderObserved();
        const base = `${API}/projects/${encodeURIComponent(project)}/check-runs/${encodeURIComponent(scope.run_id)}`;
        let currentRun;
        try { currentRun = await request(base, { method: 'GET' }); }
        catch (error) { if (Number(error?.status) === 404) { if (live()) { observed = null; renderObserved(); if (active(operation)) timer = setTimeout(() => void poll(token), 1000); } return; } throw error; }
        if (!live()) return;
        if (currentRun?.run_id !== scope.run_id) throw new Error('Check run identity mismatch');
        observed = currentRun; if (!checkActive(observed)) stopRequested = false; renderObserved();
        let moreOutput = false;
        if (observed.job_id) {
          const data = await request(`${base}/output?offset=${outputOffset}&limit=16000`, { method: 'GET' });
          if (!live()) return;
          if (data?.run_id !== scope.run_id || data.job_id !== observed.job_id || typeof data.output_base64 !== 'string'
            || !Number.isInteger(data.offset) || !Number.isInteger(data.next_offset) || data.offset < outputOffset || data.next_offset < data.offset) throw new Error('Check output identity or offset mismatch');
          const bytes = Uint8Array.from(atob(data.output_base64), character => character.charCodeAt(0));
          if (bytes.length > 16000 || data.next_offset - data.offset !== bytes.length) throw new Error('Invalid check output byte page');
          if (data.truncated || data.offset > outputOffset) { outputDecoder = new TextDecoder(); outputBounded = true; }
          moreOutput = bytes.length === 16000;
          outputText += outputDecoder.decode(bytes, { stream: checkActive(observed) || moreOutput }); outputOffset = data.next_offset;
          if (outputText.length > 64000) { outputText = outputText.slice(-64000); outputBounded = true; }
          output.textContent = outputText;
          outputNotice.replaceChildren(uiEl('span', outputBounded ? 'Output is truncated; this view retains at most 64000 characters.' : 'Output is plain text. Up to 64000 characters are retained in this view.'));
          if (data.notice) outputNotice.append(el('span', ` ${data.notice}`));
        }
        if (active(operation) || checkActive(observed) || moreOutput) timer = setTimeout(() => void poll(token), 1000);
      } catch (error) { if (live()) say('Unable to observe this check. Refresh to retry; no command was launched.', error); }
      finally { if (pollInFlight === token) pollInFlight = null; }
    }
    function selectOperation(value) {
      stopPolling(); ++pollGeneration; operation = value || null; observed = null; stopRequested = false; resetOutput(); historyOptions(); renderObserved();
      if (operation) void poll(pollGeneration);
    }
    async function loadHistory(reset = true) {
      if (disposed || !enabled || !projectId || mutating || (!reset && (historyBusy || pagingBlocked || !cursor))) return;
      const token = generation, listToken = ++listGeneration, after = reset ? '' : cursor;
      historyBusy = true; say('Loading check run history…', null, historyNotice); controls();
      try {
        const query = `${API}/operations?kind=check_run&project_id=${encodeURIComponent(projectId)}`;
        const [data, activeData] = await Promise.all([
          request(`${query}&limit=50${after ? `&after_id=${encodeURIComponent(after)}` : ''}`, { method: 'GET' }),
          reset ? request(`${query}&active_only=true&limit=1`, { method: 'GET' }) : Promise.resolve(null),
        ]);
        if (disposed || !enabled || token !== generation || listToken !== listGeneration) return;
        if (!Array.isArray(data?.operations) || (data.next_cursor != null && (typeof data.next_cursor !== 'string' || !data.next_cursor))) throw new Error('Invalid check operation page');
        if (reset && (!Array.isArray(activeData?.operations) || activeData.operations.length > 1
          || activeData.operations.some(item => !validOperation(item) || !active(item)))) throw new Error('Invalid active check operation page');
        const next = data.next_cursor || null;
        if (!reset && next && (next === after || seenCursors.has(next))) { pagingBlocked = true; say('Check history cursor did not advance. Refresh before loading more.', null, historyNotice); return; }
        const map = new Map((reset ? [] : history).map(item => [item.id, item]));
        for (const item of data.operations.filter(validOperation)) map.set(item.id, item);
        for (const item of activeData?.operations || []) map.set(item.id, item);
        if (operation && !map.has(operation.id)) map.set(operation.id, operation); history = [...map.values()]; cursor = next;
        if (reset) { seenCursors = new Set(); pagingBlocked = false; } else seenCursors.add(after);
        const recovered = attempt && history.find(item => item.scope.idempotency_key === attempt.body.idempotency_key && item.scope.profile_id === attempt.body.profile_id
          && item.scope.kind === attempt.body.kind && item.scope.expected_project_revision === attempt.body.expected_project_revision && item.scope.expected_profile_revision === attempt.body.expected_profile_revision);
        const restored = !selectedExplicitly && (activeData?.operations[0] || (!operation && (history.find(active) || history[0])));
        if (recovered) { rememberAttempt(null); selectOperation(recovered); say('Submitted check recovered from saved history. No launch request was repeated.'); renderLaunch(); }
        else if (restored && restored.id !== operation?.id) selectOperation(restored);
        else { historyOptions(); if (reset && operation) { stopPolling(); ++pollGeneration; void poll(pollGeneration); } }
        say(history.length ? 'Check run history loaded.' : 'No runs for this project in loaded history. Load more to inspect older records.', null, historyNotice);
      } catch (error) { if (!disposed && token === generation && listToken === listGeneration) say('Unable to load check run history. Retry explicitly.', error, historyNotice); }
      finally { if (!disposed && token === generation && listToken === listGeneration) { historyBusy = false; controls(); } }
    }
    function refreshSelection() {
      if (disposed) return;
      const next = checkProfiles.launchSnapshot(), nextProject = next.project ? String(next.project.id) : '';
      const key = JSON.stringify([nextProject, next.project?.revision, next.profile?.id, next.profile?.revision, next.profile?.command, next.profile?.name, !!next.ready]);
      snapshot = next;
      if (key !== snapshotKey) { snapshotKey = key; confirm.checked = false; }
      if (nextProject !== projectId) {
        projectId = nextProject; ++generation; ++listGeneration; ++pollGeneration; stopPolling(); history = []; cursor = null; seenCursors = new Set(); pagingBlocked = false;
        historyBusy = false; mutating = false; attempt = pendingAttempts.get(projectId) || null; if (attempt) kind.value = attempt.body.kind;
        conflictKey = ''; operation = null; observed = null; selectedExplicitly = false; stopRequested = false; resetOutput(); historyOptions(); renderObserved();
        if (enabled && projectId) void loadHistory();
      }
      renderLaunch();
    }
    const newKey = () => {
      if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
      const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const value = [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join(''); return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
    };
    kind.addEventListener('change', () => { confirm.checked = false; controls(); }); confirm.addEventListener('change', controls);
    historySelect.addEventListener('change', () => { if (!mutating) { selectedExplicitly = true; selectOperation(history.find(item => item.id === historySelect.value)); } });
    refresh.addEventListener('click', () => void loadHistory()); more.addEventListener('click', () => void loadHistory(false));
    launch.addEventListener('click', async () => {
      if (disposed || !enabled || mutating || !ready() || !sameAttempt() || !confirm.checked || active(operation) || checkActive(observed)) return;
      if (!attempt) rememberAttempt({ projectId, body: { profile_id: snapshot.profile.id, kind: kind.value, idempotency_key: newKey(), expected_project_revision: snapshot.project.revision, expected_profile_revision: snapshot.profile.revision, confirmation: true } });
      const token = generation, pending = attempt; mutating = true; confirm.checked = false; ++listGeneration; historyBusy = false; renderLaunch(); say('Submitting explicitly approved check…');
      let recover = false;
      try {
        const value = await request(`${API}/projects/${encodeURIComponent(projectId)}/check-runs`, { method: 'POST', body: pending.body });
        if (disposed || !enabled || token !== generation || pending !== attempt) return;
        if (!validOperation(value) || value.scope.idempotency_key !== pending.body.idempotency_key || value.scope.profile_id !== pending.body.profile_id || value.scope.kind !== pending.body.kind
          || value.scope.expected_project_revision !== pending.body.expected_project_revision || value.scope.expected_profile_revision !== pending.body.expected_profile_revision) throw new Error('Submitted check scope mismatch');
        rememberAttempt(null); selectedExplicitly = true; remember(value); selectOperation(value); say('Check submitted. Observation and reload do not launch another command.');
      } catch (error) {
        if (disposed || token !== generation || pending !== attempt) return;
        const code = Number(error?.status || error?.statusCode || error?.response?.status);
        if (code >= 400 && code < 500) { rememberAttempt(null); conflictKey = snapshotKey; say('Check was not accepted. Refresh and review the saved project/profile revisions before confirming again.', error); }
        else { recover = true; say('Submission outcome is unknown. Inspect saved history or explicitly retry the same request key; no automatic launch retry will occur.', error); }
      } finally { if (!disposed && token === generation) { mutating = false; renderLaunch(); if (recover) void loadHistory(); } }
    });
    async function mutation(stop) {
      if (disposed || !enabled || mutating || !operation || (stop ? !observed?.job_id || observed.status !== 'running' || stopRequested : !active(operation) || operation.status === 'cancel_requested')) return;
      const token = generation, selectedId = operation.id, runId = operation.scope.run_id, pollToken = ++pollGeneration; stopPolling(); mutating = true; controls();
      try {
        const path = stop ? `${API}/projects/${encodeURIComponent(projectId)}/check-runs/${encodeURIComponent(runId)}/stop` : `${API}/operations/${encodeURIComponent(selectedId)}/cancel`;
        const value = await request(path, { method: 'POST', body: stop ? { confirmation: true } : {} });
        if (disposed || token !== generation || operation?.id !== selectedId) return;
        if (stop) { if (value?.run_id !== runId) throw new Error('Stop response identity mismatch'); stopRequested = value.status === 'stop_requested'; say('Host stop requested. Waiting for the actual check status; the command is not yet confirmed stopped.'); }
        else { if (!validOperation(value) || value.id !== selectedId) throw new Error('Cancelled operation identity mismatch'); remember(value); say('Operation cancellation requested. Its host command may still be running.'); }
      } catch (error) { if (!disposed && token === generation) say(stop ? 'Host stop outcome is uncertain. Refresh actual check status before retrying.' : 'Unable to cancel the operation. Refresh its status.', error); }
      finally { if (!disposed && token === generation) { mutating = false; renderObserved(); void poll(pollToken); } }
    }
    cancelOperation.addEventListener('click', () => void mutation(false)); stopCommand.addEventListener('click', () => void mutation(true));
    historyOptions(); renderObserved(); renderLaunch();
    return { panel, refreshSelection,
      setEnabled(value) { const changed = value !== enabled; enabled = value; panel.hidden = !value; if (!value) { ++generation; ++listGeneration; ++pollGeneration; stopPolling(); } refreshSelection(); if (changed && value && projectId) void loadHistory(); controls(); },
      destroy() { ++generation; ++listGeneration; ++pollGeneration; stopPolling(); },
    };
  }

  function createContextPolicyPanel() {
    let enabled = false, snapshot = null, pending = false, fetching = false, conflict = false, generation = 0, loadedKey = '';
    let inherited = {}, eventsGeneration = 0, eventCursor = 0, eventRows = [], eventsBusy = false, eventsFailed = false;
    let imported = null, importGeneration = 0, importKey = '';
    let observationBusy = false, observationQueued = false;
    let policyTimer = null, observingVisibility = false;
    function stopPolicyPolling() { if (policyTimer !== null) clearTimeout(policyTimer); policyTimer = null; }
    function schedulePolicyPolling() {
      stopPolicyPolling();
      if (!disposed && enabled && !document.hidden) policyTimer = setTimeout(pollPolicy, 5000);
    }
    async function pollPolicy() {
      stopPolicyPolling();
      if (disposed || !enabled || document.hidden) return;
      try { await refreshObservation(); await loadEvents(false); } finally { schedulePolicyPolling(); }
    }
    function visibilityChanged() {
      stopPolicyPolling();
      if (!document.hidden) void pollPolicy();
    }
    function stopPolicyWatching() {
      stopPolicyPolling();
      if (observingVisibility) document.removeEventListener('visibilitychange', visibilityChanged);
      observingVisibility = false;
    }
    const controlsByKey = new Map();
    const panel = el('section', undefined, 'context-policy', 'team-card team-panel'); panel.hidden = true;
    panel.append(uiEl('h4', 'Context policy'), uiEl('p', 'Only explicitly checked overrides are saved. Saving policy does not invoke a model or compact a conversation. Sources show the last saved policy.'));
    const retentionRules = el('details');
    retentionRules.append(uiEl('summary', 'History retention rules'), uiEl('p', 'The minimum recent groups takes priority over the recent token budget. Set both to zero to summarize all optional history. Goals and pinned tool exchanges remain protected; an impossible target is rejected without a summary request.'));
    panel.append(retentionRules);
    const scopeSelect = el('select', undefined, 'context-scope');
    const projectOption = option('project', 'Selected project', true);
    const chatOption = option('session', 'Current chat', true);
    const taskOption = option('task', 'Current Team task', true), workerOption = option('worker', 'Selected Team worker', true);
    scopeSelect.append(option('owner', 'Owner defaults', true), chatOption, projectOption, taskOption, workerOption); scopeSelect.value = 'owner';
    if (contextOnly) { projectOption.remove(); taskOption.remove(); workerOption.remove(); }
    if (contextOnly && chatId) scopeSelect.value = 'session';
    const workerSelect = el('select', undefined, 'context-worker');
    const scopeDetails = el('p', '', 'context-scope-details');
    const reload = button('Reload context policy', 'context-reload');
    const policyNotice = el('p', '', 'context-notice', 'team-notice'); policyNotice.setAttribute('role', 'status'); policyNotice.setAttribute('aria-live', 'polite');
    const validation = el('div', undefined, 'context-validation', 'team-panel'); validation.setAttribute('aria-live', 'polite');
    const lastRequest = el('div', undefined, 'context-last-request', 'team-panel');
    const observationError = el('p', undefined, 'context-observation-error', 'team-error');
    observationError.setAttribute('role', 'status');
    const form = el('form', undefined, 'context-form', 'team-panel'); form.noValidate = true;
    const base = el('div', undefined, undefined, 'team-config'), advanced = el('details', undefined, 'context-advanced', 'team-panel');
    advanced.append(uiEl('summary', 'Advanced context settings'));
    const advancedFields = el('div', undefined, undefined, 'team-config'); advanced.append(advancedFields);
    const sourceLabel = value => value === 'default' ? 'System defaults' : value === 'owner' ? 'Owner defaults'
      : String(value).startsWith('project:') ? 'Project override' : String(value).startsWith('task:') ? 'Task override'
      : String(value).startsWith('worker:') ? 'Worker override' : String(value).startsWith('session:') ? 'Chat override' : 'Unverified';
    const say = (label, error = false, suffix = '') => {
      policyNotice.textContent = label + suffix; bindUiText(policyNotice, label, 'text', suffix);
      policyNotice.className = `team-notice${error ? ' team-error' : ''}`;
    };
    for (const spec of CONTEXT_FIELDS) {
      const [key, label, initial, min, max, isAdvanced] = spec;
      const input = el('input', undefined, `context-${key}`); input.dataset.contextField = key;
      input.type = typeof initial === 'boolean' ? 'checkbox' : 'number';
      if (input.type === 'number') { input.min = String(min); input.max = String(max); input.step = '1'; }
      else input.setAttribute('style', 'width:auto;align-self:flex-start');
      const override = el('input', undefined, `context-override-${key}`); override.type = 'checkbox'; override.id = `${prefix}-context-override-${key}`;
      override.setAttribute('style', 'width:auto;flex-shrink:0');
      const toggle = el('label', undefined, undefined, 'team-check'); toggle.htmlFor = override.id;
      toggle.append(override, uiEl('span', 'Override at this scope'));
      const row = field(label, input, `context-${key}`), source = uiEl('span', 'Unverified', `context-source-${key}`);
      const sourceRow = el('p'); sourceRow.append(uiEl('span', 'Saved source'), el('span', ': '), source);
      row.append(toggle, sourceRow); (isAdvanced ? advancedFields : base).append(row);
      controlsByKey.set(key, { input, override, source, spec });
      override.addEventListener('change', () => {
        if (!override.checked) setValue(input, inherited[key]);
        previewImport(); previewPreset(); update(); say('Unsaved context policy changes.');
      });
      input.addEventListener('input', () => { previewImport(); previewPreset(); say('Unsaved context policy changes.'); });
    }
    const save = button('Save context policy', 'context-save'); save.type = 'submit';
    const reset = button('Reset this scope to inheritance', 'context-reset');
    const presets = {
      balanced: { label: 'Balanced context', values: { auto_compact: true, trigger_percent: 75, target_percent: 50, recent_groups: 4, summary_tokens: 1200 } },
      long: { label: 'Long agent task', values: { auto_compact: true, trigger_percent: 65, target_percent: 40, recent_groups: 6, summary_tokens: 2048 } },
      compact: { label: 'Compact context', values: { auto_compact: true, trigger_percent: 60, target_percent: 35, recent_groups: 2, summary_tokens: 1024 } },
    };
    const presetSelect = el('select', undefined, 'context-preset');
    presetSelect.append(option('', 'Choose a context preset', true));
    for (const [id, preset] of Object.entries(presets)) presetSelect.append(option(id, preset.label, true));
    const presetPreview = el('div', undefined, 'context-preset-preview', 'team-panel');
    const applyPreset = button('Use preset as draft', 'context-preset-apply');
    function previewPreset() {
      presetPreview.replaceChildren();
      for (const [key, value] of Object.entries(presets[presetSelect.value]?.values || {})) {
        const row = controlsByKey.get(key), before = row.input.type === 'checkbox' ? row.input.checked : row.input.value;
        const line = el('p'); line.append(uiEl('span', row.spec[1]), el('span', ': '),
          typeof before === 'boolean' ? uiEl('span', before ? 'Enabled' : 'Disabled') : el('span', String(before)),
          el('span', ' → '), typeof value === 'boolean' ? uiEl('span', value ? 'Enabled' : 'Disabled') : el('span', String(value)));
        presetPreview.append(line);
      }
    }
    presetSelect.addEventListener('change', () => { previewPreset(); update(); });
    applyPreset.addEventListener('click', () => {
      if (applyPreset.disabled || !presets[presetSelect.value]) return;
      for (const [key, value] of Object.entries(presets[presetSelect.value].values)) {
        const row = controlsByKey.get(key); row.override.checked = true; setValue(row.input, value);
      }
      previewImport(); update(); say('Preset copied to draft. Review all overrides and save explicitly; no compaction was started.');
    });
    const importFile = el('input', undefined, 'context-import-file'); importFile.type = 'file'; importFile.accept = '.json,application/json';
    importFile.hidden = true;
    const chooseImport = button('Choose profile file', 'context-import-choose');
    chooseImport.addEventListener('click', () => { if (!chooseImport.disabled) importFile.click(); });
    const importPreview = el('div', undefined, 'context-import-preview', 'team-panel');
    const importApply = button('Apply imported values to draft', 'context-import-apply');
    const exportProfile = button('Export saved overrides', 'context-export');
    const draftValues = () => Object.fromEntries([...controlsByKey].map(([key, row]) => [key, row.input.type === 'checkbox' ? row.input.checked : Number(row.input.value)]));
    function previewImport() {
      importPreview.replaceChildren();
      if (!imported) return;
      for (const [key, value] of Object.entries(imported)) {
        const row = controlsByKey.get(key), before = draftValues()[key];
        const line = el('p'); line.append(uiEl('span', row.spec[1]), el('span', ': '));
        line.append(typeof before === 'boolean' ? uiEl('span', before ? 'Enabled' : 'Disabled') : el('span', String(before)), el('span', ' → '),
          typeof value === 'boolean' ? uiEl('span', value ? 'Enabled' : 'Disabled') : el('span', String(value)));
        importPreview.append(line);
      }
      if (!Object.keys(imported).length) importPreview.append(uiEl('p', 'This profile has no overrides; applying it makes no changes.'));
    }
    importFile.addEventListener('change', async () => {
      const token = ++importGeneration, scopeKey = keyForScope(), file = importFile.files?.[0];
      imported = null; importKey = ''; previewImport(); update();
      if (!file || importFile.disabled) return;
      try {
        if (file.size > CONTEXT_PROFILE_MAX_BYTES) throw new Error('Context profile is too large. Maximum size is 32 KiB.');
        const values = parseContextProfile(await file.text(), CONTEXT_FIELDS);
        if (disposed || !enabled || token !== importGeneration || scopeKey !== keyForScope()) return;
        const problem = validate({ ...draftValues(), ...values });
        if (problem) { say(problem[0], true); return; }
        imported = values; importKey = scopeKey; previewImport(); update();
        say('Profile preview loaded. Only listed values will be added to your draft; save separately to apply the policy.');
      } catch (error) { if (!disposed && token === importGeneration && scopeKey === keyForScope()) say(message(error), true); }
    });
    importApply.addEventListener('click', () => {
      if (importApply.disabled || !imported || importKey !== keyForScope()) return;
      const problem = validate({ ...draftValues(), ...imported });
      if (problem) { say(problem[0], true); return; }
      for (const [key, value] of Object.entries(imported)) {
        const row = controlsByKey.get(key); row.override.checked = true; setValue(row.input, value);
      }
      imported = null; importKey = ''; importFile.value = ''; previewImport(); previewPreset(); update();
      say('Imported values copied to draft. Save explicitly; no model was invoked.');
    });
    exportProfile.addEventListener('click', () => {
      if (exportProfile.disabled || !snapshot || loadedKey !== keyForScope()) return;
      try {
        const text = serializeContextProfile(snapshot.layers?.find(layer => layer.scope === targetLayer())?.overrides || {}, CONTEXT_FIELDS);
        const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
        const link = el('a'); link.href = url; link.download = 'odysseus-context-policy-v1.json'; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        say('Saved overrides exported. Unsaved edits and conversation data are not included.');
      } catch (error) { say(message(error), true); }
    });
    const library = el('div', undefined, 'context-library', 'team-panel');
    const librarySelect = el('select', undefined, 'context-library-select');
    const libraryKind = el('select', undefined, 'context-library-kind');
    libraryKind.append(option('full', 'Full saved policy', true), option('overrides', 'Saved scope overrides only', true));
    const librarySearch = el('input', undefined, 'context-library-search'); librarySearch.type = 'search'; librarySearch.maxLength = 120;
    let libraryQuery = '';
    const libraryName = el('input', undefined, 'context-library-name'); libraryName.maxLength = 120;
    const libraryRefresh = button('Load saved presets', 'context-library-refresh');
    const libraryMore = button('Load more presets', 'context-library-more'); libraryMore.hidden = true;
    const libraryCopy = button('Save saved policy as new preset', 'context-library-copy');
    const libraryReplace = button('Update selected preset from saved policy', 'context-library-update');
    const libraryRename = button('Rename selected preset', 'context-library-rename');
    const libraryDelete = button('Delete selected preset', 'context-library-delete');
    const libraryPreview = button('Preview selected preset', 'context-library-preview');
    let libraryRows = [], libraryCursor = null, libraryBusy = false;
    const selectedPreset = () => libraryRows.find(row => row.id === librarySelect.value);
    function renderLibrary(selected = librarySelect.value) {
      librarySelect.replaceChildren(option('', 'Choose a saved preset', true));
      for (const row of libraryRows) librarySelect.append(option(row.id, row.name));
      librarySelect.value = selected;
      libraryMore.hidden = libraryCursor === null;
    }
    async function libraryAction(action) {
      if (libraryBusy || disposed || !enabled) return;
      libraryBusy = true; update();
      try { await action(); }
      catch (error) { if (!disposed && enabled) say(message(error), true); }
      finally { libraryBusy = false; if (!disposed) update(); }
    }
    async function loadLibrary(more = false) {
      const query = more ? libraryQuery : librarySearch.value.trim();
      const result = await request(`${API}/context-presets?limit=50&after_seq=${more ? libraryCursor || 0 : 0}&query=${encodeURIComponent(query)}`, { method: 'GET' });
      if (disposed || !enabled) return;
      libraryQuery = query;
      libraryRows = more ? [...new Map([...libraryRows, ...result.items].map(row => [row.id, row])).values()] : result.items;
      libraryCursor = result.next_cursor; renderLibrary();
    }
    libraryRefresh.addEventListener('click', () => libraryAction(() => loadLibrary()));
    librarySearch.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); libraryAction(() => loadLibrary()); }
    });
    libraryMore.addEventListener('click', () => libraryAction(() => loadLibrary(true)));
    librarySelect.addEventListener('change', () => { libraryName.value = selectedPreset()?.name || ''; libraryKind.value = selectedPreset()?.kind || 'full'; update(); });
    libraryName.addEventListener('input', update);
    async function saveLibrary(replace) {
      const row = selectedPreset();
      if (!snapshot || conflict || (replace && !row)) return;
      const body = { name: libraryName.value.trim(), kind: libraryKind.value,
        values: libraryKind.value === 'overrides' ? { ...(snapshot.layers?.find(layer => layer.scope === targetLayer())?.overrides || {}) } : { ...snapshot.effective },
        preset_id: replace ? row.id : '', expected_revision: replace ? row.revision : 0 };
      const result = await request(`${API}/context-presets`, { method: 'POST', body });
      if (disposed || !enabled) return;
      libraryRows = [...libraryRows.filter(item => item.id !== result.id), result];
      renderLibrary(result.id); say('Preset saved. Active context policy was not changed.');
    }
    libraryCopy.addEventListener('click', () => { if (!libraryCopy.disabled) libraryAction(() => saveLibrary(false)); });
    libraryReplace.addEventListener('click', () => { if (!libraryReplace.disabled) libraryAction(() => saveLibrary(true)); });
    libraryRename.addEventListener('click', () => {
      const row = selectedPreset(); if (libraryRename.disabled || !row) return;
      const body = { name: libraryName.value.trim(), expected_revision: row.revision };
      libraryAction(async () => {
        const result = await request(`${API}/context-presets/${encodeURIComponent(row.id)}`, { method: 'PATCH', body });
        if (disposed || !enabled) return;
        libraryRows = libraryRows.map(item => item.id === result.id ? result : item);
        renderLibrary(result.id); say('Preset renamed. Its settings remain unchanged.');
      });
    });
    libraryDelete.addEventListener('click', () => {
      const row = selectedPreset();
      if (libraryDelete.disabled || !row || !window.confirm(translateUiText('Delete this preset? Applied task settings will remain unchanged.'))) return;
      libraryAction(async () => {
        await request(`${API}/context-presets/${encodeURIComponent(row.id)}?expected_revision=${row.revision}`, { method: 'DELETE' });
        if (disposed || !enabled) return;
        libraryRows = libraryRows.filter(item => item.id !== row.id); renderLibrary(''); libraryName.value = '';
        say('Preset deleted. Applied policies remain unchanged.');
      });
    });
    libraryPreview.addEventListener('click', () => {
      const row = selectedPreset(); if (libraryPreview.disabled || !row) return;
      const problem = validate({ ...draftValues(), ...row.values }); if (problem) { say(problem[0], true); return; }
      imported = { ...row.values }; importKey = keyForScope(); previewImport(); update();
      say(row.kind === 'overrides' ? 'Only saved overrides are previewed. Other settings remain unchanged.' : 'Saved preset preview loaded, including window and output reserve. Apply to draft, then save the policy separately.');
    });
    library.append(uiEl('h4', 'Saved context presets'),
      uiEl('p', 'Save all effective settings or only saved scope overrides. Unsaved edits are excluded. Preview before applying; saving a preset does not invoke a model.'),
      field('Search saved presets', librarySearch, 'context-library-search'),
      libraryRefresh, libraryMore, field('Saved preset', librarySelect, 'context-library-select'),
      field('Preset name', libraryName, 'context-library-name'), field('Preset contents', libraryKind, 'context-library-kind'), libraryCopy, libraryRename, libraryReplace, libraryDelete, libraryPreview);
    form.append(library, field('Context preset', presetSelect, 'context-preset'),
      uiEl('p', 'Presets are editable suggestions, not a guarantee of quality or GPU memory use. Window and output reserves stay unchanged. Selecting a preset does not save or run a model.'),
      presetPreview, applyPreset, field('Import context profile (JSON)', chooseImport, 'context-import-choose'), importFile,
      uiEl('p', 'Import previews only the listed overrides. Other draft values remain unchanged. Export includes saved overrides only, without task, model or conversation data.'),
      importPreview, importApply, exportProfile,
      base, uiEl('p', 'Requested window 0 uses the confirmed backend window. A policy cannot increase backend capacity; runtime still checks reserves and tool schemas.'), advanced, save, reset);
    const eventsRefresh = button('Refresh policy history', 'context-events-refresh');
    const eventsMore = button('Load more policy changes', 'context-events-more'); eventsMore.hidden = true;
    const eventsList = el('div', undefined, 'context-events', 'team-panel'); eventsList.setAttribute('aria-live', 'polite');
    const workerField = field('Context policy worker', workerSelect, 'context-worker');
    const taskHint = uiEl('p', 'Task policies use the current Team task and its saved project, not the project selected for new tasks.');
    panel.append(field('Context policy scope', scopeSelect, 'context-scope'), scopeDetails,
      ...(!contextOnly ? [workerField, taskHint] : []), reload, policyNotice, validation, lastRequest, observationError, form,
      uiEl('h4', 'Policy change history'), eventsRefresh, eventsList, eventsMore);
    const scope = () => ({ ...(scopeSelect.value === 'session' ? { session_id: chatId } : {}), project_id: scopeSelect.value === 'project' ? current()?.id || '' : '',
      task_id: ['task', 'worker'].includes(scopeSelect.value) ? taskContext?.id || '' : '',
      worker_id: scopeSelect.value === 'worker' ? workerSelect.value : '' });
    const keyForScope = () => JSON.stringify(scope());
    const targetLayer = () => scopeSelect.value === 'worker' ? `worker:${JSON.stringify([scope().task_id, scope().worker_id])}`
      : scopeSelect.value === 'task' ? `task:${scope().task_id}` : scopeSelect.value === 'session' ? `session:${chatId}` : scopeSelect.value === 'project' ? `project:${scope().project_id}` : 'owner';
    const hasScope = () => scopeSelect.value === 'owner' || (scopeSelect.value === 'session' ? !!chatId : scopeSelect.value === 'project' ? !!current()
      : !!taskContext?.id && (scopeSelect.value === 'task' || (taskContext.workers || []).some(worker => worker.id === workerSelect.value)));
    function setValue(input, value) { if (input.type === 'checkbox') input.checked = value === true; else input.value = value === undefined ? '' : String(value); }
    function update() {
      const allowed = enabled && !disposed;
      projectOption.disabled = !current(); scopeSelect.disabled = !allowed || pending;
      chatOption.disabled = !chatId;
      taskOption.disabled = !taskContext?.id; workerOption.disabled = !taskContext?.id || !taskContext.workers?.length;
      workerSelect.disabled = !allowed || pending || scopeSelect.value !== 'worker';
      reload.disabled = !allowed || pending || fetching || !hasScope();
      const editable = allowed && !pending && !fetching && !!snapshot && hasScope() && loadedKey === keyForScope();
      libraryRefresh.disabled = !allowed || libraryBusy; libraryMore.disabled = libraryRefresh.disabled;
      librarySelect.disabled = !allowed || libraryBusy; libraryName.disabled = !allowed || libraryBusy;
      librarySearch.disabled = !allowed || libraryBusy;
      libraryKind.disabled = !allowed || libraryBusy;
      libraryCopy.disabled = !editable || conflict || libraryBusy || !libraryName.value.trim();
      libraryReplace.disabled = libraryCopy.disabled || !selectedPreset();
      libraryRename.disabled = !allowed || libraryBusy || !selectedPreset() || !libraryName.value.trim();
      libraryDelete.disabled = !allowed || libraryBusy || !selectedPreset();
      libraryPreview.disabled = !editable || conflict || libraryBusy || !selectedPreset();
      for (const { input, override } of controlsByKey.values()) { override.disabled = !editable; input.disabled = !editable || !override.checked; }
      save.disabled = !editable || conflict; reset.disabled = !editable || conflict;
      presetSelect.disabled = !editable || conflict;
      applyPreset.disabled = !editable || conflict || !presets[presetSelect.value];
      importFile.disabled = !editable || conflict; exportProfile.disabled = !editable || conflict;
      chooseImport.disabled = importFile.disabled;
      importApply.disabled = !editable || conflict || !imported || importKey !== keyForScope();
      eventsRefresh.disabled = !allowed || eventsBusy; eventsMore.disabled = eventsRefresh.disabled;
    }
    function validate(values) {
      for (const [key, label, initial, min, max] of CONTEXT_FIELDS) {
        if (typeof initial === 'boolean') { if (typeof values[key] !== 'boolean') return ['Choose enabled or disabled for this setting.', label]; }
        else if (!Number.isInteger(values[key]) || values[key] < min || values[key] > max) return ['Use a whole number within the allowed range.', `${label}: ${min}–${max}`];
      }
      if (values.target_percent >= values.trigger_percent) return ['Target percentage must be lower than trigger percentage.', ''];
      return null;
    }
    function renderObservation(data) {
      observationError.replaceChildren();
      lastRequest.replaceChildren();
      if (scopeSelect.value === 'worker') {
        const observation = data.last_completed_request, used = observation?.context_policy;
        lastRequest.append(uiEl('h4', 'Last completed model request'));
        if (!used) lastRequest.append(uiEl('p', 'No confirmed context-policy observation for the last completed request.'));
        else {
          const matches = Object.keys(data.revisions || {}).length === Object.keys(used.revisions || {}).length
            && Object.entries(data.revisions || {}).every(([key, value]) => used.revisions?.[key] === value);
          lastRequest.append(uiEl('p', matches ? 'The last completed request used this saved policy revision.'
            : 'Saved policy differs from the last completed request. New settings are checked at the next request boundary.'));
          lastRequest.append(el('p', `${used.model} — ${used.endpoint_id}`),
            uiEl('p', used.summary_request ? 'Observation from a summarization request.' : 'Observation from a main model request.'),
            uiEl('p', 'Input token counts are estimates, not backend usage. This does not describe an in-flight request.'));
          for (const [label, value] of [['Context window', used.window], ['Input budget', used.input_budget],
            ['Estimated input tokens', (used.message_tokens || 0) + (used.schema_tokens || 0)], ['Maximum output tokens sent', used.max_output_tokens]]) {
            const row = el('p'); row.append(uiEl('span', label), el('span', `: ${value}`)); lastRequest.append(row);
          }
        }
      }
    }
    async function refreshObservation() {
      if (disposed || !enabled || !hasScope()) return;
      if (observationBusy || pending || fetching) { observationQueued = true; return; }
      const token = generation, body = scope(); observationBusy = true; observationQueued = false;
      try {
        const data = await request(`${API}/context-policy?${new URLSearchParams(body)}`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || JSON.stringify(body) !== keyForScope()) return;
        renderObservation(data);
        const sameRevision = snapshot && Object.keys(snapshot.revisions).length === Object.keys(data.revisions).length
          && Object.entries(snapshot.revisions).every(([key, value]) => data.revisions[key] === value);
        if (!sameRevision && !conflict) {
          conflict = true;
          validation.replaceChildren(uiEl('p', 'Displayed policy is outdated. Reload to validate the current settings.'));
          say('Context policy changed elsewhere. Reload, review and save again; your draft was not submitted again.', true);
          update();
        }
      } catch (_) {
        if (!disposed && enabled && token === generation && JSON.stringify(body) === keyForScope()) {
          observationError.replaceChildren(uiEl('span', scopeSelect.value === 'worker'
            ? 'Unable to refresh the last request. Previously displayed data may be outdated.'
            : 'Unable to refresh context policy. Previously displayed settings may be outdated.'));
        }
      } finally {
        observationBusy = false;
        if (observationQueued && !pending && !fetching) { observationQueued = false; void refreshObservation(); }
      }
    }
    function render(data) {
      ++importGeneration; imported = null; importKey = ''; importFile.value = ''; previewImport();
      presetSelect.value = ''; presetPreview.replaceChildren();
      snapshot = data; loadedKey = keyForScope(); conflict = false;
      renderObservation(data);
      inherited = Object.fromEntries(CONTEXT_FIELDS.map(([key, , value]) => [key, value]));
      for (const layer of data.layers || []) { if (layer.scope === targetLayer()) break; Object.assign(inherited, layer.overrides); }
      const own = data.layers?.find(layer => layer.scope === targetLayer())?.overrides || {};
      for (const [key, row] of controlsByKey) {
        row.override.checked = Object.hasOwn(own, key); setValue(row.input, data.effective?.[key]);
        const label = sourceLabel(data.sources?.[key]); row.source.textContent = label; bindUiText(row.source, label);
      }
      validation.replaceChildren();
      if (data.valid === false) {
        validation.append(uiEl('p', 'Inherited context policy is invalid. Correct this scope or reset its overrides; saving remains available for repairs.', undefined, 'team-error'));
        const problem = validate(data.effective || {});
        if (problem) {
          const row = uiEl('p', problem[0], undefined, 'team-error');
          if (problem[1]) row.append(el('span', ': '), uiEl('span', problem[1].split(':')[0]), el('span', problem[1].includes(':') ? ': ' + problem[1].split(':').slice(1).join(':').trim() : ''));
          validation.append(row);
        } else validation.append(uiEl('p', 'The server rejected the inherited policy. Review its overrides.', undefined, 'team-error'));
      } else validation.append(uiEl('p', data.configured ? 'Saved context policy is valid.' : 'No overrides configured; legacy runtime defaults remain active.'));
      update();
    }
    async function load() {
      if (disposed || !enabled) return;
      const token = ++generation, body = scope(); snapshot = null; loadedKey = ''; conflict = false;
      lastRequest.replaceChildren();
      observationError.replaceChildren();
      ++importGeneration; imported = null; importKey = ''; importFile.value = ''; previewImport();
      scopeDetails.textContent = body.project_id && current() ? `${current().name} — ${current().root} [${current().id}]` : '';
      if (body.task_id) scopeDetails.textContent = `${taskContext.name || ''} [${body.task_id}]${body.worker_id ? ' / ' + body.worker_id : ''}`;
      if (body.session_id) scopeDetails.textContent = body.session_id;
      if (!hasScope()) { fetching = false; update(); say('Select a valid project, Team task or worker before editing this scope.'); return; }
      fetching = true; update(); say('Loading context policy…');
      try {
        const data = await request(`${API}/context-policy?${new URLSearchParams(body)}`, { method: 'GET' });
        if (disposed || !enabled || token !== generation || JSON.stringify(body) !== keyForScope()) return;
        render(data); say('Review values and explicitly select the overrides to save.');
      } catch (error) { if (!disposed && token === generation) say('Unable to load context policy.', true, ` ${message(error)}`); }
      finally { if (!disposed && token === generation) { fetching = false; update(); if (observationQueued) void refreshObservation(); } }
    }
    async function persist(resetAll = false) {
      if (disposed || !enabled || pending || fetching || conflict || !snapshot || loadedKey !== keyForScope() || !hasScope()) return;
      const overrides = {};
      if (!resetAll) for (const [key, { input, override }] of controlsByKey) {
        if (override.checked) overrides[key] = input.type === 'checkbox' ? input.checked : input.value.trim() === '' ? NaN : Number(input.value);
      }
      const problem = validate({ ...inherited, ...overrides });
      if (problem) {
        say(problem[0], true);
        if (problem[1]) { policyNotice.append(el('span', ': '), uiEl('span', problem[1].split(':')[0]), el('span', problem[1].includes(':') ? ': ' + problem[1].split(':').slice(1).join(':').trim() : '')); }
        return;
      }
      const token = ++generation, requestScope = scope(), body = { ...requestScope, overrides, expected_revisions: { ...snapshot.revisions } };
      pending = true; update(); say('Saving context policy…');
      try {
        const data = await request(`${API}/context-policy`, { method: 'POST', body });
        if (disposed || !enabled || token !== generation || JSON.stringify(requestScope) !== keyForScope()) return;
        render(data); say('Context policy saved. No inference was started.'); void loadEvents(true);
      } catch (error) {
        if (disposed || token !== generation) return;
        conflict = Number(error?.status || error?.statusCode || error?.response?.status) === 409;
        say(conflict ? 'Context policy changed elsewhere. Reload, review and save again; your draft was not submitted again.' : 'Unable to save context policy. Your draft is preserved.', true, conflict ? '' : ` ${message(error)}`);
      } finally { if (!disposed) { pending = false; update(); if (observationQueued) void refreshObservation(); } }
    }
    async function loadEvents(resetList = false) {
      if (disposed || !enabled || eventsBusy) return;
      const token = ++eventsGeneration, cursor = resetList ? 0 : eventCursor; eventsBusy = true; update();
      try {
        const data = await request(`${API}/context-policy/events?after_seq=${cursor}&limit=50`, { method: 'GET' });
        if (disposed || !enabled || token !== eventsGeneration) return;
        // Empty replay pages do not rebuild an unchanged history every five
        // seconds. A previous error still needs a render to restore the list.
        if (!resetList && !eventsFailed && eventRows.length && !(data.events || []).length) { eventsMore.hidden = true; return; }
        eventsFailed = false;
        eventRows = [...(resetList ? [] : eventRows), ...(data.events || [])].filter((row, index, rows) => Number.isInteger(row.seq) && rows.findIndex(other => other.seq === row.seq) === index);
        eventCursor = Number.isInteger(data.next_cursor) ? data.next_cursor : cursor; eventsMore.hidden = (data.events || []).length < 50;
        eventsList.replaceChildren();
        for (const event of eventRows) {
          const row = el('article', undefined, undefined, 'team-card');
          row.append(uiEl('strong', sourceLabel(event.scope)), el('span', ` ${event.scope} · `), uiEl('span', 'Revision'), el('span', ` ${event.revision}`));
          const changes = el('p');
          for (const [key, label] of CONTEXT_FIELDS) if (Object.hasOwn(event.overrides || {}, key)) {
            const value = event.overrides[key];
            changes.append(uiEl('span', label), el('span', ': '),
              typeof value === 'boolean' ? uiEl('span', value ? 'Enabled' : 'Disabled') : el('span', String(value)),
              el('span', '; '));
          }
          if (!changes.children.length) changes.append(uiEl('span', 'No overrides: inherit parent values.'));
          row.append(changes); eventsList.append(row);
        }
        if (!eventRows.length) eventsList.append(uiEl('p', 'No context policy changes recorded.'));
      } catch (error) { if (!disposed && token === eventsGeneration) { eventsFailed = true; eventsList.replaceChildren(uiEl('p', 'Unable to load policy history.')); } }
      finally { if (!disposed && token === eventsGeneration) { eventsBusy = false; update(); } }
    }
    scopeSelect.addEventListener('change', () => void load()); reload.addEventListener('click', () => void load());
    workerSelect.addEventListener('change', () => void load());
    form.addEventListener('submit', event => { event.preventDefault(); void persist(false); }); reset.addEventListener('click', () => void persist(true));
    eventsRefresh.addEventListener('click', () => void loadEvents(true)); eventsMore.addEventListener('click', () => void loadEvents(false));
    update();
    return { panel, refreshObservation,
      setEnabled(value) {
        const previous = enabled; enabled = value; panel.hidden = !value; update();
        if (!value) { stopPolicyWatching(); ++generation; ++eventsGeneration; snapshot = null; fetching = false; eventsBusy = false; }
        else if (!previous) {
          document.addEventListener('visibilitychange', visibilityChanged); observingVisibility = true;
          void load(); void loadEvents(true); schedulePolicyPolling();
        }
      },
      selectionChanged() {
        const previous = workerSelect.value;
        workerSelect.replaceChildren(option('', 'Choose a Team worker', true));
        for (const worker of taskContext?.workers || []) workerSelect.append(option(worker.id, `${worker.name || worker.id} — ${worker.id}`));
        workerSelect.value = (taskContext?.workers || []).some(worker => worker.id === previous) ? previous : '';
        update(); if (enabled && loadedKey !== keyForScope()) void load();
      },
      destroy() { stopPolicyWatching(); ++generation; ++eventsGeneration; },
    };
  }

  function createProbePanel() {
    let enabled = false, models = [], description = null, describing = false, starting = false, cancelling = false;
    let describeGeneration = 0, modelGeneration = 0, listGeneration = 0, operationGeneration = 0, pollTimer = null, operation = null, history = [];
    let historyCursor = null, historyLoading = false, moreLoading = false, pagingBlocked = false, explicitSelection = false;
    let seenCursors = new Set();
    const active = value => ['queued', 'running', 'cancel_requested'].includes(value?.status);
    const identity = value => JSON.stringify([value.endpoint_id, value.model]);
    const selected = () => models.find(value => identity(value) === modelSelect.value);
    const displayScope = scope => scope ? `${scope.model || ''} — ${scope.endpoint_id || ''}\n${scope.config_digest || ''}` : '';
    const panel = el('section', undefined, 'probe-panel', 'team-card team-panel'); panel.hidden = true;
    panel.append(uiEl('h4', 'Model and tool-call probe'), uiEl('p', 'Only synthetic local requests are sent. No project files or real tools are used. Results are observations for this exact configuration, not permissions or a general quality guarantee.'));
    const modelSelect = el('select', undefined, 'probe-model');
    modelSelect.append(option('', 'Choose endpoint / model', true));
    const modelScope = el('p', '', 'probe-model-scope');
    const describeButton = button('Refresh probe configuration', 'probe-refresh');
    const descriptionNode = el('div', undefined, 'probe-description', 'team-panel');
    const confirm = el('input', undefined, 'probe-confirm'); confirm.type = 'checkbox'; confirm.id = `${prefix}-probe-confirm`;
    const confirmLabel = el('label', undefined, undefined, 'team-check'); confirmLabel.htmlFor = confirm.id;
    confirmLabel.append(confirm, uiEl('span', 'I approve this bounded synthetic probe for the exact selected configuration.'));
    const start = button('Run model probe', 'probe-start');
    const probeNotice = el('p', '', 'probe-notice', 'team-notice'); probeNotice.setAttribute('role', 'status'); probeNotice.setAttribute('aria-live', 'polite');
    const historySelect = el('select', undefined, 'probe-history'), refreshHistory = button('Refresh probe operations', 'probe-refresh-operations');
    const moreHistory = button('Load more history', 'probe-more-history');
    const historyNotice = el('p', '', 'probe-history-notice', 'team-notice'); historyNotice.setAttribute('role', 'status');
    const operationStatus = el('p', '', 'probe-operation-status', 'team-status'), operationScope = el('p', '', 'probe-operation-scope');
    const operationId = el('p', '', 'probe-operation-id');
    const cancel = button('Cancel probe', 'probe-cancel');
    const result = el('div', undefined, 'probe-result', 'team-panel'); result.setAttribute('aria-live', 'polite');
    panel.append(field('Probe endpoint / model', modelSelect, 'probe-model'), modelScope, describeButton, descriptionNode, confirmLabel, start, probeNotice,
      field('Recent probe operations', historySelect, 'probe-history'), refreshHistory, moreHistory, historyNotice, operationStatus, operationId, operationScope, cancel, result);
    const say = (label, error = false) => { probeNotice.textContent = label; bindUiText(probeNotice, label); probeNotice.className = `team-notice${error ? ' team-error' : ''}`; };
    const supported = () => description?.supported === true && selected()?.local === true && typeof description.scope?.config_digest === 'string'
      && description.scope.config_digest.length > 0 && [description.max_requests, description.max_output_tokens_per_request, description.deadline_seconds].every(value => Number.isFinite(value) && value > 0);
    function controls() {
      modelSelect.disabled = !enabled || starting;
      describeButton.disabled = !enabled || !selected() || starting || describing;
      confirm.disabled = !enabled || starting || describing || !supported() || active(operation);
      start.disabled = confirm.disabled || !confirm.checked;
      cancel.disabled = !enabled || cancelling || !active(operation) || operation?.status === 'cancel_requested';
      historySelect.disabled = starting || cancelling || history.length === 0;
      refreshHistory.disabled = !enabled || starting || cancelling;
      moreHistory.hidden = !historyCursor && !pagingBlocked;
      moreHistory.disabled = !enabled || starting || cancelling || historyLoading || moreLoading || pagingBlocked || !historyCursor;
    }
    function historyOptions() {
      historySelect.replaceChildren(option('', 'Choose a probe operation', true), ...history.map(item => option(item.id, `${displayScope(item.scope || item.result?.scope).split('\n')[0]} — ${item.id}`)));
      historySelect.value = operation?.id || '';
    }
    function renderOperation() {
      const names = { queued: 'Queued', running: 'Running', cancel_requested: 'Cancellation requested', cancelled: 'Cancelled', interrupted: 'Interrupted', failed: 'Failed', completed: 'Completed' };
      const label = operation ? names[operation.status] || 'Unknown operation state' : 'No probe selected';
      operationStatus.textContent = label; bindUiText(operationStatus, label);
      operationId.textContent = operation?.id || ''; operationScope.textContent = displayScope(operation?.scope || operation?.result?.scope);
      result.replaceChildren();
      const data = operation?.result;
      if (data?.status === 'stale' || (description && data?.scope?.config_digest && data.scope.config_digest !== description.scope?.config_digest)) {
        result.append(uiEl('p', 'Stale or different configuration: refresh and confirm again before a new probe.'));
      }
      for (const [key, title] of [['streaming', 'Streaming'], ['native_tools', 'Native tool calls'], ['tool_roundtrip', 'Tool-result round trip'], ['usage', 'Token usage reporting']]) {
        const row = el('p'), value = data?.capabilities?.[key];
        row.append(uiEl('strong', title), el('span', ': '), uiEl('span', value === true ? 'Observed' : value === false ? 'Not observed' : 'Unverified', `probe-capability-${key}`)); result.append(row);
      }
      if (data?.reason) result.append(el('p', String(data.reason)));
      if (operation?.error) result.append(el('p', typeof operation.error === 'string' ? operation.error : message(operation.error), undefined, 'team-error'));
      for (const [key, title] of [['requests', 'Requests sent'], ['completed_requests', 'Requests completed'], ['elapsed_seconds', 'Elapsed seconds']]) {
        if (Number.isFinite(data?.measurements?.[key])) { const row = el('p'); row.append(uiEl('strong', title), el('span', `: ${data.measurements[key]}`)); result.append(row); }
      }
      controls();
    }
    function stopPolling() { if (pollTimer !== null) clearTimeout(pollTimer); pollTimer = null; }
    function remember(value) {
      history = history.some(item => item.id === value.id) ? history.map(item => item.id === value.id ? value : item) : [value, ...history];
      operation = value; historyOptions(); renderOperation();
    }
    async function poll(generation) {
      const id = operation?.id;
      if (disposed || !enabled || generation !== operationGeneration || !id) return;
      stopPolling();
      try {
        const value = await request(`${API}/operations/${encodeURIComponent(id)}`, { method: 'GET' });
        if (disposed || !enabled || generation !== operationGeneration || operation?.id !== id) return;
        if (value?.id !== id) throw new Error('Operation identity mismatch');
        remember(value);
        if (active(value)) pollTimer = setTimeout(() => void poll(generation), 1000);
      } catch (error) { if (!disposed && generation === operationGeneration) say(message(error), true); }
    }
    function selectOperation(value) {
      stopPolling(); ++operationGeneration; operation = value || null; historyOptions(); renderOperation();
      if (operation) void poll(operationGeneration);
    }
    function historyMessage(label, error = null) {
      historyNotice.replaceChildren(uiEl('span', label));
      if (error) historyNotice.append(el('span', `: ${message(error)}`));
      historyNotice.className = `team-notice${error ? ' team-error' : ''}`;
    }
    function historyPage(data) {
      if (!Array.isArray(data?.operations) || (data.next_cursor != null && (typeof data.next_cursor !== 'string' || !data.next_cursor))) throw new Error('Invalid probe history response');
      return { operations: data.operations.filter(item => typeof item?.id === 'string' && item.id && (!item.kind || item.kind === 'model_probe')), cursor: data.next_cursor || null };
    }
    function mergeHistory(...groups) {
      const merged = new Map();
      for (const group of groups) for (const value of group) if (!merged.has(value.id)) merged.set(value.id, value);
      history = [...merged.values()];
    }
    async function loadHistory() {
      const generation = ++listGeneration, selectionAtStart = operationGeneration;
      historyLoading = true; moreLoading = false; historyMessage('Loading probe history…'); controls();
      try {
        const [recentData, activeData] = await Promise.all([
          request(`${API}/operations?kind=model_probe&limit=50`, { method: 'GET' }),
          request(`${API}/operations?kind=model_probe&active_only=true&limit=1`, { method: 'GET' }),
        ]);
        if (disposed || !enabled || generation !== listGeneration) return;
        const recent = historyPage(recentData), running = historyPage(activeData).operations.filter(active);
        mergeHistory(recent.operations, running, operation ? [operation] : []);
        if (operation) history = history.map(item => item.id === operation.id ? operation : item);
        historyCursor = recent.cursor; seenCursors = new Set(); pagingBlocked = false;
        const restored = explicitSelection ? operation : running[0] || operation || recent.operations.find(active) || recent.operations[0];
        if (selectionAtStart === operationGeneration) selectOperation(restored);
        else { historyOptions(); controls(); }
        historyMessage('Probe history loaded.');
      } catch (error) { if (!disposed && enabled && generation === listGeneration) historyMessage('Unable to load probe history. Refresh to retry.', error); }
      finally { if (!disposed && generation === listGeneration) { historyLoading = false; controls(); } }
    }
    async function loadMoreHistory() {
      if (disposed || !enabled || starting || cancelling || historyLoading || moreLoading || pagingBlocked || !historyCursor) return;
      const generation = listGeneration, cursor = historyCursor;
      moreLoading = true; historyMessage('Loading probe history…'); controls();
      try {
        const data = await request(`${API}/operations?kind=model_probe&limit=50&after_id=${encodeURIComponent(cursor)}`, { method: 'GET' });
        if (disposed || !enabled || generation !== listGeneration) return;
        const page = historyPage(data);
        if (page.cursor && (page.cursor === cursor || seenCursors.has(page.cursor))) {
          pagingBlocked = true; historyMessage('History cursor did not advance. Refresh history before loading more.'); return;
        }
        mergeHistory(history, page.operations); seenCursors.add(cursor); historyCursor = page.cursor;
        historyOptions(); historyMessage('Probe history loaded.');
      } catch (error) { if (!disposed && enabled && generation === listGeneration) historyMessage('Unable to load more history. Try again.', error); }
      finally { if (!disposed && generation === listGeneration) { moreLoading = false; controls(); } }
    }
    async function describeSelected() {
      const generation = ++describeGeneration, model = selected();
      description = null; confirm.checked = false; descriptionNode.replaceChildren(); modelScope.textContent = displayScope(model); describing = !!model; controls();
      if (!model) return;
      descriptionNode.append(uiEl('p', 'Loading probe configuration…'));
      try {
        const data = await request(`${API}/model-probe?endpoint_id=${encodeURIComponent(model.endpoint_id)}&model=${encodeURIComponent(model.model)}`, { method: 'GET' });
        if (disposed || !enabled || generation !== describeGeneration || selected() !== model) return;
        if (data.scope?.endpoint_id !== model.endpoint_id || data.scope?.model !== model.model) throw new Error('Probe configuration identity mismatch');
        description = data; descriptionNode.replaceChildren(el('p', displayScope(data.scope)));
        if (data.reason) descriptionNode.append(el('p', String(data.reason)));
        for (const [key, title] of [['max_requests', 'Maximum requests'], ['max_output_tokens_per_request', 'Output token limit per request'], ['deadline_seconds', 'Deadline in seconds']]) {
          const row = el('p'); row.append(uiEl('strong', title), el('span', `: ${data[key] ?? '—'}`)); descriptionNode.append(row);
        }
        if (model.local !== true) descriptionNode.append(uiEl('p', 'External model probes are unavailable. No paid request is sent.'));
        say('Review the exact configuration and limits before confirming.');
      } catch (error) { if (!disposed && generation === describeGeneration) { descriptionNode.replaceChildren(); say(message(error), true); } }
      finally { if (!disposed && generation === describeGeneration) { describing = false; controls(); } }
    }
    async function discoverModels() {
      const generation = ++modelGeneration;
      try {
        const data = await request('/api/team/models', { method: 'GET' });
        if (disposed || !enabled || generation !== modelGeneration) return;
        models = (data.models || []).filter(item => typeof item.endpoint_id === 'string' && typeof item.model === 'string');
        modelSelect.replaceChildren(option('', 'Choose endpoint / model', true), ...models.map(item => option(identity(item), `${item.model} — ${item.label || item.endpoint_id} [${item.endpoint_id}]`)));
        controls();
      } catch (error) { if (!disposed && enabled && generation === modelGeneration) say(message(error), true); }
    }
    modelSelect.addEventListener('change', () => { if (!disposed && !starting) void describeSelected(); });
    describeButton.addEventListener('click', () => { if (!disposed && !starting) void describeSelected(); });
    confirm.addEventListener('change', controls);
    historySelect.addEventListener('change', () => { if (!disposed && !starting && !cancelling) { explicitSelection = true; selectOperation(history.find(item => item.id === historySelect.value)); } });
    refreshHistory.addEventListener('click', () => { if (!disposed && enabled && !starting && !cancelling) void loadHistory(); });
    moreHistory.addEventListener('click', () => void loadMoreHistory());
    start.addEventListener('click', async () => {
      if (disposed || !enabled || starting || describing || !confirm.checked || !supported() || active(operation)) return;
      const model = selected(), body = { endpoint_id: model.endpoint_id, model: model.model, confirmation: true, expected_config_digest: description.scope.config_digest };
      starting = true; confirm.checked = false; ++listGeneration; historyLoading = false; moreLoading = false; controls(); say('Starting probe…');
      try {
        const value = await request(`${API}/model-probe`, { method: 'POST', body });
        if (disposed || !enabled) return;
        if (typeof value?.id !== 'string') throw new Error('Probe did not return an operation ID');
        explicitSelection = true; remember(value); selectOperation(value); say('Probe started. Leaving this panel does not cancel it.');
      } catch (error) {
        if (disposed) return;
        if (Number(error?.status || error?.statusCode || error?.response?.status) === 409) { description = null; say('Configuration changed. Refresh and confirm again; no automatic retry was made.', true); }
        else say(message(error), true);
      } finally { if (!disposed) { starting = false; controls(); } }
    });
    cancel.addEventListener('click', async () => {
      if (disposed || !enabled || cancelling || !active(operation) || operation.status === 'cancel_requested') return;
      const id = operation.id, generation = ++operationGeneration; stopPolling(); cancelling = true; controls();
      try {
        const value = await request(`${API}/operations/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {} });
        if (disposed || !enabled || generation !== operationGeneration) return;
        if (value?.id !== id) throw new Error('Operation identity mismatch');
        remember(value); say('Cancellation requested. Waiting for the server to confirm the outcome.');
      } catch (error) { if (!disposed && generation === operationGeneration) say(message(error), true); }
      finally { if (!disposed && generation === operationGeneration) { cancelling = false; controls(); void poll(generation); } }
    });
    renderOperation();
    return { panel,
      setEnabled(value) {
        const previous = enabled; enabled = value; panel.hidden = !value; controls();
        if (!value) { stopPolling(); ++describeGeneration; ++modelGeneration; ++operationGeneration; ++listGeneration; description = null; describing = false; confirm.checked = false; return; }
        if (!previous) { void discoverModels(); void loadHistory(); }
      },
      destroy() { stopPolling(); ++describeGeneration; ++modelGeneration; ++operationGeneration; ++listGeneration; },
    };
  }

  function updateControls() {
    if (disposed) return;
    refresh.disabled = loading || creating || applying;
    createButton.disabled = !feature('projects') || loading || creating || !hosts.length;
    createButton.textContent = creating ? 'Creating project…' : 'Create read-only project';
    bindUiText(createButton, creating ? 'Creating project…' : 'Create read-only project');
    projectSelect.disabled = loading || !projects.length;
    isolated.disabled = !feature('isolated_execution');
    policyMode.disabled = !feature('policy') || !current() || loading || applying;
    consent.disabled = policyMode.disabled || staleProjects.has(selectedId);
    const selectedMode = policyMode.value;
    policyButton.disabled = consent.disabled || !consent.checked || !['trusted_host', 'isolated'].includes(selectedMode);
    const policyLabel = selectedMode === 'isolated' ? 'Confirm isolated access' : 'Confirm trusted-host access';
    policyButton.textContent = applying ? 'Saving policy…' : policyLabel;
    bindUiText(policyButton, applying ? 'Saving policy…' : policyLabel);
    section.setAttribute('aria-busy', String(loading || creating || applying));
  }

  function resetConsent() { consent.checked = false; policyMode.value = ''; }

  function populateProjects() {
    projectSelect.replaceChildren(option('', 'Legacy / no Engineering project', true), ...projects.map(project => option(String(project.id), `${project.name} — ${hosts.find(host => host.id === project.host_id)?.name || project.host_id}`)));
    if (!projects.some(project => String(project.id) === selectedId)) selectedId = '';
    projectSelect.value = selectedId;
  }

  async function showProject() {
    if (disposed) return;
    const generation = ++toolsGeneration, project = current();
    resetConsent(); updateControls(); projectDetails.replaceChildren(); toolList.replaceChildren();
    contextPolicy.selectionChanged();
    lsp.selectionChanged();
    checkProfiles.selectionChanged();
    requirements.selectionChanged();
    projectMemory.selectionChanged();
    baselineComparison.selectionChanged();
    // Publish only a canonical loaded selection, never a delayed tool response.
    onProjectSelected(project ? { ...project } : null);
    if (disposed || generation !== toolsGeneration) return;
    if (!project) { projectDetails.append(uiEl('p', 'No Engineering project selected. New Team runs use legacy settings.')); return; }
    const access = project.access_mode === 'trusted_host' ? 'Trusted host (not isolated)' : project.access_mode === 'isolated' ? 'Isolated runner: approved checks run only in a verification copy.' : 'Read-only — host access has not been approved';
    projectDetails.append(el('p', project.name));
    for (const [label, value, authored] of [['Host', hosts.find(host => host.id === project.host_id)?.name || project.host_id], ['Folder', project.root], ['Policy', access, true], ['Revision', project.revision]]) {
      const row = el('p'); row.append(uiEl('span', label), el('span', ': '), authored ? uiEl('span', value) : el('span', String(value))); projectDetails.append(row);
    }
    if (!feature('tool_catalog')) return;
    toolList.append(uiEl('p', 'Loading tools…'));
    try {
      const data = await request(`${API}/tools?project_id=${encodeURIComponent(project.id)}`, { method: 'GET' });
      if (disposed || generation !== toolsGeneration || selectedId !== String(project.id)) return;
      toolList.replaceChildren();
      for (const tool of data.tools || []) {
        const row = el('article', undefined, undefined, 'team-card');
        const availability = el('p'); availability.append(uiEl('span', tool.available === true ? 'Available' : 'Unavailable'), el('span', ' · '), tool.effect ? el('span', tool.effect) : uiEl('span', 'Effect not specified'));
        row.append(el('strong', tool.name || tool.id), availability);
        row.append(tool.reason ? el('p', tool.reason) : uiEl('p', tool.available === true ? 'Available under this project policy.' : 'No availability reason was provided by the server.'));
        toolList.append(row);
      }
      if (!toolList.children.length) toolList.append(uiEl('p', 'No tools reported for this project.'));
    } catch (error) {
      if (!disposed && generation === toolsGeneration) toolList.replaceChildren(uiEl('span', 'Unable to load tools'), el('span', `: ${message(error)}`));
    }
  }

  async function discover() {
    if (disposed || loading || creating || applying) return;
    const generation = ++discoveryGeneration;
    loading = true; updateControls(); notice('Loading engineering capabilities…');
    try {
      const found = await request(`${API}/capabilities`, { method: 'GET' });
      if (disposed || generation !== discoveryGeneration) return;
      capabilities = found;
      if (contextOnly) {
        content.hidden = !feature('context_policy');
        contextPolicy.setEnabled(feature('context_policy'));
        notice(feature('context_policy') ? 'Review values and explicitly select the overrides to save.' : 'Context settings are unavailable on this server.');
        return;
      }
      content.hidden = !feature('projects') && !feature('model_probe') && !feature('context_policy') && !feature('check_profiles') && !feature('check_runs') && !feature('requirements') && !feature('project_memory') && !feature('baseline_comparison') && !feature('reviewed_mcp');
      probe.setEnabled(feature('model_probe'));
      contextPolicy.setEnabled(feature('context_policy'));
      lsp.setEnabled(feature('lsp'));
      checkProfiles.setEnabled(feature('check_profiles'));
      checkRuns.setEnabled(feature('check_runs'));
      requirements.setEnabled(feature('requirements'));
      projectMemory.setEnabled(feature('project_memory'));
      baselineComparison.setEnabled(feature('baseline_comparison'));
      mcpReviews.setEnabled(feature('reviewed_mcp'));
      createForm.hidden = !feature('projects'); policyForm.hidden = !feature('policy'); toolCard.hidden = !feature('tool_catalog');
      if (!feature('projects')) { selectedId = ''; ++toolsGeneration; lsp.selectionChanged(); checkProfiles.selectionChanged(); requirements.selectionChanged(); projectMemory.selectionChanged(); baselineComparison.selectionChanged(); onProjectSelected(null); notice('Engineering projects are unavailable or disabled on this server.'); return; }
      const [hostData, projectData] = await Promise.all([
        request(`${API}/hosts`, { method: 'GET' }), request(`${API}/projects`, { method: 'GET' }),
      ]);
      if (disposed || generation !== discoveryGeneration) return;
      hosts = hostData.hosts || []; projects = projectData.projects || [];
      const draftHost = hostSelect.value;
      hostSelect.replaceChildren(option('', 'Choose a host', true), ...hosts.map(host => option(host.id, `${host.name} — ${host.platform || '—'} · ${host.status || '—'}`)));
      hostSelect.value = hosts.some(host => host.id === draftHost) ? draftHost : '';
      staleProjects.clear(); populateProjects(); void showProject();
      notice(hosts.length ? 'Review a project or register a new read-only project.' : 'No hosts are configured. Project creation is unavailable.');
    } catch (error) {
      if (!disposed && generation === discoveryGeneration) notice(message(error), true, true);
    } finally {
      if (!disposed && generation === discoveryGeneration) { loading = false; updateControls(); }
    }
  }

  createForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (disposed || creating || loading || !feature('projects')) return;
    const body = { name: nameInput.value.trim(), root: rootInput.value.trim(), host_id: hostSelect.value };
    if (!body.name || !hosts.some(host => host.id === body.host_id) || !/^(\/|[A-Za-z]:[\\/]|\\\\)/.test(body.root)) {
      notice('Enter a project name, choose a configured host and provide an absolute project folder.', true); return;
    }
    creating = true; updateControls(); notice('Registering a read-only project…');
    try {
      const project = await request(`${API}/projects`, { method: 'POST', body });
      if (disposed) return;
      projects = [...projects.filter(item => String(item.id) !== String(project.id)), project];
      selectedId = String(project.id); populateProjects(); void showProject();
      notice('Project registered. No trusted-host access has been granted.');
    } catch (error) { if (!disposed) notice(message(error), true, true); }
    finally { if (!disposed) { creating = false; updateControls(); } }
  });

  projectSelect.addEventListener('change', () => {
    selectedId = projectSelect.value; notice('Review the selected project policy.'); void showProject();
  });
  policyMode.addEventListener('change', () => { consent.checked = false; updateControls(); });
  consent.addEventListener('change', updateControls);
  policyForm.addEventListener('submit', async event => {
    event.preventDefault();
    const project = current();
    const accessMode = policyMode.value;
    if (disposed || applying || loading || !feature('policy') || !project || !consent.checked || !['trusted_host', 'isolated'].includes(accessMode) || (accessMode === 'isolated' && !feature('isolated_execution')) || staleProjects.has(selectedId)) return;
    // Pin the exact project/revision the user reviewed; never retry a conflict automatically.
    const projectId = String(project.id), body = { expected_revision: project.revision, access_mode: accessMode, confirmation: true };
    applying = true; updateControls(); notice('Saving the confirmed project policy…');
    try {
      const updated = await request(`${API}/projects/${encodeURIComponent(projectId)}/policy`, { method: 'POST', body });
      if (disposed) return;
      projects = projects.map(item => String(item.id) === projectId ? updated : item);
      if (selectedId === projectId) { void showProject(); notice(accessMode === 'isolated' ? 'Isolated policy saved. Approved checks run in a verification copy.' : 'Trusted-host policy saved. This is not an isolated environment.'); }
    } catch (error) {
      if (disposed) return;
      const conflict = Number(error?.status || error?.statusCode || error?.response?.status) === 409;
      if (conflict) staleProjects.add(projectId);
      if (selectedId === projectId) notice(conflict ? 'Project policy changed elsewhere (409 conflict). Refresh projects, review the new revision and confirm again. No automatic retry was performed.' : message(error), true, !conflict);
    } finally {
      if (!disposed) { applying = false; resetConsent(); updateControls(); }
    }
  });
  refresh.addEventListener('click', () => void discover());
  updateControls(); void discover();
  const destroy = () => { disposed = true; probe.destroy(); contextPolicy.destroy(); lsp.destroy(); checkProfiles.destroy(); checkRuns.destroy(); requirements.destroy(); projectMemory.destroy(); baselineComparison.destroy(); mcpReviews.destroy(); ++toolsGeneration; ++discoveryGeneration; section.remove(); };
  destroy.setTaskContext = value => {
    if (disposed || JSON.stringify(taskContext) === JSON.stringify(value)) return;
    taskContext = value; contextPolicy.selectionChanged();
  };
  destroy.refreshContextObservation = () => contextPolicy.refreshObservation();
  destroy.setSessionId = value => {
    const next = typeof value === 'string' ? value : '';
    if (disposed || chatId === next) return;
    chatId = next; contextPolicy.selectionChanged();
  };
  return destroy;
}
