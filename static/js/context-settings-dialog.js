import { mountEngineeringWorkspace } from './engineering-workspace.js?v=20260923context1';
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
  // Open the shell before mounting.  A synchronous mount/import failure must
  // remain visible as an actionable dialog instead of making the button look
  // dead (the old order never reached showModal()).
  try {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  } catch (error) {
    console.error('[context-settings] dialog open failed:', error);
    dialog.setAttribute('open', '');
  }
  try {
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
  } catch (error) {
    console.error('[context-settings] workspace mount failed:', error);
    const message = document.createElement('p');
    message.textContent = 'Context settings could not be loaded. Refresh the page and try again.';
    bindUiText(message, 'Context settings could not be loaded. Refresh the page and try again.');
    message.setAttribute('role', 'alert');
    root.replaceChildren(message);
  }
  timer = setInterval(() => { if (getSessionId() !== sessionId) cleanup(); }, 100);
  close.focus();
  return cleanup;
}
