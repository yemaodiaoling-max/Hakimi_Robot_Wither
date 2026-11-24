(() => {
  'use strict';
  const TOKEN_KEY = 'memoryConsoleToken';
  const $ = id => document.getElementById(id);
  const form = $('setupForm');
  const state = {data: null, token: '', qrTimer: null};

  function takeToken() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const fromHash = fragment.get('token');
    if (fromHash && fromHash.length >= 32) {
      try { sessionStorage.setItem(TOKEN_KEY, fromHash); } catch (error) { /* private mode */ }
      history.replaceState(null, '', location.pathname + location.search);
    }
    try { state.token = sessionStorage.getItem(TOKEN_KEY) || ''; } catch (error) { state.token = fromHash || ''; }
  }

  async function api(path, body) {
    const options = {headers: {Authorization: 'Bearer ' + state.token, Accept: 'application/json'}, cache: 'no-store'};
    if (body !== undefined) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let payload = null;
    try { payload = await response.json(); } catch (error) { /* empty */ }
    if (response.status === 401) { showGate('令牌无效或已更换，请重新获取。'); throw new Error('unauthorised'); }
    if (!response.ok || !payload || payload.ok === false) {
      const message = payload && payload.error ? payload.error.message : '请求失败（HTTP ' + response.status + '）';
      const error = new Error(message);
      error.api = true;
      throw error;
    }
    return payload.data;
  }

  function showGate(message) {
    $('tokenGate').hidden = false;
    $('setupBody').hidden = true;
    $('saveState').textContent = message || '需要令牌';
    $('saveState').className = 'badge warn';
  }

  // ------------------------------------------------------------ form <-> settings
  const get = (obj, dotted) => dotted.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
  function set(obj, dotted, value) {
    const keys = dotted.split('.');
    let o = obj;
    for (const k of keys.slice(0, -1)) o = o[k] = o[k] && typeof o[k] === 'object' ? o[k] : {};
    o[keys[keys.length - 1]] = value;
  }
  const LISTS = new Set(['owner.ids', 'owner.aliases', 'owner.known_bot_ids']);

  function fill(settings) {
    for (const input of form.querySelectorAll('[name]')) {
      const name = input.name;
      if (input.type === 'password') { input.value = ''; continue; }
      let value = get(settings, name);
      if (name === 'model.routing') { input.checked = value === 'dual'; continue; }
      if (input.type === 'checkbox') { input.checked = !!value; continue; }
      if (LISTS.has(name) && Array.isArray(value)) value = value.join(', ');
      input.value = value == null ? '' : String(value);
    }
  }

  function collect() {
    const settings = JSON.parse(JSON.stringify(state.data.settings));
    for (const input of form.querySelectorAll('[name]')) {
      const name = input.name;
      if (name === 'model.routing') { set(settings, name, input.checked ? 'dual' : 'single'); continue; }
      if (input.type === 'checkbox') { set(settings, name, input.checked); continue; }
      if (LISTS.has(name)) { set(settings, name, input.value.split(/[,，\s]+/).filter(Boolean)); continue; }
      set(settings, name, input.value.trim());
    }
    settings.modules = [...document.querySelectorAll('.module-card input:checked')].map(i => i.value);
    const prices = {};
    for (const row of document.querySelectorAll('.price-row')) {
      const [inp, out] = row.querySelectorAll('input');
      if (inp.value || out.value) prices[row.dataset.model] = {input_usd_per_million: inp.value.trim(), output_usd_per_million: out.value.trim()};
    }
    settings.model.prices = prices;
    return settings;
  }

  // ------------------------------------------------------------ modules
  function renderModules(modules, enabled) {
    const grid = $('moduleGrid');
    grid.replaceChildren();
    for (const m of modules) {
      const card = document.createElement('label');
      card.className = 'module-card';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = m.key;
      input.checked = m.key === 'core' || enabled.includes(m.key);
      input.disabled = m.key === 'core' || !m.installed;
      const head = document.createElement('div');
      head.className = 'module-head';
      const name = document.createElement('strong');
      name.textContent = m.name;
      const sw = document.createElement('span');
      sw.className = 'switch';
      head.append(name, sw);
      const text = document.createElement('p');
      text.textContent = m.summary;
      const tag = document.createElement('span');
      tag.className = 'badge ' + (m.key === 'core' ? 'accent' : m.installed ? 'plain' : 'warn');
      tag.textContent = m.key === 'core' ? '始终启用' : m.installed ? '已安装' : '未安装（见 README）';
      card.append(input, head, text, tag);
      if (input.disabled && m.key !== 'core') card.classList.add('disabled');
      input.addEventListener('change', syncModules);
      grid.append(card);
    }
    syncModules();
  }

  function syncModules() {
    const on = new Set([...document.querySelectorAll('.module-card input:checked')].map(i => i.value));
    for (const card of document.querySelectorAll('.module-card')) card.classList.toggle('on', card.querySelector('input').checked);
    for (const el of document.querySelectorAll('[data-module]')) el.classList.toggle('off', !on.has(el.dataset.module));
    if (typeof persona !== 'undefined' && persona.entries.length) renderPersonaList();
  }

  // ------------------------------------------------------------ secrets, prices, pet preview
  function renderSecrets(secrets) {
    for (const flag of document.querySelectorAll('.secret-flag')) {
      const isSet = !!secrets[flag.dataset.secret];
      flag.className = 'secret-flag ' + (isSet ? 'set' : 'unset');
      const input = form.querySelector('[name="' + flag.dataset.secret + '"]');
      if (input && input.type === 'password') input.placeholder = isSet ? '已保存，留空则不修改' : '';
    }
  }

  function renderPrices() {
    const names = new Set(['model.main_model', 'model.light_model', 'model.strong_model', 'model.video_model']
      .map(n => form.querySelector('[name="' + n + '"]').value.trim()).filter(Boolean));
    const box = $('priceRows');
    const current = {};
    for (const row of box.querySelectorAll('.price-row')) {
      const [inp, out] = row.querySelectorAll('input');
      current[row.dataset.model] = {input_usd_per_million: inp.value, output_usd_per_million: out.value};
    }
    const saved = Object.assign({}, state.data.settings.model.prices || {}, current);
    box.replaceChildren();
    for (const name of names) {
      const row = document.createElement('div');
      row.className = 'price-row';
      row.dataset.model = name;
      const label = document.createElement('span');
      label.className = 'mono';
      label.textContent = name;
      label.title = name;
      const inp = document.createElement('input');
      inp.placeholder = '输入 $/百万';
      inp.inputMode = 'decimal';
      inp.setAttribute('aria-label', name + ' 输入单价');
      inp.value = (saved[name] || {}).input_usd_per_million || '';
      const out = document.createElement('input');
      out.placeholder = '输出 $/百万';
      out.inputMode = 'decimal';
      out.setAttribute('aria-label', name + ' 输出单价');
      out.value = (saved[name] || {}).output_usd_per_million || '';
      row.append(label, inp, out);
      box.append(row);
    }
  }

  function renderPetPreview() {
    const v = n => form.querySelector('[name="pet.' + n + '"]').value.trim();
    const ssh = v('connection') === 'ssh';
    for (const el of document.querySelectorAll('.ssh-only')) el.hidden = !ssh;
    const preview = ssh ? {
      mode: 'ssh', host: v('ssh_host'), port: Number(v('ssh_port') || 22), user: v('ssh_user'),
      identity: v('ssh_identity'), known_hosts: v('ssh_known_hosts'),
      remote_command: 'HAKIMI_ROOT=' + state.data.root + ' ' + v('remote_python') + ' -u ' + v('remote_app') + '/desktop_pet_agent.py',
    } : {mode: 'local'};
    $('petConnection').textContent = JSON.stringify(preview, null, 2);
  }

  // ------------------------------------------------------------ persona editor
  const GROUPS = {qq: 'QQ 机器人', pet: '桌宠', video: '视频机器人'};
  const persona = {current: null, saved: '', entries: []};

  function personaDirty() {
    return persona.current && $('personaText').value !== persona.saved;
  }

  async function loadPersonaList() {
    const data = await api('/api/setup/persona', {action: 'list'});
    persona.entries = data.entries;
    renderPersonaList();
  }

  function renderPersonaList() {
    const box = $('personaList');
    const on = new Set([...document.querySelectorAll('.module-card input:checked')].map(i => i.value));
    box.replaceChildren();
    let group = '';
    for (const entry of persona.entries.filter(e => e.available)) {
      if (entry.module !== group) {
        group = entry.module;
        box.append(Object.assign(document.createElement('div'), {className: 'persona-group', textContent: GROUPS[group] || group}));
      }
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'persona-item' + (entry.custom ? ' custom' : '') + (on.has(entry.module) ? '' : ' off');
      item.setAttribute('role', 'tab');
      item.setAttribute('aria-selected', String(persona.current === entry.file));
      const shortLabel = entry.label.replace(/^(QQ|桌宠|视频)\s*·\s*/, '');
      item.setAttribute('aria-label', shortLabel + (entry.custom ? '（已自定义）' : ''));
      item.append(document.createTextNode(shortLabel));
      item.append(Object.assign(document.createElement('span'), {className: 'dot'}));
      item.addEventListener('click', () => openPersona(entry.file));
      box.append(item);
    }
  }

  async function openPersona(file) {
    if (personaDirty() && !confirm('当前人设有未保存的修改，确定放弃吗？')) return;
    const data = await api('/api/setup/persona', {action: 'read', file});
    persona.current = file;
    persona.saved = data.text;
    $('personaLabel').textContent = data.label;
    $('personaSummary').textContent = data.summary + (data.restart.length ? ' 生效需重启：' + data.restart.join(' ') : '');
    $('personaText').value = data.text;
    $('personaText').disabled = false;
    $('personaSave').disabled = false;
    $('personaReset').disabled = !data.custom;
    $('personaState').hidden = false;
    $('personaState').textContent = data.custom ? '已自定义' : '默认';
    $('personaState').className = 'badge ' + (data.custom ? 'accent' : 'plain');
    $('personaResult').textContent = '';
    renderPersonaList();
  }

  async function savePersona(reset) {
    if (!persona.current) return;
    if (reset && !confirm('恢复默认会删除你对这个人设的修改，确定吗？')) return;
    const result = $('personaResult');
    try {
      const body = reset ? {action: 'reset', file: persona.current} : {action: 'save', file: persona.current, text: $('personaText').value};
      const data = await api('/api/setup/persona', body);
      persona.saved = data.text;
      $('personaText').value = data.text;
      for (const entry of persona.entries) if (entry.file === persona.current) entry.custom = data.custom;
      $('personaReset').disabled = !data.custom;
      $('personaState').textContent = data.custom ? '已自定义' : '默认';
      $('personaState').className = 'badge ' + (data.custom ? 'accent' : 'plain');
      result.textContent = (reset ? '已恢复默认' : '已保存') + (data.restart.length ? '，重启 ' + data.restart.join(' ') + ' 后生效' : '');
      result.className = 'ok-text';
      renderPersonaList();
    } catch (error) {
      result.textContent = error.message;
      result.className = 'error';
    }
  }

  // ------------------------------------------------------------ actions
  async function checkModel() {
    const out = $('checkModelResult');
    out.textContent = '正在连接…';
    const field = n => form.querySelector('[name="model.' + n + '"]').value.trim();
    try {
      const data = await api('/api/setup/check', {target: 'model', model: {
        provider: field('provider'), base_url: field('base_url'), auth: field('auth'), proxy: field('proxy'), api_key: field('api_key')}});
      const list = $('modelNames');
      list.replaceChildren(...data.models.map(name => Object.assign(document.createElement('option'), {value: name})));
      out.textContent = '连接成功，可用模型 ' + data.models.length + ' 个（输入框可直接选择）';
      out.className = 'ok-text';
    } catch (error) {
      out.textContent = error.message;
      out.className = 'error';
    }
  }

  async function qrStart() {
    clearTimeout(state.qrTimer);
    const box = $('qrBox');
    $('qrState').textContent = '正在获取二维码…';
    try {
      const data = await api('/api/setup/bilibili-login', {action: 'start'});
      box.replaceChildren();
      if (data.image) {
        const img = document.createElement('img');
        img.alt = 'B站登录二维码';
        img.src = data.image;
        box.append(img);
      } else {
        const p = document.createElement('span');
        p.className = 'muted';
        p.textContent = '服务器未安装 qrcode，请手动填写 Cookie';
        box.append(p);
      }
      $('qrState').textContent = '用 B 站 App 扫码并确认';
      poll(data.ticket, Date.now() + 180000);
    } catch (error) {
      $('qrState').textContent = error.message;
    }
  }

  function poll(ticket, until) {
    state.qrTimer = setTimeout(async () => {
      if (Date.now() > until) { $('qrState').textContent = '二维码已过期，请重新获取'; return; }
      try {
        const data = await api('/api/setup/bilibili-login', {action: 'poll', ticket});
        $('qrState').textContent = data.message;
        if (data.state === 'done') {
          $('qrBox').replaceChildren(Object.assign(document.createElement('span'), {textContent: '✓ 已登录', className: 'ok-text'}));
          await refresh(false);
          return;
        }
        if (data.state === 'waiting' || data.state === 'scanned') poll(ticket, until);
      } catch (error) {
        $('qrState').textContent = error.message;
      }
    }, 2000);
  }

  async function save(event) {
    event.preventDefault();
    const button = $('saveButton');
    const error = $('formError');
    error.textContent = '';
    for (const el of form.querySelectorAll('[aria-invalid]')) el.removeAttribute('aria-invalid');
    button.disabled = true;
    button.textContent = '正在应用…';
    try {
      const result = await api('/api/setup', {settings: collect(), restart: $('restart').checked});
      const report = $('report');
      report.replaceChildren(...result.report.map(row => {
        const li = document.createElement('li');
        if (!row.ok) li.className = 'fail';
        li.append(document.createTextNode(row.step + ' '));
        const detail = document.createElement('span');
        detail.textContent = row.detail;
        li.append(detail);
        return li;
      }));
      const units = result.restart_units.join(' ');
      $('restartNote').textContent = result.restarted ? '已请求重启：' + units
        : '设置已保存。让新设置生效：systemctl --user restart ' + units;
      $('sec-result').hidden = false;
      $('sec-result').scrollIntoView({behavior: 'smooth', block: 'start'});
      await refresh(false);
      $('saveState').textContent = result.report.every(r => r.ok) ? '已保存' : '部分步骤失败';
      $('saveState').className = 'badge ' + (result.report.every(r => r.ok) ? 'ok' : 'warn');
    } catch (err) {
      error.textContent = err.message;
      const field = /（([a-z_.]+)）$/.exec(err.message);
      if (field) {
        const input = form.querySelector('[name="' + field[1] + '"]');
        if (input) { input.setAttribute('aria-invalid', 'true'); input.focus(); }
      }
    } finally {
      button.disabled = false;
      button.textContent = '保存并应用';
    }
  }

  async function refresh(full = true) {
    const data = await api('/api/setup');
    state.data = data;
    if (full) {
      fill(data.settings);
      renderModules(data.modules, data.settings.modules || []);
    } else {
      for (const input of form.querySelectorAll('input[type=password]')) input.value = '';
    }
    renderSecrets(data.secrets);
    renderPrices();
    renderPetPreview();
    $('spent').textContent = '$' + Number(data.gateway.spent_usd || 0).toFixed(2) + ' · ' + data.gateway.requests + ' 次';
    $('dshState').textContent = data.tools.dsh ? '（已检测到 dsh）' : '（未检测到 dsh）';
    if ($('codexState')) $('codexState').textContent = data.tools.codex ? '（已检测到 codex）' : '（未检测到 codex）';
    $('restartToggle').hidden = !data.tools.systemctl;
    if (full) {
      $('saveState').textContent = data.secrets['model.api_key'] ? '已配置' : '尚未配置';
      $('saveState').className = 'badge ' + (data.secrets['model.api_key'] ? 'ok' : 'warn');
    }
  }

  function trackSections() {
    const links = [...document.querySelectorAll('.steps a')];
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        for (const a of links) a.classList.toggle('active', a.getAttribute('href') === '#' + entry.target.id);
      }
    }, {rootMargin: '-30% 0px -60% 0px'});
    for (const a of links) {
      const target = document.querySelector(a.getAttribute('href'));
      if (target) observer.observe(target);
    }
  }

  // ------------------------------------------------------------ start
  takeToken();
  $('tokenForm').addEventListener('submit', event => {
    event.preventDefault();
    const value = $('tokenInput').value.trim();
    if (value.length < 32) return;
    try { sessionStorage.setItem(TOKEN_KEY, value); } catch (error) { /* private mode */ }
    location.reload();
  });
  if (!state.token) { showGate('需要令牌'); return; }
  form.addEventListener('submit', save);
  $('checkModel').addEventListener('click', checkModel);
  $('qrStart').addEventListener('click', qrStart);
  $('copyPet').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('petConnection').textContent); $('copyState').textContent = '已复制'; }
    catch (error) { $('copyState').textContent = '请手动选中复制'; }
  });
  form.addEventListener('input', event => {
    if (/^model\.(main|light|strong|video)_model$/.test(event.target.name)) renderPrices();
    if (event.target.name && event.target.name.startsWith('pet.')) renderPetPreview();
  });
  form.addEventListener('change', event => {
    if (event.target.name === 'model.provider') {
      if (event.target.value === 'gemini') {
        form.querySelector('[name="model.base_url"]').value = 'https://generativelanguage.googleapis.com';
        form.querySelector('[name="model.auth"]').value = 'x-goog-api-key';
      } else if (event.target.value === 'openai_compatible') {
        form.querySelector('[name="model.auth"]').value = 'bearer';
        const u = form.querySelector('[name="model.base_url"]');
        if (u.value.trim() === 'https://generativelanguage.googleapis.com') u.value = 'https://api.openai.com';
      }
    }
    if (event.target.name && event.target.name.startsWith('pet.')) renderPetPreview();
  });
  $('personaSave').addEventListener('click', () => savePersona(false));
  $('personaReset').addEventListener('click', () => savePersona(true));
  window.addEventListener('beforeunload', event => { if (personaDirty()) event.preventDefault(); });
  refresh().then(async () => {
    $('setupBody').hidden = false;
    trackSections();
    await loadPersonaList();
  }).catch(error => {
    if (error.message !== 'unauthorised') {
      $('saveState').textContent = '读取失败：' + error.message;
      $('saveState').className = 'badge danger';
    }
  });
})();
