import { bindUiText, t } from './i18n.js';

const DEFAULT_MODE = 'ask_important';
const MODES = Object.freeze({
  ask_every_time: {
    label: 'Ask every time',
    description: 'Ask before every action that can change data, run code or access a service.',
  },
  ask_important: {
    label: 'Ask only important',
    description: 'Ask before code execution, writes, network side effects and destructive actions.',
  },
  full_access: {
    label: 'Full access',
    description: 'Run enabled tools without routine approval prompts for this account.',
  },
});

let apiBase = window.location.origin;
let state = { mode: DEFAULT_MODE, revision: 0 };
let refreshPromise = null;
let savePromise = null;
let bound = false;
let interval = null;
let menuHome = null;

const el = id => document.getElementById(id);
const normalize = value => Object.hasOwn(MODES, String(value || '').trim())
  ? String(value).trim() : DEFAULT_MODE;

function toast(message, error = false) {
  const ui = window.uiModule;
  (error ? ui?.showError : ui?.showToast)?.(t(message));
}

function render() {
  const button = el('access-mode-btn');
  const menu = el('access-mode-menu');
  if (!button || !menu) return;
  const mode = normalize(state.mode);
  button.dataset.accessMode = mode;
  button.classList.toggle('full-access', mode === 'full_access');
  button.setAttribute('aria-label', `${t('Access settings')}: ${t(MODES[mode].label)}`);
  button.title = `${t('Access settings')}: ${t(MODES[mode].label)}`;
  menu.querySelectorAll('[data-access-mode]').forEach(option => {
    const selected = option.dataset.accessMode === mode;
    option.classList.toggle('active', selected);
    option.setAttribute('aria-checked', String(selected));
  });
  menu.dataset.mode = mode;
}

async function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = fetch(`${apiBase}/api/prefs/access-mode`, {
    credentials: 'same-origin', cache: 'no-store',
  }).then(async response => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    state = { mode: normalize(data.mode), revision: Number.isInteger(data.revision) ? data.revision : 0 };
    render();
    return state;
  }).catch(error => {
    // A temporary auth/network failure must not erase the last known policy.
    console.warn('[access-mode] refresh failed', error);
    render();
    return state;
  }).finally(() => { refreshPromise = null; });
  return refreshPromise;
}

async function setMode(mode) {
  const desired = normalize(mode);
  if (savePromise || desired === normalize(state.mode)) {
    render();
    return state;
  }
  const previous = { ...state };
  state = { ...state, mode: desired };
  render();
  savePromise = fetch(`${apiBase}/api/prefs/access-mode`, {
    method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: desired, expected_revision: previous.revision }),
  }).then(async response => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 409) await refresh();
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    state = { mode: normalize(data.mode), revision: Number.isInteger(data.revision) ? data.revision : previous.revision + 1 };
    render();
    toast(`${t('Access settings')}: ${t(MODES[state.mode].label)}`);
    return state;
  }).catch(error => {
    state = previous;
    render();
    toast(error.message || 'Unable to save access settings', true);
    return state;
  }).finally(() => { savePromise = null; });
  return savePromise;
}

function positionMenu(menu, button) {
  const margin = 8;
  const rect = button.getBoundingClientRect();
  menu.classList.add('access-mode-menu-portal');
  menu.hidden = false;
  menu.style.visibility = 'hidden';
  menu.style.left = '-10000px';
  menu.style.top = '-10000px';
  const popup = menu.getBoundingClientRect();
  const maxLeft = Math.max(margin, window.innerWidth - popup.width - margin);
  const left = Math.min(Math.max(margin, rect.left), maxLeft);
  const above = rect.top - popup.height - margin;
  const below = rect.bottom + margin;
  const maxTop = Math.max(margin, window.innerHeight - popup.height - margin);
  const top = above >= margin ? above : Math.min(Math.max(margin, below), maxTop);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.style.visibility = '';
}

function openMenu() {
  const menu = el('access-mode-menu');
  const button = el('access-mode-btn');
  if (!menu || !button) return;
  if (!menuHome) menuHome = menu.parentElement;
  if (menu.parentElement !== document.body) document.body.appendChild(menu);
  positionMenu(menu, button);
  button.setAttribute('aria-expanded', 'true');
}

function closeMenu() {
  const menu = el('access-mode-menu');
  const button = el('access-mode-btn');
  if (!menu) return;
  menu.hidden = true;
  menu.classList.remove('access-mode-menu-portal');
  menu.style.left = '';
  menu.style.top = '';
  menu.style.visibility = '';
  if (menuHome && menu.parentElement !== menuHome) menuHome.appendChild(menu);
  button?.setAttribute('aria-expanded', 'false');
}

function bind() {
  if (bound) return;
  const button = el('access-mode-btn');
  const menu = el('access-mode-menu');
  if (!button || !menu) return;
  bound = true;
  bindUiText(menu.querySelector('.access-mode-menu-title'), 'Access settings');
  menu.querySelectorAll('[data-access-mode]').forEach(option => {
    const mode = MODES[option.dataset.accessMode];
    if (!mode) return;
    bindUiText(option.querySelector('strong'), mode.label);
    bindUiText(option.querySelector('small'), mode.description);
  });
  button.addEventListener('click', async event => {
    event.preventDefault();
    event.stopPropagation();
    if (!menu.hidden) { closeMenu(); return; }
    await refresh();
    openMenu();
  });
  menu.querySelectorAll('[data-access-mode]').forEach(option => {
    option.addEventListener('click', async event => {
      event.preventDefault();
      event.stopPropagation();
      await setMode(option.dataset.accessMode);
      closeMenu();
    });
  });
  document.addEventListener('click', event => {
    if (!event.target.closest?.('#access-mode-wrap, #access-mode-menu')) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeMenu();
  });
  ['focus', 'pageshow', 'online'].forEach(type => window.addEventListener(type, () => { void refresh(); }));
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') void refresh(); });
  const reposition = () => {
    if (!menu.hidden) positionMenu(menu, button);
  };
  window.addEventListener('resize', reposition);
  window.addEventListener('scroll', reposition, true);
  if (!interval) interval = window.setInterval(() => {
    if (document.visibilityState === 'visible' && !savePromise) void refresh();
  }, 30000);
  render();
}

const accessMode = {
  init(base = window.location.origin) { apiBase = base; bind(); void refresh(); return this; },
  refresh,
  setMode,
  getMode: () => normalize(state.mode),
  getState: () => ({ ...state }),
  appendToForm(form) { if (form?.append) form.append('access_mode', normalize(state.mode)); return form; },
  modes: MODES,
};

export default accessMode;
export { MODES, DEFAULT_MODE };
