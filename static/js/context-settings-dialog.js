import { mountEngineeringWorkspace } from './engineering-workspace.js';
import { bindUiText } from './i18n.js';

let closeCurrent = null;

// Reuses the same controls, policy API and validation as Engineering; no Team run.
export function openContextSettings({ getSessionId, fetchImpl = globalThis.fetch, onSaved = () => {} }) {
  closeCurrent?.();
  const sessionId = getSessionId();
  if (!sessionId) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'context-settings-dialog';
  dialog.style.cssText = 'width:min(920px,94vw);max-height:90dvh;overflow:auto;background:var(--bg-secondary,#20242b);color:inherit;border:1px solid var(--border-color,#52616b);border-radius:12px;padding:16px;';
  const title = document.createElement('h2'); title.id = 'context-settings-title';
  title.textContent = 'Context settings'; bindUiText(title, 'Context settings');
  dialog.setAttribute('aria-labelledby', title.id);
  const close = document.createElement('button'); close.type = 'button';
  close.textContent = 'Close'; bindUiText(close, 'Close');
  const root = document.createElement('div'); dialog.append(title, close, root);
  document.body.append(dialog);
  let disposed = false, destroy = null, timer = null;
  const cleanup = () => {
    if (disposed) return;
    disposed = true; clearInterval(timer); destroy?.(); dialog.close(); dialog.remove();
    if (closeCurrent === cleanup) closeCurrent = null;
  };
  closeCurrent = cleanup;
  close.addEventListener('click', cleanup); dialog.addEventListener('close', cleanup);
  destroy = mountEngineeringWorkspace(root, { contextOnly: true, sessionId,
    request: async (path, options = {}) => {
      if (disposed || getSessionId() !== sessionId) throw new Error('Chat changed. Reopen context settings.');
      const response = await fetchImpl(path, { ...options, credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }) });
      const result = await response.json();
      if (!response.ok) { const error = new Error(result.detail || 'Request failed'); error.status = response.status; throw error; }
      if (!disposed && getSessionId() === sessionId && options.method === 'POST' && path === '/api/team/engineering/context-policy') onSaved();
      return result;
    } });
  timer = setInterval(() => { if (getSessionId() !== sessionId) cleanup(); }, 100);
  dialog.showModal(); close.focus();
  return cleanup;
}
