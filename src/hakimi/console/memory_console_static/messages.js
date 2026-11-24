'use strict';

const el = id => document.getElementById(id);
const initialParams = new URLSearchParams(location.search);
const token = new URLSearchParams(location.hash.slice(1)).get('token');
if (token) {
  sessionStorage.setItem('memoryConsoleToken', token);
  history.replaceState(null, '', location.pathname + location.search);
}
let records = [];
let updated = '';
let directoryRequest = 0;
let messageRequest = 0;
let activeMessageRequests = 0;
let directoryPending = false;
let requestedSession = initialParams.get('session') || '';
const sessionLabels = new Map();
const labels = {received: '收到', sent: '已发送', queued: '进入队列'};

el('scope').value = initialParams.get('scope') === 'private' || requestedSession.startsWith('private:')
  ? 'private' : 'group';
el('query').value = initialParams.get('q') || '';
el('kind').value = initialParams.get('kind') || 'chat';

function headers() {
  return {Authorization: 'Bearer ' + (sessionStorage.getItem('memoryConsoleToken') || '')};
}

function addSession(session, name) {
  sessionLabels.set(session, name);
  if (!Array.from(el('session').options).some(option => option.value === session)) {
    el('session').append(new Option(name, session));
  }
}

function draw() {
  const query = el('query').value.trim().toLowerCase();
  const kind = el('kind').value;
  const rows = records.filter(row =>
    (!el('session').value || row.session === el('session').value) &&
    (kind === 'chat' ? row.kind !== 'queued' : row.kind === kind) &&
    [row.session, row.session_name, row.user_id, row.actor_id, row.actor, row.text, row.decision].join(' ').toLowerCase().includes(query));
  const fragment = document.createDocumentFragment();
  for (const row of rows) {
    const article = document.createElement('article');
    const badge = document.createElement('b');
    const metadata = document.createElement('small');
    const text = document.createElement('p');
    article.dataset.kind = row.kind;
    badge.textContent = labels[row.kind] || row.kind;
    metadata.textContent = [new Date(row.at * 1000).toLocaleString(),
      sessionLabels.get(row.session) || window.panelSessionName(row), row.actor].filter(Boolean).join(' · ');
    text.textContent = row.text || '（无文本）';
    article.append(badge, metadata, text);
    if (row.decision) {
      const detail = document.createElement('small');
      detail.textContent = row.decision;
      article.append(detail);
    }
    const isPrivate = row.session.startsWith('private:');
    const person = row.relation_person_id || '';
    const canOpenPerson = isPrivate && Boolean(person);
    const canOpenConversation = isPrivate && el('session').value !== row.session;
    if (!isPrivate || canOpenPerson || canOpenConversation) {
      const links = document.createElement('div');
      links.className = 'panel-context-links';
      const link = document.createElement('a');
      if (!isPrivate) {
        link.href = '/?' + new URLSearchParams({group: row.session});
        link.textContent = '查看会话关系';
      } else if (canOpenPerson) {
        link.href = '/?' + new URLSearchParams({person});
        link.textContent = '查看人物关系';
      } else {
        link.href = '/messages.html?' + new URLSearchParams({scope: 'private', session: row.session});
        link.textContent = '查看此私聊';
      }
      links.append(link);
      article.append(links);
    }
    fragment.append(article);
  }
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'panel-empty';
    empty.textContent = '当前筛选下没有消息。可切换会话、活动类型，或清空搜索。';
    fragment.append(empty);
  }
  el('rows').replaceChildren(fragment);
  el('status').textContent = rows.length + ' 条匹配 · 每个会话最多300条 · 更新于 ' + updated;
}

async function refresh() {
  if (directoryPending) return;
  const request = ++messageRequest;
  const scope = el('scope').value;
  const session = el('session').value;
  const params = new URLSearchParams({scope});
  if (session) params.set('session', session);
  ++activeMessageRequests;
  try {
    const response = await fetch('/api/messages?' + params, {headers: headers(), cache: 'no-store'});
    if (!response.ok) throw Error(response.status);
    const payload = (await response.json()).data;
    if (request !== messageRequest || scope !== el('scope').value || session !== el('session').value) return;
    records = payload;
    for (const row of records) addSession(row.session, window.panelSessionName(row));
    for (const option of el('session').options) {
      if (sessionLabels.has(option.value)) option.textContent = sessionLabels.get(option.value);
    }
    updated = new Date().toLocaleTimeString();
    draw();
  } catch (error) {
    if (request === messageRequest) {
      el('status').textContent = '读取失败（' + error.message + '），请通过桌面快捷方式重新连接。';
    }
  } finally {
    --activeMessageRequests;
  }
}

async function loadSessions() {
  const request = ++directoryRequest;
  ++messageRequest; // Discard responses from the previous directory, even when scope changes back.
  directoryPending = true;
  const scope = el('scope').value;
  const selected = requestedSession;
  const endpoint = scope === 'private' ? '/api/messages/sessions' : '/api/groups';
  let directory = [];
  try {
    const response = await fetch(endpoint + '?' + new URLSearchParams({scope}), {
      headers: headers(), cache: 'no-store',
    });
    if (!response.ok) throw Error(response.status);
    directory = (await response.json()).data.groups || [];
  } catch (error) {
    if (request === directoryRequest) el('status').textContent = '会话目录暂时不可用，正在读取消息。';
  }
  if (request !== directoryRequest) return;
  el('session').replaceChildren(new Option('全部会话', ''));
  for (const row of directory) {
    if ((row.session.startsWith('private:') ? 'private' : 'group') === scope) {
      addSession(row.session, window.panelSessionName(row));
    }
  }
  if (selected) {
    addSession(selected, sessionLabels.get(selected) || window.panelSessionName({session: selected}));
    el('session').value = selected;
  }
  directoryPending = false;
  window.dispatchEvent(new Event('panel-context-change'));
  await refresh();
}

el('scope').onchange = () => {
  requestedSession = '';
  records = [];
  el('session').replaceChildren(new Option('全部会话', ''));
  draw();
  loadSessions();
};
el('session').onchange = () => { draw(); refresh(); };
el('kind').onchange = draw;
el('query').oninput = draw;
el('refresh').onclick = refresh;
setInterval(() => {
  if (el('auto').checked && !document.hidden && !directoryPending && activeMessageRequests === 0) refresh();
}, 5000);
document.addEventListener('DOMContentLoaded', loadSessions);
