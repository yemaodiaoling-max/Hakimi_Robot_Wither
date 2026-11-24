(() => {
  'use strict';

  // A token passed as #token=... is kept for this tab only; each page removes it from the address bar.
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (fragment.get('token') && fragment.get('token').length >= 32) {
    try { sessionStorage.setItem('memoryConsoleToken', fragment.get('token')); } catch (error) { /* private mode */ }
  }

  // [path, label, module that provides the page]
  const pages = [
    ['/', '关系面板', 'core'],
    ['/messages.html', '消息日志', 'qq'],
    ['/identity.html', '旧群与成员匹配', 'qq'],
    ['/activity.html', '视频活动', 'video'],
    ['/review-history.html', '复盘变化', 'core'],
    ['/#vertexBudgetBar', '调用预算', 'core'],
    ['/drawing.html', '绘图设置', 'qq'],
  ];
  const shell = document.createElement('nav');
  shell.className = 'panel-shell';
  shell.setAttribute('aria-label', '信息面板导航');

  const brand = document.createElement('a');
  brand.className = 'shell-brand';
  brand.href = '/';
  const mark = document.createElement('span');
  mark.className = 'shell-mark';
  mark.textContent = '哈';
  const title = document.createElement('span');
  title.textContent = '哈基米面板';
  brand.append(mark, title);
  shell.append(brand);

  const here = location.pathname === '/index.html' ? '/' : location.pathname;
  for (const [path, label, module] of pages) {
    const anchor = document.createElement('a');
    anchor.className = 'shell-link';
    anchor.dataset.panelPath = path;
    anchor.dataset.module = module;
    anchor.textContent = label;
    if (here === path) anchor.setAttribute('aria-current', 'page');
    shell.append(anchor);
  }
  const spacer = document.createElement('span');
  spacer.className = 'shell-spacer';
  const setup = document.createElement('a');
  setup.className = 'shell-setup';
  setup.href = '/setup.html';
  setup.dataset.panelPath = '/setup.html';
  const dot = document.createElement('span');
  dot.className = 'shell-dot';
  setup.append(dot, document.createTextNode('设置'));
  if (here === '/setup.html') setup.setAttribute('aria-current', 'page');
  shell.append(spacer, setup);
  document.body.prepend(shell);

  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty('--panel-shell-height', shell.offsetHeight + 'px');
    }).observe(shell);
  }

  // Hide pages of modules that are not enabled; mark the setup link until a model key exists.
  const token = sessionStorage.getItem('memoryConsoleToken') || '';
  if (token) {
    fetch('/api/modules', {headers: {Authorization: 'Bearer ' + token}, cache: 'no-store'})
      .then(r => r.ok ? r.json() : null)
      .then(body => {
        if (!body || !body.data) return;
        const on = new Set(body.data.modules.filter(m => m.enabled).map(m => m.key));
        for (const a of shell.querySelectorAll('a.shell-link')) {
          if (!on.has(a.dataset.module) && a.getAttribute('aria-current') !== 'page') a.hidden = true;
        }
      }).catch(() => {});
    if (here !== '/setup.html') {
      fetch('/api/setup', {headers: {Authorization: 'Bearer ' + token}, cache: 'no-store'})
        .then(r => r.ok ? r.json() : null)
        .then(body => {
          if (body && body.data && !body.data.secrets['model.api_key']) {
            setup.classList.add('needs-setup');
            setup.title = '还没有填写模型 API Key';
          }
        }).catch(() => {});
    }
  }

  function updateLinks() {
    const params = new URLSearchParams(location.search);
    const selected = document.getElementById('groupFilter') ||
      document.getElementById('session') || document.getElementById('historyGroup');
    const group = selected ? selected.value :
      params.get('group') || params.get('session') || params.get('identity_group') || '';
    const person = params.get('person') || '';
    for (const anchor of shell.querySelectorAll('a.shell-link')) {
      const target = new URL(anchor.dataset.panelPath, location.href);
      if (target.pathname === '/drawing.html') {
        anchor.href = target.pathname;
        continue;
      }
      if (group &&(!group.startsWith('private:') || target.pathname === '/messages.html')) {
        const key = target.pathname === '/messages.html' ? 'session' :
          target.pathname === '/identity.html' ? 'identity_group' : 'group';
        target.searchParams.set(key, group);
      }
      if (person) target.searchParams.set('person', person);
      anchor.href = target.pathname + target.search + target.hash;
    }
  }

  function updateLocation(key, value) {
    const target = new URL(location.href);
    if (value) target.searchParams.set(key, value);
    else target.searchParams.delete(key);
    history.replaceState(null, '', target.pathname + target.search + target.hash);
    updateLinks();
  }

  document.addEventListener('change', event => {
    const keys = {scope: 'scope', session: 'session', kind: 'kind',
      groupFilter: 'group', historyGroup: 'group'};
    const key = keys[event.target.id];
    if (!key) return;
    if (key === 'scope') updateLocation('session', '');
    updateLocation(key, event.target.value);
  });
  document.addEventListener('input', event => {
    if (event.target.id === 'query') updateLocation('q', event.target.value);
  });
  window.addEventListener('panel-context-change', updateLinks);
  window.addEventListener('pageshow', updateLinks);
  window.addEventListener('popstate', updateLinks);
  const observed = document.getElementById('app') || document.querySelector('main');
  if (observed) new MutationObserver(updateLinks).observe(observed, {childList: true, subtree: true});
  updateLinks();

  window.panelSessionName = row => {
    const session = String(row.session || '');
    const id = session.split(':').slice(1).join(':');
    for (const value of [row.label, row.group_name, row.name, row.session_name, row.title]) {
      if (value && value !== session && !(id && String(value).includes(id))) return String(value);
    }
    return session.startsWith('private:') ? '私聊会话' : '未命名群聊';
  };
})();
