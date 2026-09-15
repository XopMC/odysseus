import { bindUiText, t } from './i18n.js';

const api = window.location.origin;
let projects = [];
const el = id => document.getElementById(id);
const request = async (url, options = {}) => {
  const res = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
};
const post = (url, body) => request(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });

function chatRow(chat) {
  const row = document.createElement('button');
  row.type = 'button'; row.className = 'list-item project-chat-row'; row.dataset.sessionId = chat.id;
  row.innerHTML = `<span class="grow"></span>${window.chatModule?.hasActiveStream?.(chat.id) ? '<span class="sidebar-notif-dot"></span>' : ''}`;
  row.querySelector('.grow').textContent = chat.name || t('Untitled chat');
  row.addEventListener('click', () => window.sessionModule?.selectSession?.(chat.id));
  return row;
}

function projectRow(project) {
  const wrap = document.createElement('div'); wrap.className = 'project-sidebar-item';
  const head = document.createElement('button'); head.type = 'button'; head.className = 'list-item project-sidebar-head';
  head.innerHTML = '<span class="project-chevron">›</span><span class="grow"></span><span class="project-open" aria-hidden="true">•••</span><span class="project-new-chat">+</span>';
  head.querySelector('.grow').textContent = project.name;
  const chats = document.createElement('div'); chats.className = 'project-chat-list hidden';
  const add = head.querySelector('.project-new-chat');
  add.title = t('New chat in project');
  add.addEventListener('click', event => {
    event.stopPropagation();
    sessionStorage.setItem('odysseus-pending-project-id', project.id);
    el('sidebar-new-chat-btn')?.click();
  });
  const open = head.querySelector('.project-open');
  open.title = t('Open project');
  open.addEventListener('click', event => { event.stopPropagation(); void openProject(project); });
  head.addEventListener('click', () => {
    chats.classList.toggle('hidden');
    head.querySelector('.project-chevron').textContent = chats.classList.contains('hidden') ? '›' : '⌄';
  });
  const projectChats = window.sessionModule?.getSessions?.().filter(chat => chat.project_id === project.id && !chat.archived) || [];
  if (!projectChats.length) {
    const empty = document.createElement('div'); empty.className = 'project-empty'; empty.textContent = t('No chats yet'); chats.appendChild(empty);
  } else projectChats.forEach(chat => chats.appendChild(chatRow(chat)));
  wrap.append(head, chats); return wrap;
}

function detailsModal() {
  let node = el('project-details-modal');
  if (node) return node;
  node = document.createElement('div'); node.id = 'project-details-modal'; node.className = 'modal hidden';
  node.innerHTML = `<div class="modal-content project-details-dialog" role="dialog" aria-modal="true" aria-label="Project details">
    <div class="modal-header"><h4 class="project-details-title" data-i18n-ignore>Project</h4><button type="button" class="close-btn">×</button></div>
    <div class="project-tabs" role="tablist"><button type="button" data-project-tab="chats">Chats</button><button type="button" data-project-tab="memory">Memory</button><button type="button" data-project-tab="skills">Skills</button><button type="button" data-project-tab="access">Access settings</button></div>
    <div class="modal-body"><section data-project-panel="chats"><button type="button" id="project-details-new-chat">New chat in project</button><div id="project-details-chats"></div></section>
    <section data-project-panel="memory" hidden><p>Project memory is isolated from other projects.</p><div id="project-details-memory"></div></section>
    <section data-project-panel="skills" hidden><p>Project skills are untrusted instructions and never grant permissions.</p><button type="button" id="project-skills-import">Import project SKILL.md</button><div id="project-details-skills"></div></section>
    <section data-project-panel="access" hidden><div id="project-details-access"></div></section></div></div>`;
  document.body.appendChild(node);
  node.querySelectorAll('h4,button,p').forEach(item => { const source = item.textContent.trim(); if (source) bindUiText(item, source); });
  bindUiText(node.querySelector('[role="dialog"]'), 'Project details', 'aria-label');
  node.querySelector('.close-btn').addEventListener('click', () => node.classList.add('hidden'));
  node.querySelectorAll('[data-project-tab]').forEach(tab => tab.addEventListener('click', () => {
    node.querySelectorAll('[data-project-panel]').forEach(panel => { panel.hidden = panel.dataset.projectPanel !== tab.dataset.projectTab; });
    node.querySelectorAll('[data-project-tab]').forEach(item => item.classList.toggle('active', item === tab));
  }));
  node.querySelector('[data-project-tab="chats"]').classList.add('active');
  return node;
}

async function loadProjectDetails(project) {
  const node = detailsModal(); node.dataset.projectId = project.id;
  node.querySelector('.modal-header h4').textContent = project.name;
  const [chatData, memoryData, skillData] = await Promise.all([
    request(`${api}/api/projects/${encodeURIComponent(project.id)}/chats`),
    request(`${api}/api/projects/${encodeURIComponent(project.id)}/memory?limit=200`),
    request(`${api}/api/projects/${encodeURIComponent(project.id)}/skills?limit=200`),
  ]);
  const chats = el('project-details-chats'); chats.replaceChildren(...(chatData.chats || []).map(chatRow));
  if (!chats.children.length) chats.textContent = t('No chats yet');
  const memories = el('project-details-memory'); memories.replaceChildren();
  for (const item of memoryData.items || []) {
    const row = document.createElement('article'); row.className = 'project-detail-row';
    const title = document.createElement('strong'); title.textContent = `${item.kind} · ${item.state}`;
    const text = document.createElement('div'); text.textContent = item.text;
    const source = document.createElement('small'); source.textContent = item.source;
    row.append(title, text, source); memories.appendChild(row);
  }
  if (!memories.children.length) memories.textContent = t('No project memory yet');
  const skills = el('project-details-skills'); skills.replaceChildren();
  for (const item of skillData.skills || []) {
    const row = document.createElement('article'); row.className = 'project-detail-row';
    const title = document.createElement('strong'); title.textContent = item.name;
    const source = document.createElement('small'); source.textContent = `${item.source} · ${item.digest.slice(0, 12)} · r${item.revision}`;
    row.append(title, source); skills.appendChild(row);
  }
  if (!skills.children.length) skills.textContent = t('No project skills yet');
  el('project-details-access').textContent = `${t('Execution host')}: ${project.host_id}\n${t('Project folder')}: ${project.root}\n${t('Access mode')}: ${project.access_mode || 'read_only'}`;
}

async function openProject(project) {
  const node = detailsModal(); node.classList.remove('hidden');
  try { await loadProjectDetails(project); }
  catch (error) { window.uiModule?.showError?.(t(error.message)); }
  el('project-details-new-chat').onclick = () => {
    sessionStorage.setItem('odysseus-pending-project-id', project.id);
    node.classList.add('hidden'); el('sidebar-new-chat-btn')?.click();
  };
  el('project-skills-import').onclick = async () => {
    try {
      const result = await post(`${api}/api/projects/${encodeURIComponent(project.id)}/skills/import`, {});
      window.uiModule?.showToast?.(`${t('Imported project skills')}: ${result.count}`);
      await loadProjectDetails(project);
    } catch (error) { window.uiModule?.showError?.(t(error.message)); }
  };
}

function render() {
  const list = el('project-list'); if (!list) return;
  list.replaceChildren(...projects.map(projectRow));
  el('projects-section')?.classList.toggle('hidden', projects.length === 0 && !el('project-create-btn'));
}

async function refresh() {
  try {
    const data = await request(`${api}/api/projects?limit=200`);
    projects = data.projects || [];
    render();
  } catch (error) {
    if (!/disabled|404/i.test(error.message)) console.warn('[projects]', error);
  }
}

function modal() {
  let node = el('project-create-modal');
  if (node) return node;
  node = document.createElement('div'); node.id = 'project-create-modal'; node.className = 'modal hidden';
  node.innerHTML = `<div class="modal-content project-create-dialog" role="dialog" aria-modal="true" aria-label="Create project">
    <div class="modal-header"><h4>Create project</h4><button type="button" class="close-btn">×</button></div>
    <div class="modal-body"><label>Project name<input id="project-name" maxlength="200"></label>
    <label>Execution host<select id="project-host"></select></label>
    <label>Project folder<div class="project-path-row"><input id="project-root" value="/home/xopmc"><button type="button" id="project-browse">Browse</button></div></label>
    <div id="project-folders" class="project-folder-list"></div>
    <label>Access mode<select id="project-access"><option value="read_only">Read-only</option><option value="trusted_host">Trusted host</option><option value="isolated">Isolation</option></select></label>
    <p class="project-safety">Selecting a folder does not grant execution. Trusted host and isolation are explicit project permissions.</p>
    <div class="chat-work-actions"><button type="button" id="project-save">Create</button><button type="button" class="project-close">Cancel</button></div></div></div>`;
  document.body.appendChild(node);
  node.querySelectorAll('h4,label,button,option,p').forEach(item => { const source = item.textContent.trim(); if (source) bindUiText(item, source); });
  const close = () => node.classList.add('hidden');
  node.querySelector('.close-btn').addEventListener('click', close); node.querySelector('.project-close').addEventListener('click', close);
  el('project-browse').addEventListener('click', async () => {
    const host_id = el('project-host').value, path = el('project-root').value.trim();
    try {
      const data = await post(`${api}/api/projects/browse`, {host_id, path});
      el('project-root').value = data.path;
      const list = el('project-folders'); list.replaceChildren();
      if (data.parent && data.parent !== '/') {
        const up = document.createElement('button'); up.type='button'; up.textContent='..'; up.addEventListener('click', () => { el('project-root').value=data.parent; el('project-browse').click(); }); list.appendChild(up);
      }
      for (const folder of data.directories || []) {
        const button = document.createElement('button'); button.type='button'; button.textContent=folder.name;
        button.addEventListener('click', () => { el('project-root').value=folder.path; el('project-browse').click(); }); list.appendChild(button);
      }
    } catch (error) { window.uiModule?.showError?.(t(error.message)); }
  });
  el('project-save').addEventListener('click', async () => {
    try {
      await post(`${api}/api/projects`, {name:el('project-name').value.trim(), root:el('project-root').value.trim(), host_id:el('project-host').value, access_mode:el('project-access').value});
      close(); await refresh(); window.uiModule?.showToast?.(t('Project created'));
    } catch (error) { window.uiModule?.showError?.(t(error.message)); }
  });
  return node;
}

async function openCreate() {
  const node = modal();
  try {
    const data = await request(`${api}/api/projects/hosts`);
    const select = el('project-host'); select.replaceChildren();
    for (const host of data.hosts || []) { const option=document.createElement('option'); option.value=host.id; option.textContent=host.name; select.appendChild(option); }
    node.classList.remove('hidden'); el('project-name').focus();
  } catch (error) { window.uiModule?.showError?.(t(error.message)); }
}

function bind() {
  const create = el('project-create-btn');
  create?.addEventListener('click', openCreate);
  if (create) { bindUiText(create, 'Create project', 'title'); bindUiText(create, 'Create project', 'aria-label'); }
  const title = el('projects-section')?.querySelector('.section-title');
  if (title) {
    title.tabIndex = 0; title.setAttribute('role', 'button'); title.setAttribute('aria-expanded', 'true');
    const toggle = () => {
      const list = el('project-list'); if (!list) return;
      list.classList.toggle('hidden'); title.setAttribute('aria-expanded', String(!list.classList.contains('hidden')));
    };
    title.addEventListener('click', event => { if (!event.target.closest('#project-create-btn')) toggle(); });
    title.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); } });
  }
  refresh();
}
export default { bind, refresh, render, openCreate, openProject, getProjects: () => projects };
