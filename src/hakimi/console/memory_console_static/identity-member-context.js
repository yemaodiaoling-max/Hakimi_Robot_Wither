'use strict';

(function () {
  const MEMBER_LIST_ID = 'newMemberList';
  const GROUP_LIST_ID = 'newGroupList';
  const MOUNT_ID = 'newMemberContext';
  const TITLE = '最近的群内发言 · 帮你核对身份';
  const PROMPT = '选择右侧新成员后，这里显示他的近期发言';
  const HINT = '仅供你核对身份，不会据此自动匹配';
  const TOKEN_KEY = 'memoryConsoleToken';

  const state = {
    group: '',
    user: '',
    lastKey: '',
    seq: 0,
    controller: null,
    memberReady: true,
  };

  let memberList = null;
  let groupList = null;
  let mount = null;

  function selectedId(list) {
    if (!list) return '';
    if (list.id === GROUP_LIST_ID) {
      const selected = list.querySelector('.pick-item.selected[data-group-id]');
      return selected ? selected.dataset.groupId : '';
    }
    const code = list.querySelector('.pick-item.selected .id-code');
    if (!code) return '';
    const full = code.getAttribute('title');
    return full ? String(full).trim() : '';
  }

  function readToken() {
    try {
      return sessionStorage.getItem(TOKEN_KEY) || '';
    } catch (error) {
      return '';
    }
  }

  function formatAt(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '时间未知';
    const millis = number > 1000000000000 ? number : number * 1000;
    const date = new Date(millis);
    if (Number.isNaN(date.getTime())) return '时间未知';
    return date.toLocaleString('zh-CN', { hour12: false });
  }

  function heading() {
    const node = document.createElement('div');
    node.className = 'pick-title';
    node.textContent = TITLE;
    return node;
  }

  function messageLine(text) {
    const node = document.createElement('p');
    node.className = 'muted';
    node.textContent = text;
    return node;
  }

  function paintMessage(text) {
    if (!mount) return;
    mount.replaceChildren(heading(), messageLine(text));
  }

  function concise(value) {
    if (typeof value !== 'string') return '';
    const text = value.trim();
    return text.length > 120 ? text.slice(0, 120) : text;
  }

  function paintContext(data) {
    if (!mount) return;
    const nodes = [heading()];
    const messages = Array.isArray(data.messages) ? data.messages : [];
    if (!messages.length) {
      nodes.push(messageLine('暂时没有可核对的发言'));
    } else {
      const list = document.createElement('div');
      list.className = 'note-preview-list';
      for (const item of messages) {
        const row = document.createElement('div');
        row.className = 'note-preview';
        const meta = document.createElement('div');
        meta.className = 'note-meta';
        const label = document.createElement('span');
        label.className = 'tag';
        label.textContent = item && item.label === '旧摘要' ? '旧摘要' : '群内原话';
        const time = document.createElement('span');
        time.className = 'muted';
        time.textContent = formatAt(item && item.at);
        meta.append(label, time);
        const excerpt = document.createElement('p');
        excerpt.textContent = String((item && item.excerpt) || '');
        row.append(meta, excerpt);
        list.append(row);
      }
      nodes.push(list);
    }
    const hint = document.createElement('p');
    hint.className = 'muted';
    hint.textContent = String(data.hint || HINT);
    nodes.push(hint);
    mount.replaceChildren(...nodes);
  }

  function cancelRequest() {
    state.seq += 1;
    if (state.controller) {
      try {
        state.controller.abort();
      } catch (error) {
        /* already settled */
      }
      state.controller = null;
    }
  }

  function requestContext(group, user, key) {
    cancelRequest();
    const token = state.seq;
    state.lastKey = key;
    paintMessage('正在读取最近发言…');

    const bearer = readToken();
    if (!bearer) {
      paintMessage('登录状态已失效，请刷新后重试');
      return;
    }

    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    state.controller = controller;
    const params = new URLSearchParams();
    params.set('group', group);
    params.set('user', user);
    const options = {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: {
        Authorization: 'Bearer ' + bearer,
        Accept: 'application/json',
      },
    };
    if (controller) options.signal = controller.signal;

    fetch('/api/identity/member-context?' + params.toString(), options)
      .then((response) => response.json()
        .catch(() => null)
        .then((body) => ({ response: response, body: body })))
      .then((result) => {
        if (token !== state.seq) return;
        state.controller = null;
        if (!result.response.ok) {
          if (result.response.status === 401) {
            paintMessage('登录状态已失效，请刷新后重试');
            return;
          }
          const detail = result.body && result.body.error && result.body.error.message;
          paintMessage(concise(detail) || '暂时无法读取最近发言');
          return;
        }
        const data = result.body && result.body.ok === true && result.body.data
          ? result.body.data : result.body;
        if (!data || !Array.isArray(data.messages)) {
          paintMessage('暂时无法读取最近发言');
          return;
        }
        paintContext(data);
      })
      .catch(() => {
        if (token !== state.seq) return;
        state.controller = null;
        paintMessage('暂时无法连接本地服务');
      });
  }

  function sync() {
    const group = state.group;
    const user = state.user;
    if (!group || !user) {
      cancelRequest();
      state.lastKey = '';
      paintMessage(PROMPT);
      return;
    }
    const key = group + '\n' + user;
    if (key === state.lastKey) return;
    requestContext(group, user, key);
  }

  function onMutations(records) {
    if (!memberList || !groupList) return;
    let memberTouched = false;
    for (const record of records) {
      if (record.target === memberList || memberList.contains(record.target)) {
        memberTouched = true;
        break;
      }
    }

    const group = selectedId(groupList);
    if (group !== state.group) {
      state.group = group;
      state.user = '';
      state.memberReady = false;
      cancelRequest();
      state.lastKey = '';
      paintMessage(PROMPT);
      if (!group) return;
      if (!memberTouched) return;
      state.memberReady = true;
    } else if (memberTouched) {
      state.memberReady = true;
    } else {
      return;
    }

    state.user = state.memberReady ? selectedId(memberList) : '';
    sync();
  }

  function boot() {
    memberList = document.getElementById(MEMBER_LIST_ID);
    groupList = document.getElementById(GROUP_LIST_ID);
    if (!memberList || !groupList) return;

    mount = document.getElementById(MOUNT_ID);
    if (!mount) {
      mount = document.createElement('div');
      mount.id = MOUNT_ID;
      mount.className = 'manual-box';
      memberList.insertAdjacentElement('afterend', mount);
    }

    state.group = selectedId(groupList);
    state.user = selectedId(memberList);
    state.memberReady = true;
    paintMessage(PROMPT);
    if (state.group && state.user) sync();

    const observer = new MutationObserver(onMutations);
    observer.observe(memberList, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class', 'title'],
    });
    observer.observe(groupList, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class', 'title'],
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
