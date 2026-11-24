'use strict';

(function () {
  const API_ROOT = '';
  const LS_TOKEN = 'memoryConsoleToken';
  const WORLD = { width: 1000, height: 760, pad: 58 };
  const METRIC_LABELS = { unified: '最新综合判断', manual: '亲笔评分', assisted: '辅助推测', terra: 'Terra 观察', interaction: '互动熟悉度' };
  const MEASURE_LABELS = { affinity: '好感', closeness: '亲近程度', interaction: '互动熟悉度' };
  const SCORE_RANGES = [
    { max: 19, variable: '--score-0-19', label: '0–19 红' },
    { max: 39, variable: '--score-20-39', label: '20–39 橙' },
    { max: 59, variable: '--score-40-59', label: '40–59 灰蓝' },
    { max: 79, variable: '--score-60-79', label: '60–79 青绿' },
    { max: 100, variable: '--score-80-100', label: '80–100 绿' }
  ];
  const KIND_LABELS = {
    chat: '聊天原话', person: '人物', preference: '偏好', lore: '考据',
    episode: '共同经历', reflection: '主观反思', commitment: '约定', dialogue: '对话'
  };
  const SESSION_LABELS = { group: '群', private: '私聊' };

  const state = {
    token: '',
    view: 'graph',
    status: null,
    version: null,
    groups: [],
    group: new URLSearchParams(location.search).get('group') || '',
    people: { page: 1, pages: 1, total: 0, items: [] },
    peopleQuery: '',
    graph: { nodes: [], edges: [], counts: {}, limit: 35, metric: 'assisted' },
    limit: 35,
    spacing: 110,
    threshold: 0,
    measure: 'affinity',
    source: 'unified',
    labels: true,
    zoom: 1,
    panX: 0,
    panY: 0,
    viewportEl: null,
    positions: new Map(),
    userMoved: new Set(),
    focus: null,
    selectedPerson: null,
    selectedEdge: null,
    selectedRelation: null,
    personData: null,
    library: { page: 1, pages: 1, total: 0, items: [], q: '', kind: '', person: '' },
    memory: null,
    polling: null,
    refreshing: false,
    graphRequest: 0,
    libraryRequest: 0,
    personRequest: 0,
    peopleRequest: 0,
    memoryRequest: 0,
    offline: false,
    lastStatusAt: 0,
    drag: null,
    panDrag: null,
  };

  const $ = (id) => document.getElementById(id);
  const dom = {
    statusDot: $('statusDot'), statusText: $('statusText'), syncCount: $('syncCount'),
    banner: $('banner'), peopleTotal: $('peopleTotal'), peopleSearch: $('peopleSearch'),
    groupFilter: $('groupFilter'), peopleList: $('peopleList'), peoplePrev: $('peoplePrev'),
    peopleNext: $('peopleNext'), peoplePageLabel: $('peoplePageLabel'),
    graphView: $('graphView'), libraryView: $('libraryView'), graphSvg: $('graphSvg'),
    canvasEmpty: $('canvasEmpty'), metricLegend: $('metricLegend'), graphSummary: $('graphSummary'),
    graphHint: $('graphHint'), nodeLimit: $('nodeLimit'), nodeLimitValue: $('nodeLimitValue'),
    spacing: $('spacing'), spacingValue: $('spacingValue'), threshold: $('threshold'),
    thresholdValue: $('thresholdValue'), measure: $('measure'), source: $('source'),
    labelToggle: $('labelToggle'),
    memorySearch: $('memorySearch'), memoryKind: $('memoryKind'), clearPersonFilter: $('clearPersonFilter'),
    librarySummary: $('librarySummary'), memoryList: $('memoryList'), memoryPrev: $('memoryPrev'),
    memoryNext: $('memoryNext'), memoryPageLabel: $('memoryPageLabel'),
    detailEmpty: $('detailEmpty'), detailContent: $('detailContent'), detailPane: $('detailPane'),
    tokenGate: $('tokenGate'), tokenForm: $('tokenForm'), tokenInput: $('tokenInput'),
    tokenError: $('tokenError'), memoryModal: $('memoryModal'), memoryBody: $('memoryBody'),
    memoryTitle: $('memoryTitle'), memoryClose: $('memoryClose'), toast: $('toast'),
    zoomIn: $('zoomIn'), zoomOut: $('zoomOut'), zoomReset: $('zoomReset'),
    identityEntry: $('identityEntry'),
  };

  class AuthError extends Error {}

  function make(tag, options, children) {
    const element = document.createElement(tag);
    const opts = options || {};
    if (opts.className) element.className = opts.className;
    if (opts.text !== undefined && opts.text !== null) element.textContent = String(opts.text);
    if (opts.title) element.title = String(opts.title);
    if (opts.type) element.type = opts.type;
    if (opts.value !== undefined) element.value = opts.value;
    if (opts.disabled) element.disabled = true;
    if (opts.hidden) element.hidden = true;
    if (opts.attrs) {
      for (const [key, value] of Object.entries(opts.attrs)) {
        if (value !== undefined && value !== null) element.setAttribute(key, String(value));
      }
    }
    if (opts.onClick) element.addEventListener('click', opts.onClick);
    if (children) {
      for (const child of Array.isArray(children) ? children : [children]) {
        if (child === null || child === undefined || child === false) continue;
        element.append(child instanceof Node ? child : document.createTextNode(String(child)));
      }
    }
    return element;
  }

  function svgNode(tag, attrs, children) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value !== undefined && value !== null) element.setAttribute(key, String(value));
    }
    for (const child of children || []) {
      if (child) element.append(child);
    }
    return element;
  }

  function append(parent, ...children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return parent;
  }

  function clear(mount) {
    mount.replaceChildren();
    return mount;
  }

  function formatTime(value, withDate) {
    if (!value) return '—';
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    try {
      return new Intl.DateTimeFormat('zh-CN', {
        ...(withDate === false ? {} : { year: 'numeric', month: '2-digit', day: '2-digit' }),
        hour: '2-digit', minute: '2-digit'
      }).format(date);
    } catch (error) {
      return date.toLocaleString();
    }
  }

  function relativeTime(value) {
    if (!value) return '';
    const seconds = Math.max(0, Date.now() / 1000 - Number(value));
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时前';
    if (seconds < 86400 * 30) return Math.floor(seconds / 86400) + ' 天前';
    return formatTime(value, true);
  }

  function sessionLabel(session) {
    if (!session) return '—';
    const group = state.groups.find(row => row.session === session);
    return window.panelSessionName(group || {session});
  }

  function scoreText(value) {
    return value === null || value === undefined ? '待了解' : String(value) + ' / 100';
  }

  function numericScore(value) {
    if (value === null || value === undefined || value === '') return null;
    const score = Number(value);
    if (!Number.isFinite(score)) return null;
    return Math.max(0, Math.min(100, score));
  }

  function edgeMeasureScore(edge, measure, source) {
    if (!edge) return null;
    if (measure === 'interaction') {
      const group = edge.measures && edge.measures.interaction;
      const value = group && typeof group === 'object' && group.value !== undefined
        ? group.value : edge.scores && edge.scores.interaction;
      return numericScore(value);
    }
    const provenance = { manual: 'gemini_manual', terra: 'terra_assisted', assisted: 'dsh_assisted' }[source];
    const latest = new Map();
    for (const item of edge.directions || []) {
      if ((source !== 'unified' && item.provenance !== provenance) || !item.direction) continue;
      const old = latest.get(item.direction);
      if (!old || Number(item.created_at) > Number(old.created_at) ||
          (Number(item.created_at) === Number(old.created_at) && Number(item.assessment_id) > Number(old.assessment_id))) latest.set(item.direction, item);
    }
    if (state.focus && (edge.source === state.focus || edge.target === state.focus)) {
      const subject = state.focus.replace(/^(person|assistant):/, '');
      const other = (edge.source === state.focus ? edge.target : edge.source).replace(/^(person|assistant):/, '');
      const item = latest.get(subject + '->' + other);
      return item && item.scores_available ? numericScore(item[measure]) : null;
    }
    if (latest.size && ![...latest.values()].some(item => item.scores_available && numericScore(item[measure]) !== null)) return null;
    const values = [...latest.values()].filter(item => item.scores_available)
      .map(item => numericScore(item[measure])).filter(value => value !== null);
    if (values.length) return values.reduce((a, b) => a + b, 0) / values.length;
    const group = edge.measures && edge.measures[measure];
    if (group && typeof group === 'object' && Object.prototype.hasOwnProperty.call(group, source)) {
      return numericScore(group[source]);
    }
    if (measure === 'affinity' && edge.scores && Object.prototype.hasOwnProperty.call(edge.scores, source)) {
      return numericScore(edge.scores[source]);
    }
    if (measure === 'closeness' && edge.closeness && Object.prototype.hasOwnProperty.call(edge.closeness, source)) {
      return numericScore(edge.closeness[source]);
    }
    // Only trust the server's resolved metric when it matches the current
    // controls; switching source in the browser must not reuse another source.
    if (edge.metric_source === source && edge.measure === measure) {
      return numericScore(edge.metric_score);
    }
    return null;
  }

  function metricScore(edge) {
    if (!edge) return null;
    if (state.focus && edge.source !== state.focus && edge.target !== state.focus) return null;
    if (state.measure === 'interaction') return edgeMeasureScore(edge, 'interaction', 'interaction');
    return edgeMeasureScore(edge, state.measure, state.source);
  }

  function scoreColorVariable(score) {
    const value = numericScore(score);
    if (value === null) return '--edge-unknown';
    const range = SCORE_RANGES.find((item) => value <= item.max);
    return range ? range.variable : '--score-80-100';
  }

  function scoreSourceLabel() {
    return state.measure === 'interaction' ? '互动熟悉度' : (METRIC_LABELS[state.source] || state.source);
  }

  function edgeDirections(edge) {
    const seen = new Set();
    for (const item of edge && edge.directions || []) {
      if (item && item.direction) seen.add(String(item.direction).replace(/\s*->\s*/g, '→'));
    }
    for (const item of edge && edge.direction || []) {
      if (item) seen.add(String(item).replace(/\s*->\s*/g, '→'));
    }
    return Array.from(seen);
  }

  function directionSummary(edge) {
    const directions = edgeDirections(edge);
    if (!directions.length) return '方向未标注';
    if (directions.length === 1) return directions[0];
    return '双向：' + directions.join(' / ');
  }

  function focusedEdgeLabel(edge, focus, fallback) {
    if (state.measure === 'interaction') return String(fallback);
    return '→ ' + (fallback === null ? '未知' : fallback);
  }

  function shortId(value) {
    const text = String(value || '');
    return text.length > 6 ? '…' + text.slice(-6) : text;
  }

  function displayName(label, id) {
    const raw = String(label || '');
    const official = String(id || '');
    if (raw && raw !== '用户' + official && !/^\d{6,}$/.test(raw)) {
      return raw.length > 13 ? raw.slice(0, 12) + '…' : raw;
    }
    return shortId(official || raw.replace(/^用户/, ''));
  }

  function scoreBox(label, value, provenance, withProgress) {
    const box = make('div', { className: 'score-box' });
    append(box, make('div', { className: 'score-label', text: label }));
    append(box, make('div', {
      className: 'score-value' + (value === null || value === undefined ? ' unknown' : ''),
      text: scoreText(value)
    }));
    if (withProgress !== false && value !== null && value !== undefined) {
      const meter = make('progress', {
        attrs: { max: '100', value: String(Math.max(0, Math.min(100, Number(value)))) }
      });
      if (provenance === 'manual') meter.className = 'manual';
      if (provenance === 'terra') meter.className = 'terra';
      if (provenance === 'interaction') meter.className = 'interaction';
      append(box, meter);
    }
    return box;
  }

  function statBox(label, value) {
    const box = make('div', { className: 'score-box' });
    append(box, make('div', { className: 'score-label', text: label }));
    append(box, make('div', {
      className: 'score-value' + (value === null || value === undefined ? ' unknown' : ''),
      text: value === null || value === undefined ? '待了解' : String(value)
    }));
    return box;
  }

  function confidenceRow(value, provenance) {
    return scoreBox('置信度', value, provenance);
  }

  function tag(text, kind) {
    return make('span', { className: 'tag ' + (kind || 'tag-unknown'), text });
  }

  function metricTag(metric) {
    return tag(METRIC_LABELS[metric] || metric, metric === 'manual' ? 'tag-manual'
      : metric === 'terra' ? 'tag-terra'
      : metric === 'assisted' ? 'tag-assisted' : 'tag-interaction');
  }

  function provenanceKind(provenance) {
    if (provenance === 'gemini_manual') return 'manual';
    if (provenance === 'terra_assisted') return 'terra';
    return 'assisted';
  }

  function provenanceLabel(provenance) {
    if (provenance === 'gemini_manual') return 'Gemini 亲笔';
    if (provenance === 'terra_assisted') return 'Terra 观察';
    return 'DSH 辅助整理';
  }

  function assessmentDate(item) {
    const when = formatTime(item.created_at, true);
    if (item.stale) return when + ' · 证据缺失/已被取代';
    if (item.provenance === 'terra_assisted') return when + ' · 无时间过期，按证据更新';
    if (item.expires_at) return when + ' · 记录有效期 ' + formatTime(item.expires_at, true);
    return when;
  }

  function showBanner(text, kind) {
    dom.banner.hidden = false;
    dom.banner.textContent = text;
    dom.banner.className = 'banner' + (kind === 'error' ? ' error' : '');
  }

  function hideBanner() {
    dom.banner.hidden = true;
    dom.banner.textContent = '';
  }

  let toastTimer = null;
  function toast(text) {
    dom.toast.hidden = false;
    dom.toast.textContent = text;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { dom.toast.hidden = true; }, 3200);
  }

  function setOnline(online) {
    state.offline = !online;
    dom.statusDot.classList.toggle('online', online);
    dom.statusDot.classList.toggle('offline', !online);
    if (!online) dom.statusText.textContent = '连接中断，正在重试…';
  }

  async function apiGet(path, params) {
    if (!state.token) throw new AuthError('missing token');
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    const url = API_ROOT + path + (query.toString() ? '?' + query.toString() : '');
    let response;
    try {
      response = await fetch(url, {
        method: 'GET', cache: 'no-store', credentials: 'same-origin',
        headers: { Authorization: 'Bearer ' + state.token, Accept: 'application/json' }
      });
    } catch (error) {
      setOnline(false);
      showBanner('无法连接本地服务，正在每5秒重试。');
      throw error;
    }
    if (response.status === 401) {
      loseToken();
      throw new AuthError('unauthorised');
    }
    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!response.ok || !payload || payload.ok !== true) {
      setOnline(true);
      const message = payload && payload.error && payload.error.message ? payload.error.message : '请求失败';
      throw new Error(message);
    }
    setOnline(true);
    hideBanner();
    return payload.data;
  }

  function loseToken() {
    state.token = '';
    try { sessionStorage.removeItem(LS_TOKEN); } catch (error) { /* private mode */ }
    showTokenGate('访问口令无效或已更换，请从本机入口重新打开。');
  }

  function consumeFragmentToken() {
    if (!location.hash || !location.hash.startsWith('#token=')) return;
    const raw = location.hash.slice('#token='.length);
    try {
      const token = decodeURIComponent(raw);
      if (token.length >= 32) {
        state.token = token;
        try { sessionStorage.setItem(LS_TOKEN, token); } catch (error) { /* private mode */ }
      }
    } catch (error) { /* malformed fragment */ }
    try { history.replaceState(null, '', location.pathname + location.search); } catch (error) { /* file */ }
  }

  function showTokenGate(message) {
    state.token = '';
    dom.tokenGate.hidden = false;
    dom.tokenError.hidden = !message;
    dom.tokenError.textContent = message || '';
    dom.tokenInput.value = '';
    setTimeout(() => dom.tokenInput.focus(), 30);
  }

  function hideTokenGate() {
    dom.tokenGate.hidden = true;
    dom.tokenError.hidden = true;
  }

  function updateStatus(status) {
    state.status = status;
    const counts = status.counts || {};
    dom.statusText.textContent = `实时 · ${formatTime(status.updated_at, true)} 更新`;
    dom.syncCount.textContent = `${counts.people || 0} 人 · ${counts.records || 0} 条`;
    setOnline(true);
  }

  async function bootstrap() {
    bindEvents();
    consumeFragmentToken();
    if (!state.token) {
      try { state.token = sessionStorage.getItem(LS_TOKEN) || ''; } catch (error) { state.token = ''; }
    }
    if (!state.token) {
      showTokenGate('入口通常会自动带入访问口令；如果没有，请从本机入口重新打开或粘贴访问口令。');
      return;
    }
    try {
      await loadAll();
      startPolling();
    } catch (error) {
      if (error instanceof AuthError) return;
      showBanner('初始化失败：' + error.message, 'error');
    }
  }

  async function loadAll() {
    const [status, groups, people, graph] = await Promise.all([
      apiGet('/api/status'),
      apiGet('/api/groups'),
      apiGet('/api/people', { q: state.peopleQuery, group: state.group, page: 1, page_size: 30 }),
      apiGet('/api/graph', graphParams())
    ]);
    state.version = status.version;
    updateStatus(status);
    state.groups = groups.groups || [];
    renderGroupOptions();
    applyPeople(people);
    setGraph(graph, { reset: true });
    hideTokenGate();
    hideBanner();
    const person = new URLSearchParams(location.search).get('person');
    if (person && !state.selectedPerson) await selectPerson(person);
  }

  async function startPolling() {
    clearTimeout(state.polling);
    const tick = async () => {
      try {
        const status = await apiGet('/api/status');
        updateStatus(status);
        if (!state.version) {
          state.version = status.version;
        } else if (status.version !== state.version) {
          const syncedVersion = await refreshAfterChange();
          if (syncedVersion) state.version = syncedVersion;
        }
      } catch (error) {
        if (!(error instanceof AuthError)) {
          setOnline(false);
          showBanner('实时连接中断，正在每5秒重试；画布上的旧数据不会被当成新情绪。');
        }
      } finally {
        state.polling = setTimeout(tick, 5000);
      }
    };
    state.polling = setTimeout(tick, 5000);
  }

  async function refreshAfterChange() {
    if (state.refreshing) return null;
    state.refreshing = true;
    const scrollTop = dom.detailPane.scrollTop;
    const peopleScroll = dom.peopleList.scrollTop;
    const memoryScroll = dom.memoryList.scrollTop;
    const selectedPerson = state.selectedPerson;
    const selectedRelation = state.selectedRelation;
    const group = state.group;
    const peopleRequest = ++state.peopleRequest;
    const graphRequest = ++state.graphRequest;
    try {
      const [status, groups, people, graph] = await Promise.all([
        apiGet('/api/status'),
        apiGet('/api/groups'),
        apiGet('/api/people', { q: state.peopleQuery, group: state.group, page: state.people.page, page_size: 30 }),
        apiGet('/api/graph', graphParams())
      ]);
      updateStatus(status);
      state.groups = groups.groups || [];
      renderGroupOptions();
      if (peopleRequest === state.peopleRequest && group === state.group) applyPeople(people);
      if (graphRequest === state.graphRequest) setGraph(graph, { reset: false });
      if (state.selectedPerson) {
        const id = state.selectedPerson;
        const request = ++state.personRequest;
        const person = await apiGet('/api/person', { id, group: state.group });
        if (request === state.personRequest && state.selectedPerson === id && state.group === group) {
          state.personData = person;
          renderPersonDetail(person);
        }
      } else if (state.selectedRelation) {
        const selected = state.selectedRelation;
        const relation = await apiGet('/api/relation', {
          a: state.selectedRelation.left, b: state.selectedRelation.right, group: state.group
        });
        if (state.selectedRelation === selected && state.group === group) renderRelationDetail(relation);
      }
      if (state.view === 'library') await loadLibrary(false, true);
      if (state.selectedPerson === selectedPerson && state.selectedRelation === selectedRelation && state.group === group) {
        dom.detailPane.scrollTop = scrollTop;
        dom.memoryList.scrollTop = memoryScroll;
      }
      if (peopleRequest === state.peopleRequest && group === state.group) dom.peopleList.scrollTop = peopleScroll;
      hideBanner();
      toast('新记录已同步，当前焦点和滚动位置已保留。');
      // The version only advances after every required request succeeded.
      return status.version;
    } catch (error) {
      if (!(error instanceof AuthError)) {
        showBanner('检测到新记录，但这次同步没有完成，尚未同步；画面保留上次成功的数据，下一轮会自动重试。', 'error');
      }
      return null;
    } finally {
      state.refreshing = false;
    }
  }

  function graphParams() {
    const params = {
      group: state.group, limit: state.limit, threshold: state.threshold,
      metric: state.measure === 'interaction' ? 'interaction' : state.source,
      measure: state.measure, source: state.source
    };
    if (state.focus) {
      params.focus = state.focus === 'assistant:hakimi' ? 'hakimi' : state.focus.replace('person:', '');
    }
    return params;
  }

  async function loadGraph(options) {
    const request = ++state.graphRequest;
    try {
      const data = await apiGet('/api/graph', graphParams());
      if (request !== state.graphRequest) return;
      setGraph(data, { reset: Boolean(options && options.reset) });
    } catch (error) {
      if (request !== state.graphRequest) return;
      if (state.focus && /焦点|focus/i.test(error.message)) {
        state.focus = null;
        const data = await apiGet('/api/graph', graphParams());
        setGraph(data, { reset: false });
      } else {
        throw error;
      }
    }
  }

  function applyPeople(data) {
    const page = data.page || 1;
    state.people = {
      page, pages: data.pages || 1, total: data.total || 0,
      items: data.people || []
    };
    renderPeople();
    renderPager();
    renderSelectedPeople();
  }

  function renderGroupOptions() {
    const current = state.group;
    clear(dom.groupFilter);
    append(dom.groupFilter, make('option', { value: '', text: '全部群' }));
    for (const group of state.groups) {
      const label = window.panelSessionName(group) + ' · ' + (group.chats || group.records || 0) + ' 条';
      append(dom.groupFilter, make('option', {
        value: group.session, text: label, title: window.panelSessionName(group)
      }));
    }
    dom.groupFilter.value = state.groups.some((item) => item.session === current) ? current : '';
  }

  function renderPeople() {
    clear(dom.peopleList);
    const items = state.people.items || [];
    if (!items.length) {
      append(dom.peopleList, make('div', { className: 'empty-inline', text: '没有匹配的人物；试试只搜QQ号或换一个群。' }));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const person of items) {
      const button = make('button', {
        type: 'button', className: 'person-item' + (state.selectedPerson === person.id ? ' active' : ''),
        title: (person.nickname || '') + ' · ' + person.id,
        attrs: { 'data-person-id': person.id },
        onClick: () => selectPerson(person.id)
      });
      append(button,
        make('span', { className: 'person-name', text: displayName(person.nickname, person.id) }),
        make('span', { className: 'person-time', text: relativeTime(person.last_at) }),
        make('span', { className: 'person-id', text: shortId(person.id), title: person.id }),
        make('span', { className: 'person-time', text: (person.chats || 0) + ' 句话' })
      );
      const badges = make('span', { className: 'person-badges' });
      if (person.terra_count) append(badges, make('span', { className: 'mini-badge terra', text: 'Terra ' + person.terra_count }));
      if (person.assisted_count) append(badges, make('span', { className: 'mini-badge assisted', text: '辅助 ' + person.assisted_count }));
      if (person.manual_count) append(badges, make('span', { className: 'mini-badge manual', text: '亲笔 ' + person.manual_count }));
      if (person.private_records) append(badges, make('span', { className: 'mini-badge private', text: '含私聊' }));
      if (!person.assisted_count && !person.manual_count && !person.terra_count) append(badges, make('span', { className: 'mini-badge', text: '待了解' }));
      append(button, badges);
      fragment.append(button);
    }
    dom.peopleList.append(fragment);
  }

  function renderPager() {
    dom.peopleTotal.textContent = state.people.total ? state.people.total + ' 人' : '';
    dom.peoplePageLabel.textContent = `${state.people.page} / ${state.people.pages}`;
    dom.peoplePrev.disabled = state.people.page <= 1;
    dom.peopleNext.disabled = state.people.page >= state.people.pages;
  }

  function renderSelectedPeople() {
    for (const button of dom.peopleList.querySelectorAll('.person-item')) {
      button.classList.toggle('active', button.dataset.personId === state.selectedPerson);
    }
  }

  function showDetailEmpty() {
    state.personData = null;
    updatePersonSelection(null);
    state.selectedEdge = null;
    state.selectedRelation = null;
    clear(dom.detailContent);
    dom.detailContent.hidden = true;
    dom.detailEmpty.hidden = false;
    dom.detailPane.scrollTop = 0;
  }

  async function loadPeople(page) {
    const request = ++state.peopleRequest;
    try {
      const data = await apiGet('/api/people', {
        q: state.peopleQuery, group: state.group, page: page || 1, page_size: 30
      });
      if (request !== state.peopleRequest) return;
      applyPeople(data);
      renderPager();
      renderSelectedPeople();
    } catch (error) {
      if (request !== state.peopleRequest) return;
      if (error instanceof AuthError) return;
      showBanner('人物列表加载失败：' + error.message, 'error');
    }
  }

  function updatePersonSelection(id) {
    state.selectedPerson = id;
    const url = new URL(location.href);
    if (id) url.searchParams.set('person', id);
    else url.searchParams.delete('person');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
    window.dispatchEvent(new Event('panel-context-change'));
  }

  async function selectPerson(id) {
    updatePersonSelection(id);
    state.selectedEdge = null;
    state.selectedRelation = null;
    state.focus = 'person:' + id;
    renderSelectedPeople();
    const detailPromise = loadPersonDetail(id);
    const inGraph = (state.graph.nodes || []).some(node => node.id === state.focus);
    renderGraph();
    renderGraphSummary();
    const graphPromise = inGraph ? Promise.resolve() : loadGraph();
    if (state.view === 'library') {
      state.library.person = id;
      state.library.page = 1;
      loadLibrary();
    }
    await Promise.all([
      detailPromise.catch(() => null),
      graphPromise.catch(() => null)
    ]);
  }

  async function loadPersonDetail(id) {
    const request = ++state.personRequest;
    const group = state.group;
    clear(dom.detailContent).append(
      make('div', { className: 'detail-empty' },
        [make('div', { className: 'detail-empty-mark', text: '正在读取…' }),
         make('p', { className: 'muted', text: '只读本地数据库；不会向外发送。' })])
    );
    dom.detailEmpty.hidden = true;
    dom.detailContent.hidden = false;
    try {
      const data = await apiGet('/api/person', { id, group });
      if (request !== state.personRequest || state.selectedPerson !== id || state.group !== group) return;
      state.personData = data;
      renderPersonDetail(data);
    } catch (error) {
      if (request !== state.personRequest || state.selectedPerson !== id || state.group !== group) return;
      if (error instanceof AuthError) return;
      clear(dom.detailContent).append(make('div', { className: 'detail-empty' },
        [make('div', { className: 'detail-empty-mark', text: '读取失败' }),
         make('p', { className: 'muted', text: error.message })]));
    }
  }

  function section(title, hint) {
    const wrapper = make('section', { className: 'section' });
    const head = make('div', { className: 'section-head' });
    append(head, make('h3', { text: title }));
    if (hint) append(head, make('span', { className: 'section-hint', text: hint }));
    append(wrapper, head);
    return wrapper;
  }

  function memoryButton(memoryId, label) {
    return make('button', {
      type: 'button', className: 'memory-link', text: label || memoryId,
      title: '查看原文 ' + memoryId,
      onClick: (event) => { event.stopPropagation(); openMemory(memoryId); }
    });
  }

  function evidenceRow(evidence) {
    const row = make('div', { className: 'evidence-row' });
    if (!evidence || !evidence.length) {
      append(row, make('span', { className: 'muted', text: '证据当前不可见' }));
      return row;
    }
    for (const item of evidence) {
      const label = item.memory_id + (item.date ? ' · ' + item.date : '');
      const button = memoryButton(item.memory_id, label);
      button.title = (item.excerpt || '') + '（' + (item.date || '') + '）';
      append(row, button);
    }
    return row;
  }

  function assessmentCard(item, kind, people) {
    const cardClass = kind === 'manual' ? 'manual-card' : kind === 'terra' ? 'terra-card' : 'assisted-card';
    const card = make('div', { className: 'card ' + cardClass });
    const personName = (id) => id === 'hakimi' ? '哈基米（本机）'
      : ((people && people[id]) || ('用户' + id));
    const head = make('div', { className: 'card-head' });
    append(head,
      tag(kind === 'manual' ? '亲笔评分' : kind === 'terra' ? 'Terra 观察' : '辅助整理',
        kind === 'manual' ? 'tag-manual' : kind === 'terra' ? 'tag-terra' : 'tag-assisted'),
      make('span', { className: 'card-title', text: item.relationship_type || '关系印象' }),
      make('span', { className: 'card-time', text: assessmentDate(item) })
    );
    append(card, head);
    append(card, make('div', { className: 'card-text', text: item.impression || '' }));
    if (item.reason) append(card, make('div', { className: 'reason', text: '依据：' + item.reason }));
    const grid = make('div', { className: 'score-grid' });
    append(grid,
      scoreBox('好感（主观）', item.affinity, kind),
      scoreBox('亲近（主观）', item.closeness, kind),
      confidenceRow(item.confidence, kind),
      statBox('版本', 'v' + item.version)
    );
    append(card, grid);
    const relation = make('div', { className: 'relation-line' });
    append(relation,
      make('span', { text: personName(item.subject_id) + ' → ' + personName(item.target_id) }),
      item.original_memory_id ? memoryButton(item.original_memory_id, item.original_memory_id + ' 原文') : null
    );
    append(card, relation);
    if (item.evidence_dates && item.evidence_dates.length) {
      append(card, make('div', { className: 'muted',
        text: '证据日期：' + item.evidence_dates.join('、') }));
    }
    append(card, evidenceRow(item.evidence));
    if (item.evidence_missing && item.evidence_missing.length) {
      append(card, make('div', { className: 'warn-line',
        text: '部分证据已删除或不可见：' + item.evidence_missing.join('、') + '；相关分数不再作为可见依据。' }));
    }
    if (item.evidence_expired && item.evidence_expired.length) {
      append(card, make('div', { className: 'warn-line',
        text: '证据原文已被清理：' + item.evidence_expired.join('、') + '；原评分降级为待了解。' }));
    } else if (item.scores_note && !item.scores_available) {
      append(card, make('div', { className: 'warn-line', text: item.scores_note }));
    }
    if (item.stale) {
      append(card, make('div', { className: 'warn-line',
        text: 'Terra 观察的证据缺失、已被取代或不可核对；分数不再作为当前依据，但不按时间自动过期。' }));
    }
    if (item.staleness) {
      if (item.staleness.needs_update) {
        append(card, make('div', { className: 'warn-line',
          text: `整理后又出现 ${item.staleness.new_messages} 条相关新消息，待更新；旧推测不代表当前情绪。` }));
      } else {
        append(card, make('div', { className: 'good-line', text: '截至当前未见更新的目标消息。' }));
      }
    }
    return card;
  }

  function manualCard(item) {
    const card = make('div', { className: 'card manual-card' + (item.is_latest === false ? ' historical-card' : '') });
    const head = make('div', { className: 'card-head' });
    append(head,
      tag('Gemini 手写原文', 'tag-manual'),
      tag(item.is_latest === false ? '历史版本' : '当前版本',
        item.is_latest === false ? 'tag-unknown' : 'tag-manual'),
      make('span', { className: 'card-title', text: item.key || KIND_LABELS[item.kind] || item.kind }),
      make('span', { className: 'card-time', text: formatTime(item.time, true) })
    );
    append(card, head);
    append(card, make('div', { className: 'card-text', text: item.text || '' }));
    const meta = make('div', { className: 'relation-line' });
    append(meta,
      memoryButton(item.memory_id, item.memory_id),
      make('span', { className: 'muted', text: sessionLabel(item.session) }),
      make('span', { className: 'muted', text: item.state === 'superseded' ? '历史版本' : '当前版本' }),
      item.hash ? make('span', { className: 'muted', text: 'hash ' + item.hash.slice(0, 12) + '…' }) : null
    );
    append(card, meta);
    return card;
  }

  function interactionCard(data, personName, options) {
    const card = make('div', { className: 'card' });
    const score = data.score;
    const settings = options || {};
    const title = settings.title || '与哈基米（本机）';
    const nameOf = settings.nameOf || ((id) => id === 'hakimi' ? '哈基米（本机）' : personName);
    append(card, make('div', { className: 'card-head' }, [
      tag('互动熟悉度', 'tag-interaction'),
      make('span', { className: 'card-title', text: title }),
      make('span', { className: 'card-time', text: score === null ? '待了解' : score + ' / 100' })
    ]));
    const basis = data.basis || {};
    const grid = make('div', { className: 'score-grid' });
    append(grid,
      scoreBox('熟悉度', score, 'interaction'),
      statBox('双向天数', basis.mutual_days),
      statBox('互动次数', basis.events),
      statBox('覆盖天数', basis.days)
    );
    append(card, grid);
    if (basis.reason) append(card, make('div', { className: 'interaction-basis', text: basis.reason }));
    const details = data.stats;
    if (details && details.direction_events) {
      const left = details.left;
      const right = details.right;
      const parts = Object.entries(details.direction_events).map(([key, value]) => {
        const other = key === left ? right : left;
        const direction = nameOf(key) + ' → ' + nameOf(other);
        return direction + ' ' + value + ' 次';
      });
      if (parts.length) append(card, make('div', { className: 'interaction-basis', text: parts.join('；') }));
    }
    return card;
  }

  function relationNames(data, item) {
    const people = data.people || {};
    const name = (id) => id === 'hakimi' ? '哈基米（本机）' : (people[id] || ('用户' + id));
    return name(item.subject_id) + ' → ' + name(item.target_id);
  }

  function renderPersonDetail(data) {
    const person = data.person;
    const root = clear(dom.detailContent);
    dom.detailEmpty.hidden = true;
    dom.detailContent.hidden = false;
    const head = make('div', { className: 'detail-head' });
    append(head,
      make('div', { className: 'avatar', text: displayName(person.nickname, person.id).replace('…', '').slice(0, 1) || '用' }),
      make('div', {},
        [make('h2', { text: displayName(person.nickname, person.id) }),
         make('div', { className: 'detail-sub', text: '官方ID ' + person.id + ' · ' + person.sessions.length + ' 个会话' }),
         make('div', { className: 'muted', text: '最近活动 ' + formatTime(person.last_at, true) })]),
      make('div', { className: 'detail-actions' },
        [make('button', {
          type: 'button', className: 'ghost-button', text: '展开此人的关系',
          onClick: () => {
            state.focus = 'person:' + person.id;
            state.selectedEdge = null;
            loadGraph();
          }
        })])
    );
    append(root, head);
    const links = make('div', {className:'panel-context-links'});
    for (const session of (person.sessions || [])) {
      const value = typeof session === 'string' ? session : session.session;
      if (!value) continue;
      append(links, make('a', {href:'/messages.html?' + new URLSearchParams({session:value, person:person.id}), text:'查看' + sessionLabel(value) + '的消息'}));
    }
    append(root, links);
    const graphNode = (state.graph.nodes || []).find(node => node.id === 'person:' + person.id);
    if (graphNode && graphNode.selection_basis) {
      const basis = graphNode.selection_basis;
      const reasonSection = section('为什么出现在图中', '按有效关系、互动和近期证据选取，筛选后再限制人数');
      append(reasonSection, make('div', { className: 'empty-card', text:
        '有效连接 ' + (basis.supported_links || 0) + ' · 互动记录 ' + (basis.interaction_events || 0) +
        (basis.relationship_last_at ? ' · 最近关系证据 ' + formatTime(basis.relationship_last_at, true) : '') }));
      append(root, reasonSection);
    }

    if (data.behavior) {
      const b = data.behavior;
      const labels = {warm:'更愿意亲近',neutral:'自然相处',reserved:'有所保留',distant:'保持距离',unknown:'待了解'};
      const behaviorSection = section('当前相处偏好', '仅依据她自己的有效判断');
      append(behaviorSection, make('div', {className:'empty-card', text:
        (labels[b.band] || '待了解') + (b.known ? ' · 好感 ' + b.affinity : '') +
        (b.protected ? ' · 账号豁免拒收' : b.at_gate ? ' · 此人的@不直接唤醒' : '')}));
      append(root, behaviorSection);
    }

    const manualSection = section('她记下的印象', 'Gemini 手写原文 · 逐字保留');
    if (data.impressions.manual.length) {
      for (const item of data.impressions.manual) append(manualSection, manualCard(item));
    } else {
      append(manualSection, make('div', { className: 'empty-card', text: '还没有关于这个人的亲笔印象原文。' }));
    }
    append(root, manualSection);

    const history = data.identity_history;
    if (history && history.available && history.links && history.links.length) {
      const historySection = section('旧群/旧成员历史', '已确认对应 · 历史原文或旧摘要，不是当前新发生的话');
      for (const link of history.links) {
        const card = make('div', { className: 'card' });
        const oldName = window.panelSessionName({session: 'group:' + link.old_group, name: link.old_group_name});
        const newName = sessionLabel('group:' + link.new_group);
        append(card, make('div', { className: 'card-head' }, [
          make('span', { className: 'card-title', text: '旧群 ' + oldName + ' → 新群 ' + newName }),
          tag('已确认对应', 'tag-assisted')
        ]));
        if (link.notes && link.notes.length) {
          for (const note of link.notes) {
            append(card, make('div', { className: 'compact-memory' }, [
              make('button', {
                type: 'button', className: 'memory-link', text: note.memory_id,
                onClick: () => openLegacyMemory(note.memory_id, link.new_group)
              }),
              make('span', { className: 'preview', text: note.preview || '' }),
              make('span', { className: 'memory-meta', text: note.origin || '旧记录' })
            ]));
          }
        } else {
          append(card, make('div', { className: 'empty-card', text: '尚无旧笔记；身份匹配已保存。' }));
        }
        append(historySection, card);
      }
      append(root, historySection);
    }

    const terraSection = section('Terra 观察', '固定两小时抽样 · 辅助推断，不是当事人内心；显示来源日期与证据状态，不做时间过期');
    if (data.impressions.terra && data.impressions.terra.length) {
      for (const item of data.impressions.terra) append(terraSection, assessmentCard(item, 'terra', data.people));
    } else {
      append(terraSection, make('div', { className: 'empty-card', text: '暂无 Terra 观察；没有材料就保持未知。' }));
    }
    append(root, terraSection);

    const assistedSection = section('辅助整理', 'DSH 根据公开证据整理 · 不是当事人内心');
    if (data.impressions.assisted.length) {
      for (const item of data.impressions.assisted) append(assistedSection, assessmentCard(item, 'assisted', data.people));
    } else {
      append(assistedSection, make('div', { className: 'empty-card', text: '暂无辅助整理；未知就保持待了解。' }));
    }
    append(root, assistedSection);

    if (data.impressions.handwritten_scores.length) {
      const scoreSection = section('亲笔评分', '模型通过 memory.social 写下的主观评分');
      for (const item of data.impressions.handwritten_scores) append(scoreSection, assessmentCard(item, 'manual', data.people));
      append(root, scoreSection);
    }

    const interactionSection = section('互动熟悉度', '只统计真实@/引用，不是好感');
    append(interactionSection, interactionCard(data.interaction.with_hakimi, person.nickname));
    if (data.interaction.partners && data.interaction.partners.length) {
      const partners = make('div', { className: 'card' });
      append(partners, make('div', { className: 'card-head' }, [
        make('span', { className: 'card-title', text: '与其他人的公开互动' })
      ]));
      for (const partner of data.interaction.partners) {
        const line = make('div', { className: 'compact-memory' });
        append(line,
          make('span', { className: 'preview', text: partner.name }),
          make('span', { className: 'memory-meta',
            text: partner.score === null ? '待了解' : partner.score + ' / 100' }),
          partner.stats ? make('span', { className: 'memory-meta',
            text: partner.stats.events + ' 次 / ' + partner.stats.days.length + ' 天' }) : null
        );
        append(partners, line);
      }
      append(interactionSection, partners);
    }
    append(root, interactionSection);

    const relationSection = section('相关关系', '方向与来源明确');
    if (data.relations && data.relations.length) {
      for (const item of data.relations) {
        const card = make('div', { className: 'card' });
        const headRow = make('div', { className: 'card-head' });
        append(headRow,
          tag(provenanceLabel(item.provenance),
            item.provenance === 'gemini_manual' ? 'tag-manual'
              : item.provenance === 'terra_assisted' ? 'tag-terra' : 'tag-assisted'),
          make('span', { className: 'card-title', text: item.relationship_type || '关系印象' }),
          make('span', { className: 'card-time', text: assessmentDate(item) })
        );
        append(card, headRow);
        append(card, make('div', { className: 'relation-line' }, [
          make('span', { text: relationNames(data, item) })
        ]));
        if (item.reason) append(card, make('div', { className: 'reason', text: item.reason }));
        const scoreKind = provenanceKind(item.provenance);
        const scores = [scoreBox('好感', item.affinity, scoreKind),
                        scoreBox('亲近', item.closeness, scoreKind)];
        const grid = make('div', { className: 'score-grid' });
        append(grid, ...scores);
        append(card, grid);
        if (item.evidence_dates && item.evidence_dates.length) {
          append(card, make('div', { className: 'muted', text: '证据日期：' + item.evidence_dates.join('、') }));
        }
        append(card, evidenceRow(item.evidence));
        append(card, make('button', {
          type: 'button', className: 'memory-link', text: '查看这段关系 →',
          onClick: () => openRelationByIds(item.subject_id, item.target_id)
        }));
        append(relationSection, card);
      }
    } else {
      append(relationSection, make('div', { className: 'empty-card', text: '还没有可核对的关系印象。' }));
    }
    append(root, relationSection);

    const memoriesSection = section('记忆 / 聊天', '最近记录 · 点M编号读原文');
    if (data.memories && data.memories.length) {
      for (const item of data.memories) {
        const line = make('div', { className: 'compact-memory' });
        append(line,
          memoryButton(item.id, item.id),
          make('span', { className: 'preview', text: item.preview || '（无正文）' }),
          make('span', { className: 'memory-meta', text: KIND_LABELS[item.kind] || item.kind }),
          make('span', { className: 'memory-meta', text: formatTime(item.time, false) })
        );
        append(memoriesSection, line);
      }
    } else {
      append(memoriesSection, make('div', { className: 'empty-card', text: '暂无可见记忆。' }));
    }
    append(root, memoriesSection);
    dom.detailPane.scrollTop = 0;
  }

  function renderRelationDetail(data) {
    const root = clear(dom.detailContent);
    dom.detailEmpty.hidden = true;
    dom.detailContent.hidden = false;
    append(root, make('div', { className: 'detail-head' }, [
      make('div', { className: 'avatar', text: '⇄' }),
      make('div', {}, [
        make('h2', { text: data.left.name + ' ↔ ' + data.right.name }),
        make('div', { className: 'detail-sub', text: data.left.id + ' / ' + data.right.id }),
        make('div', { className: 'muted', text: '方向明确：subject → target；辅助整理不是当事人内心。' })
      ])
    ]));
    append(root, make('p', { className: 'muted', text: data.policy || '' }));
    append(root, make('div', { className: 'relation-line' }, [
      make('span', { text: data.left.name + ' ↔ ' + data.right.name }),
      make('span', { className: 'muted',
        text: '互动统计不分方向；下面的关系评分按 subject → target 标注。' })
    ]));
    const interactionSection = section('互动熟悉度', '只统计真实@/引用，不是好感');
    const people = { [data.left.id]: data.left.name, [data.right.id]: data.right.name };
    const nameOf = (id) => id === 'hakimi' ? '哈基米（本机）' : (people[id] || ('用户' + id));
    const card = interactionCard({
      score: data.interaction.score,
      basis: data.interaction.basis,
      stats: data.interaction.stats
    }, data.left.name + ' 与 ' + data.right.name, {
      title: '两人的公开互动',
      nameOf
    });
    append(interactionSection, card);
    append(root, interactionSection);
    if (data.directions && data.directions.length) {
      const directionSection = section('方向参考值', '两个方向分别展示，旧版本不会覆盖新版本');
      for (const item of data.directions) {
        const line = make('div', { className: 'compact-memory' });
        append(line,
          make('span', { className: 'preview',
            text: item.direction + ' · ' + (item.relationship_type || '关系印象') }),
          make('span', { className: 'memory-meta',
            text: provenanceLabel(item.provenance) +
              ' · 好感 ' + (item.affinity === null ? '待了解' : item.affinity) +
              ' · 亲近 ' + (item.closeness === null ? '待了解' : item.closeness) })
        );
        append(directionSection, line);
      }
      append(root, directionSection);
    }
    const assessmentSection = section('关系印象与评分', '两个方向分别展示');
    if (data.assessments && data.assessments.length) {
      for (const item of data.assessments) append(assessmentSection, assessmentCard(item,
        item.provenance === 'gemini_manual' ? 'manual' : 'assisted', people));
    } else {
      append(assessmentSection, make('div', { className: 'empty-card', text: '只有可观察互动，没有关系评分。' }));
    }
    append(root, assessmentSection);
    dom.detailPane.scrollTop = 0;
  }

  function focusNeighbours() {
    const neighbours = new Set();
    if (!state.focus) return neighbours;
    for (const edge of visibleEdges()) {
      if (edge.source === state.focus) neighbours.add(edge.target);
      if (edge.target === state.focus) neighbours.add(edge.source);
    }
    return neighbours;
  }

  function visibleEdges() {
    const edges = state.graph.edges || [];
    return edges.filter((edge) => {
      const score = metricScore(edge);
      if (state.threshold > 0) return score !== null && score !== undefined && score >= state.threshold;
      return true;
    });
  }

  function setGraph(data, options) {
    const focus = state.focus ? state.focus.replace('person:', '').replace('assistant:', '') : null;
    if (Number(data.limit) !== state.limit || (data.group || 'all') !== (state.group || 'all') ||
        (data.focus || null) !== focus) return;
    state.graph = data;
    const first = !state.positions.size;
    ensurePositions();
    if (options && options.reset || first) {
      layoutGraph(220);
    }
    renderGraph();
    renderGraphSummary();
  }

  function ensurePositions() {
    const nodes = state.graph.nodes || [];
    const centerX = WORLD.width / 2;
    const centerY = WORLD.height / 2;
    const focusNode = state.focus;
    const added = new Set();
    const engine = window.HakimiGraphLayout;
    const seed = engine ? engine.initialPositions(nodes, WORLD.width, WORLD.height, WORLD.pad) : null;
    nodes.forEach((node, index) => {
      if (state.positions.has(node.id)) return;
      if (seed && seed.has(node.id)) {
        state.positions.set(node.id, { ...seed.get(node.id) });
      } else {
        const angle = index * 2.399963229728653;
        const radius = (Math.min(WORLD.width, WORLD.height) / 2 - WORLD.pad - 30) * Math.sqrt((index + 1) / Math.max(1, nodes.length));
        const jitter = (hashString(node.id + ':' + index) % 47) - 23;
        state.positions.set(node.id, {
          x: clamp(centerX + Math.cos(angle) * (radius + jitter), WORLD.pad, WORLD.width - WORLD.pad),
          y: clamp(centerY + Math.sin(angle) * (radius + jitter), WORLD.pad, WORLD.height - WORLD.pad)
        });
      }
      added.add(node.id);
    });
    if (focusNode && added.has(focusNode) && !state.userMoved.has(focusNode)) {
      const position = state.positions.get(focusNode);
      position.x = centerX;
      position.y = centerY;
    } else if (!focusNode && added.has('assistant:hakimi') && !state.userMoved.has('assistant:hakimi')) {
      const position = state.positions.get('assistant:hakimi');
      position.x = centerX;
      position.y = centerY;
    }
  }

  function layoutGraph(iterations) {
    const nodes = state.graph.nodes || [];
    if (!nodes.length) return;
    const engine = window.HakimiGraphLayout;
    if (!engine) return;
    const positions = state.positions;
    const centerX = WORLD.width / 2;
    const centerY = WORLD.height / 2;
    const centered = [];
    if (state.focus && positions.has(state.focus) && !state.userMoved.has(state.focus)) {
      centered.push(state.focus);
    } else if (!state.focus && nodes.some((node) => node.id === 'assistant:hakimi' && !state.userMoved.has(node.id))) {
      centered.push('assistant:hakimi');
    }
    const pinned = new Set(centered);
    state.userMoved.forEach((id) => { if (positions.has(id)) pinned.add(id); });
    const result = engine.layout(nodes, visibleEdges(), {
      width: WORLD.width, height: WORLD.height, pad: WORLD.pad,
      spacing: Math.max(60, Number(state.spacing) || 110),
      iterations: iterations || 220,
      positions, pinned: Array.from(pinned), centerPinned: centered
    });
    // Update the existing Map in place.  Some callers (and the offline review
    // harness) hold a reference to the pre-layout Map, so replacing it would
    // make them audit stale initial positions instead of the fitted result.
    positions.clear();
    for (const [id, point] of result) positions.set(id, point);
    state.positions = positions;
  }

  function updateViewport() {
    if (state.viewportEl) state.viewportEl.setAttribute('transform',
      `translate(${state.panX} ${state.panY}) translate(${WORLD.width / 2} ${WORLD.height / 2}) scale(${state.zoom}) translate(${-WORLD.width / 2} ${-WORLD.height / 2})`);
  }

  function updateDraggedGeometry() {
    const id = state.drag && state.drag.id;
    const point = state.positions.get(id);
    if (!point || !state.viewportEl) return;
    for (const node of state.viewportEl.querySelectorAll('[data-node-id]')) {
      if (node.dataset.nodeId === id) node.setAttribute('transform', `translate(${point.x} ${point.y})`);
    }
    // Repaint only connected geometry, leaving the rest of the DOM intact.
    for (const group of state.viewportEl.querySelectorAll('[data-edge-source]')) {
      const a = state.positions.get(group.dataset.edgeSource), b = state.positions.get(group.dataset.edgeTarget);
      if (!a || !b || (group.dataset.edgeSource !== id && group.dataset.edgeTarget !== id)) continue;
      for (const line of group.querySelectorAll('line')) {
        line.setAttribute('x1', a.x); line.setAttribute('y1', a.y);
        line.setAttribute('x2', b.x); line.setAttribute('y2', b.y);
      }
      for (const label of group.querySelectorAll('text')) {
        label.setAttribute('x', (a.x + b.x) / 2);
        label.setAttribute('y', (a.y + b.y) / 2 - 4);
      }
      const arrow = group.querySelector('path');
      if (arrow) {
        const from = group.dataset.edgeSource === state.focus ? a : b;
        const to = from === a ? b : a;
        arrow.setAttribute('transform', `translate(${from.x + (to.x - from.x) * .67} ${from.y + (to.y - from.y) * .67}) rotate(${Math.atan2(to.y-from.y,to.x-from.x)*180/Math.PI})`);
      }
    }
  }

  function renderGraph() {
    if (!dom.graphSvg) return;
    const nodes = state.graph.nodes || [];
    const edges = visibleEdges();
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    const focus = state.focus && nodeById.has(state.focus) ? state.focus : null;
    const neighbours = focusNeighbours();
    const selectedEdge = state.selectedEdge;
    const edgeKey = (edge) => edge.source + '|' + edge.target;
    const focusEdges = new Map();
    if (focus) {
      for (const edge of edges) {
        if (edge.source === focus) focusEdges.set(edge.target, edge);
        else if (edge.target === focus) focusEdges.set(edge.source, edge);
      }
    }
    clear(dom.graphSvg);
    const viewport = svgNode('g', {
      transform: `translate(${state.panX} ${state.panY}) translate(${WORLD.width / 2} ${WORLD.height / 2}) scale(${state.zoom}) translate(${-WORLD.width / 2} ${-WORLD.height / 2})`
    });
    state.viewportEl = viewport;
    dom.graphSvg.append(viewport);
    dom.graphSvg.setAttribute('viewBox', `0 0 ${WORLD.width} ${WORLD.height}`);
    dom.canvasEmpty.hidden = nodes.length > 0;
    if (!nodes.length) {
      renderLegend();
      return;
    }
    for (const edge of edges) {
      const left = state.positions.get(edge.source);
      const right = state.positions.get(edge.target);
      if (!left || !right) continue;
      const score = metricScore(edge);
      const hasScore = score !== null;
      const visualScore = score;
      const dim = (focus && !(edge.source === focus || edge.target === focus)) ||
        (selectedEdge && edgeKey(selectedEdge) !== edgeKey(edge));
      const group = svgNode('g', {
        'data-edge-source': edge.source, 'data-edge-target': edge.target,
        class: dim ? 'edge-group dim' : 'edge-group'
      });
      const width = focus && !dim ? 2.6 : visualScore === null ? 1 : 0.8 + Number(visualScore) / 50;
      const lineClass = hasScore ? (state.measure === 'interaction' ? 'interaction' : state.source) : 'unknown';
      const line = svgNode('line', {
        x1: left.x, y1: left.y, x2: right.x, y2: right.y,
        class: 'edge ' + lineClass + ' ' + scoreColorVariable(visualScore).slice(2),
        'stroke-width': width,
        opacity: dim ? 0.10 : focus ? 0.95 : 0.6
      });
      const hit = svgNode('line', {
        x1: left.x, y1: left.y, x2: right.x, y2: right.y,
        class: 'edge-hit', opacity: 1
      });
      const hoverParts = [
        edge.relationship_type || '关系',
        '方向：' + directionSummary(edge),
        hasScore ? '当前 ' + scoreSourceLabel() + '：' + score + '/100'
          : '当前 ' + scoreSourceLabel() + '：待了解'
      ];
      hoverParts.push(focus && state.measure !== 'interaction' ? '箭头与颜色表示选中者对邻居；点开查看出处' : '点选人物查看其出发方向');
      append(hit, svgNode('title', {}, [document.createTextNode(hoverParts.join('\n'))]));
      hit.addEventListener('click', (event) => {
        event.stopPropagation();
        openRelation(edge);
      });
      group.append(line, hit);
      if (focus && state.measure !== 'interaction' && (edge.source === focus || edge.target === focus)) {
        const from = edge.source === focus ? left : right;
        const to = edge.source === focus ? right : left;
        const angle = Math.atan2(to.y - from.y, to.x - from.x);
        const x = from.x + (to.x - from.x) * 0.67;
        const y = from.y + (to.y - from.y) * 0.67;
        group.append(svgNode('path', {
          d: 'M -7 -4 L 0 0 L -7 4', transform: `translate(${x} ${y}) rotate(${angle * 180 / Math.PI})`,
          fill: 'none', stroke: `var(${scoreColorVariable(visualScore)})`,
          'stroke-width': 2, 'pointer-events': 'none'
        }));
      }
      const midX = (left.x + right.x) / 2;
      const midY = (left.y + right.y) / 2;
      const incident = focus && (edge.source === focus || edge.target === focus);
      if (incident && (visualScore !== null || state.measure !== 'interaction')) {
        const label = svgNode('text', {
          x: midX, y: midY - 4,
          class: 'edge-label' + (dim ? ' dim' : '')
        }, [document.createTextNode(focusedEdgeLabel(edge, focus, visualScore))]);
        group.append(label);
      }
      if (selectedEdge && edgeKey(selectedEdge) === edgeKey(edge)) {
        const directions = edgeDirections(edge);
        const directionText = directions.length > 1
          ? '双向 ' + directions.join(' / ')
          : directions.length ? '方向 ' + directions[0] : '方向未标注';
        group.append(svgNode('text', {
          x: midX, y: midY + 18, class: 'edge-direction-label'
        }, [document.createTextNode(directionText)]));
      }
      viewport.append(group);
    }
    for (const node of nodes) {
      const position = state.positions.get(node.id);
      if (!position) continue;
      const dim = focus && node.id !== focus && !neighbours.has(node.id);
      const pairEdge = focus ? focusEdges.get(node.id) : null;
      const pairScore = pairEdge ? metricScore(pairEdge) : null;
      const hasPairScore = pairScore !== null;
      const group = svgNode('g', {
        class: 'node ' + node.type + (focus === node.id ? ' focused' : '') +
          (hasPairScore ? ' has-score' : '') +
          (dim ? ' dim' : ''),
        transform: `translate(${position.x} ${position.y})`,
        'data-node-id': node.id
      });
      const degree = Number(node.degree || 0);
      const radius = node.type === 'assistant' ? 17 : 6 + 9 * Math.sqrt(degree / Math.max(1, ...nodes.map(item => Number(item.degree || 0))));
      if (hasPairScore && node.id !== focus) {
        const haloVariable = scoreColorVariable(pairScore);
        group.append(svgNode('circle', {
          r: radius + 6, class: 'node-halo ' + haloVariable.slice(2),
          opacity: dim ? 0.10 : focus ? 0.95 : 0.65
        }));
      }
      group.append(svgNode('circle', {
        r: radius, class: 'node-circle'
      }));
      group.append(svgNode('text', {
        y: 4, class: 'node-title'
      }, [document.createTextNode(node.type === 'assistant' ? '猫' : displayName(node.label, node.official_id).replace('…', '').slice(0, 1))]));
      if (state.labels) {
        group.append(svgNode('text', {
          y: radius + 16, class: 'node-label'
        }, [document.createTextNode(displayName(node.label, node.official_id))]));
        group.append(svgNode('text', {
          y: radius + 32, class: 'node-sub', opacity: dim ? 0.10 : focus ? 0.95 : 0.65
        }, [document.createTextNode(node.type === 'assistant' ? '本机' : shortId(node.official_id))]));
      }
      const pairInfo = focus ? '\n与当前选中人物：' + (hasPairScore ? pairScore + '/100（' + scoreSourceLabel() + '）'
        : '当前来源待了解') : '';
      append(group, svgNode('title', {}, [document.createTextNode(
        `${node.label || ''}\n官方ID ${node.official_id || ''}\n记录 ${node.records || 0} · 连接 ${node.degree || 0}${pairInfo}`
      )]));
      group.addEventListener('pointerdown', (event) => startDrag(event, node));
      viewport.append(group);
    }
    renderLegend();
  }

  function renderLegend() {
    clear(dom.metricLegend);
    const rangeGroup = make('span', { className: 'legend-group' }, [
      make('strong', { className: 'legend-title', text: '分数颜色' })
    ]);
    for (const range of SCORE_RANGES) {
      const key = make('span', { className: 'legend-key' });
      append(key,
        make('span', {
          className: 'legend-swatch ' + range.variable.slice(2)
        }),
        make('span', { text: range.label }));
      rangeGroup.append(key);
    }
    const current = state.measure === 'interaction'
      ? '互动熟悉度（独立指标，不是好感）'
      : (MEASURE_LABELS[state.measure] || state.measure) + ' · ' +
        (METRIC_LABELS[state.source] || state.source);
    const currentGroup = make('span', { className: 'legend-group legend-current' }, [
      make('strong', { className: 'legend-title', text: '当前' }),
      make('span', { className: 'legend-key active', text: current }),
      make('span', { className: 'legend-key', text: '门槛 ' + state.threshold })
    ]);
    const note = '未选中时颜色取双向均值（仅一方已知取单方）；选中后表示TA对邻居的评分，未知不按0。';
    dom.metricLegend.append(rangeGroup, currentGroup,
      make('span', { className: 'legend-note', text: note }));
  }

  function renderGraphSummary() {
    const counts = state.graph.counts || {};
    const shownNodes = counts.nodes || (state.graph.nodes || []).length;
    const shownEdges = (visibleEdges() || []).length;
    const totalNodes = counts.total_people_in_scope || shownNodes;
    const totalEdges = counts.total_edges_in_scope || shownEdges;
    let summary = `显示 ${shownNodes} 人 / ${shownEdges} 条关系`;
    if (totalNodes > shownNodes || totalEdges > shownEdges) {
      summary += ` · 范围内共 ${totalNodes} 人、${totalEdges} 条关系，画布只渲染有界视图`;
    }
    dom.graphSummary.textContent = summary + ' · 当前高亮 ' +
      (MEASURE_LABELS[state.measure] || state.measure) +
      (state.measure === 'interaction' ? '' : '（' + (METRIC_LABELS[state.source] || state.source) + '）');
    const selection = state.graph.selection || {};
    const basis = state.graph.nodes && state.graph.nodes.find(node => node.id === state.focus);
    const reason = basis && basis.selection_basis;
    if (reason) dom.graphSummary.textContent += ' · 入图依据：' + reason.supported_links + ' 条有效连接 / ' + reason.interaction_events + ' 次互动';
    if (!state.focus && selection.policy) dom.graphSummary.textContent += ' · 按关系与互动证据选取前 ' + state.limit + ' 人';
    dom.graphHint.textContent = (state.focus
      ? '已选中人物：查看TA出发的好感与方向；换选对方可查看反向。'
      : '默认隐藏边数字；选中人物后才显示相连边数字。拖动平移、滚轮缩放。') +
      ' 详情保留各方向与出处；完整官方ID在悬停和详情里。';
  }

  function startPan(event) {
    if (event.button !== 0) return;
    if (event.target.closest && (event.target.closest('.node') || event.target.closest('.edge-group'))) return;
    state.panDrag = {
      startX: event.clientX, startY: event.clientY,
      panX: state.panX, panY: state.panY, moved: false
    };
    event.preventDefault();
    window.addEventListener('pointermove', movePan);
    window.addEventListener('pointerup', endPan, { once: true });
  }

  function movePan(event) {
    if (!state.panDrag) return;
    const dx = event.clientX - state.panDrag.startX;
    const dy = event.clientY - state.panDrag.startY;
    if (Math.abs(dx) + Math.abs(dy) > 3) state.panDrag.moved = true;
    state.panX = state.panDrag.panX + dx;
    state.panY = state.panDrag.panY + dy;
    updateViewport();
  }

  function endPan() {
    window.removeEventListener('pointermove', movePan);
    const drag = state.panDrag;
    state.panDrag = null;
    if (drag && drag.moved) {
      state.panJustMoved = true;
      setTimeout(() => { state.panJustMoved = false; }, 220);
    }
  }

  function startDrag(event, node) {
    if (event.button !== 0) return;
    const position = state.positions.get(node.id);
    if (!position) return;
    const point = worldPoint(event);
    state.drag = {
      id: node.id,
      offsetX: position.x - point.x,
      offsetY: position.y - point.y,
      startX: event.clientX,
      startY: event.clientY,
      moved: false
    };
    event.preventDefault();
    event.stopPropagation();
    window.addEventListener('pointermove', moveDrag);
    window.addEventListener('pointerup', endDrag, { once: true });
  }

  function moveDrag(event) {
    if (!state.drag) return;
    if (Math.hypot(event.clientX - state.drag.startX, event.clientY - state.drag.startY) > 3) {
      state.drag.moved = true;
      state.userMoved.add(state.drag.id);
    }
    const point = worldPoint(event);
    const position = state.positions.get(state.drag.id);
    if (!position) return;
    position.x = clamp(point.x + state.drag.offsetX, WORLD.pad, WORLD.width - WORLD.pad);
    position.y = clamp(point.y + state.drag.offsetY, WORLD.pad, WORLD.height - WORLD.pad);
    if (!state.drag.frame) {
      state.drag.frame = requestAnimationFrame(() => {
        if (state.drag) state.drag.frame = null;
        updateDraggedGeometry();
      });
    }
  }

  function endDrag() {
    window.removeEventListener('pointermove', moveDrag);
    if (!state.drag) return;
    const drag = state.drag;
    state.drag = null;
    if (!drag.moved) {
      const node = (state.graph.nodes || []).find((item) => item.id === drag.id);
      if (node) activateNode(node);
    }
  }

  function activateNode(node) {
    state.selectedEdge = null;
    if (node.type === 'assistant') {
      state.focus = node.id;
      updatePersonSelection(null);
      state.selectedRelation = null;
      renderSelectedPeople();
      renderGraph();
      renderGraphSummary();
      clear(dom.detailContent);
      dom.detailEmpty.hidden = true;
      dom.detailContent.hidden = false;
      const box = make('div', { className: 'detail-empty' });
      append(box,
        make('div', { className: 'detail-empty-mark', text: '哈基米（本机）' }),
        make('p', { text: '这是本机节点。它与人物之间的边表示亲笔评分、DSH 辅助整理或 Terra 观察；具体印象请在左侧选择人物查看。' }),
        make('p', { className: 'muted', text: '本机节点不代表任何人际关系结论。' })
      );
      append(dom.detailContent, box);
      return;
    }
    selectPerson(node.official_id);
  }

  async function openRelation(edge) {
    state.selectedEdge = edge;
    updatePersonSelection(null);
    renderSelectedPeople();
    renderGraph();
    const left = edge.source === 'assistant:hakimi' ? 'hakimi' : edge.source.replace('person:', '');
    const right = edge.target === 'assistant:hakimi' ? 'hakimi' : edge.target.replace('person:', '');
    state.selectedRelation = { left, right };
    openRelationByIds(left, right);
  }

  async function openRelationByIds(left, right) {
    state.selectedRelation = { left, right };
    updatePersonSelection(null);
    renderSelectedPeople();
    clear(dom.detailContent).append(make('div', { className: 'detail-empty' },
      [make('div', { className: 'detail-empty-mark', text: '正在读取关系…' })]));
    dom.detailEmpty.hidden = true;
    dom.detailContent.hidden = false;
    try {
      const data = await apiGet('/api/relation', { a: left, b: right, group: state.group });
      renderRelationDetail(data);
    } catch (error) {
      if (error instanceof AuthError) return;
      clear(dom.detailContent).append(make('div', { className: 'detail-empty' },
        [make('div', { className: 'detail-empty-mark', text: '关系读取失败' }),
         make('p', { className: 'muted', text: error.message })]));
    }
  }

  function worldPoint(event) {
    const point = dom.graphSvg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const matrix = (state.viewportEl || dom.graphSvg).getScreenCTM();
    if (!matrix) return { x: 0, y: 0 };
    const transformed = point.matrixTransform(matrix.inverse());
    return { x: transformed.x, y: transformed.y };
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function hashString(value) {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  async function loadLibrary(showLoading, rethrow) {
    const request = ++state.libraryRequest;
    if (showLoading !== false) {
      clear(dom.memoryList).append(make('div', { className: 'empty-inline', text: '正在读取记忆库…' }));
    }
    try {
      const data = await apiGet('/api/memories', {
        q: state.library.q, group: state.group, kind: state.library.kind,
        person: state.library.person, page: state.library.page, page_size: 30
      });
      if (request !== state.libraryRequest) return true;
      state.library.page = data.page || 1;
      state.library.pages = data.pages || 1;
      state.library.total = data.total || 0;
      state.library.items = data.memories || [];
      renderLibrary();
      return true;
    } catch (error) {
      if (request !== state.libraryRequest) return true;
      if (rethrow) throw error;
      if (error instanceof AuthError) return false;
      clear(dom.memoryList).append(make('div', { className: 'empty-inline', text: '记忆库读取失败：' + error.message }));
      return false;
    }
  }

  function renderLibrary() {
    clear(dom.memoryList);
    const items = state.library.items || [];
    dom.clearPersonFilter.hidden = !state.library.person;
    const personName = state.personData && state.personData.person ? state.personData.person.nickname : '';
    dom.librarySummary.textContent = `${state.library.total} 条结果` +
      (state.library.person ? ' · 只看 ' + (personName || '用户' + state.library.person) : '') +
      ' · 全库搜索，列表只是分页视图';
    if (!items.length) {
      append(dom.memoryList, make('div', { className: 'empty-inline', text: '没有匹配的记忆。' }));
    } else {
      for (const item of items) {
        const row = make('button', {
          type: 'button', className: 'memory-row', title: '查看原文 ' + item.memory_id,
          onClick: () => openMemory(item.memory_id)
        });
        append(row,
          make('span', { className: 'memory-id', text: item.memory_id }),
          make('span', { className: 'memory-preview', text: item.preview || '（无正文）' }),
          make('span', { className: 'memory-kind', text: KIND_LABELS[item.kind] || item.kind }),
          make('span', { className: 'memory-meta', text: sessionLabel(item.session) + ' · ' + formatTime(item.time, true) }),
          make('span', { className: 'memory-meta', text: (item.provider || '来源未知') + (item.has_original ? ' · 有原文' : '') })
        );
        dom.memoryList.append(row);
      }
    }
    dom.memoryPageLabel.textContent = `${state.library.page} / ${state.library.pages}`;
    dom.memoryPrev.disabled = state.library.page <= 1;
    dom.memoryNext.disabled = state.library.page >= state.library.pages;
  }

  function switchView(view) {
    state.view = view;
    for (const button of document.querySelectorAll('[data-view]')) {
      button.classList.toggle('active', button.dataset.view === view);
    }
    dom.graphView.hidden = view !== 'graph';
    dom.libraryView.hidden = view !== 'library';
    if (view === 'library') loadLibrary();
    else renderGraph();
  }

  async function openLegacyMemory(memoryId, group) {
    const request = ++state.memoryRequest;
    dom.memoryModal.hidden = false;
    dom.memoryTitle.textContent = memoryId + ' · 旧记录';
    clear(dom.memoryBody).append(make('div', { className: 'empty-inline', text: '正在读取旧原文…' }));
    try {
      const data = await apiGet('/api/identity/memory', {
        id: memoryId, group: group || '', limit: 6000
      });
      if (request !== state.memoryRequest || dom.memoryModal.hidden) return;
      const body = clear(dom.memoryBody);
      dom.memoryTitle.textContent = (data.origin_label || '旧记录') + ' · ' + data.id;
      append(body, make('div', { className: 'memory-meta' }, [
        make('span', { text: data.identity_label || '' }),
        make('span', { text: '来源 ' + (data.origin_label || '旧记录') }),
        make('span', { text: formatTime(data.time, true) }),
        make('span', { text: '状态 ' + data.state })
      ]));
      append(body, make('div', { className: 'memory-meta' }, [
        make('span', { className: 'hash', text: '原文校验值 ' + (data.hash || '—') }),
        make('span', { text: '旧编号 ' + data.id })
      ]));
      append(body, make('pre', { className: 'memory-text', text: data.text || '' }));
      append(body, make('div', { className: 'muted',
        text: '这是通过已确认的群/成员匹配读取的旧记录，不会改写成当前新话。' }));
      if (data.has_more) {
        append(body, make('div', { className: 'muted',
          text: '原文还有后续；这里只显示前 6000 字符。' }));
      }
    } catch (error) {
      if (request !== state.memoryRequest || dom.memoryModal.hidden) return;
      if (error instanceof AuthError) return;
      clear(dom.memoryBody).append(make('div', { className: 'empty-card', text: '旧原文读取失败：' + error.message }));
    }
  }

  async function openMemory(memoryId, offset) {
    const request = ++state.memoryRequest;
    dom.memoryModal.hidden = false;
    dom.memoryTitle.textContent = memoryId + ' · 原文';
    clear(dom.memoryBody).append(make('div', { className: 'empty-inline', text: '正在读取原文…' }));
    try {
      const data = await apiGet('/api/memory', {
        id: memoryId, offset: offset || 0, limit: 2000
      });
      if (request !== state.memoryRequest || dom.memoryModal.hidden) return;
      state.memory = data;
      renderMemoryModal(data);
    } catch (error) {
      if (request !== state.memoryRequest || dom.memoryModal.hidden) return;
      if (error instanceof AuthError) return;
      clear(dom.memoryBody).append(make('div', { className: 'empty-card', text: '原文读取失败：' + error.message }));
    }
  }

  function renderMemoryModal(data) {
    const body = clear(dom.memoryBody);
    dom.memoryTitle.textContent = data.memory_id + ' · ' + (KIND_LABELS[data.kind] || data.kind);
    const meta = make('div', { className: 'memory-meta' });
    for (const text of [
      sessionLabel(data.session), '人物 ' + (data.user_id || '—'), formatTime(data.time, true),
      '状态 ' + data.state, '来源 ' + (data.provider || data.channel || '—'),
      '正文类型 ' + data.text_kind
    ]) append(meta, make('span', { text }));
    append(body, meta);
    const hash = make('div', { className: 'memory-meta' });
    append(hash, make('span', { className: 'hash', text: 'sha256 ' + (data.hash || '—') }));
    append(body, hash);
    append(body, make('pre', { className: 'memory-text', text: data.text || '' }));
    if (data.source) append(body, make('div', { className: 'reason', text: '出处：' + data.source }));
    if (data.evidence) append(body, make('div', { className: 'reason', text: '证据：' + data.evidence }));
    if (data.supersedes || data.superseded_by) {
      append(body, make('div', { className: 'muted',
        text: '版本链：' + (data.supersedes ? '上一版 ' + data.supersedes : '') +
          (data.supersedes && data.superseded_by ? ' · ' : '') +
          (data.superseded_by ? '已被 ' + data.superseded_by + ' 更正' : '') }));
    }
    const toolbar = make('div', { className: 'memory-toolbar' });
    const previousOffset = Math.max(0, data.offset - data.limit);
    const prev = make('button', {
      type: 'button', className: 'ghost-button', text: '上一段',
      onClick: () => openMemory(data.memory_id, previousOffset)
    });
    prev.disabled = data.offset <= 0;
    const next = make('button', {
      type: 'button', className: 'ghost-button', text: '下一段',
      onClick: () => openMemory(data.memory_id, data.next_offset)
    });
    next.disabled = !data.has_more;
    append(toolbar, prev, next,
      make('span', { className: 'muted',
        text: `字符 ${data.offset + 1}–${data.offset + data.returned_chars} / ${data.total_chars}` }));
    append(body, toolbar);
  }

  function closeMemory() {
    state.memoryRequest += 1;
    dom.memoryModal.hidden = true;
    state.memory = null;
  }

  function debounce(fn, delay) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  function bindEvents() {
    for (const button of document.querySelectorAll('[data-view]')) {
      button.addEventListener('click', () => switchView(button.dataset.view));
    }
    if (dom.identityEntry) {
      dom.identityEntry.addEventListener('click', () => {
        dom.identityEntry.href = '/identity.html' + (state.token
          ? '#token=' + encodeURIComponent(state.token) : '');
      });
    }
    dom.groupFilter.addEventListener('change', async () => {
      state.group = dom.groupFilter.value;
      state.focus = null;
      state.selectedEdge = null;
      state.selectedRelation = null;
      state.positions.clear();
      state.userMoved.clear();
      state.panX = 0;
      state.panY = 0;
      state.people.page = 1;
      state.library.page = 1;
      showDetailEmpty();
      await Promise.all([
        loadPeople(1).catch(() => null),
        loadGraph({ reset: true }).catch(() => null)
      ]);
      if (state.view === 'library') loadLibrary();
    });
    dom.peopleSearch.addEventListener('input', debounce(() => {
      state.peopleQuery = dom.peopleSearch.value.trim();
      state.people.page = 1;
      loadPeople(1);
    }, 280));
    dom.peoplePrev.addEventListener('click', () => {
      if (state.people.page > 1) loadPeople(state.people.page - 1);
    });
    dom.peopleNext.addEventListener('click', () => {
      if (state.people.page < state.people.pages) loadPeople(state.people.page + 1);
    });
    dom.nodeLimit.addEventListener('input', debounce(() => {
      state.limit = Number(dom.nodeLimit.value);
      dom.nodeLimitValue.textContent = state.limit;
      loadGraph({ reset: true });
    }, 220));
    dom.spacing.addEventListener('input', () => {
      state.spacing = Number(dom.spacing.value);
      dom.spacingValue.textContent = state.spacing;
      layoutGraph(90);
      renderGraph();
    });
    const requestThresholdGraph = debounce(() => loadGraph(), 220);
    dom.threshold.addEventListener('input', () => {
      state.threshold = Number(dom.threshold.value);
      dom.thresholdValue.textContent = state.threshold;
      renderGraph();
      renderGraphSummary();
      requestThresholdGraph();
    });
    dom.measure.addEventListener('change', () => {
      state.measure = dom.measure.value;
      if (state.measure === 'interaction') {
        dom.source.disabled = true;
      } else {
        dom.source.disabled = false;
      }
      renderGraph();
      renderGraphSummary();
    });
    dom.source.addEventListener('change', () => {
      state.source = dom.source.value;
      renderGraph();
      renderGraphSummary();
      loadGraph();
    });
    dom.labelToggle.addEventListener('change', () => {
      state.labels = dom.labelToggle.checked;
      renderGraph();
    });
    dom.zoomIn.addEventListener('click', () => {
      state.zoom = Math.min(2.6, state.zoom + 0.15);
      updateViewport();
    });
    dom.zoomOut.addEventListener('click', () => {
      state.zoom = Math.max(0.45, state.zoom - 0.15);
      updateViewport();
    });
    dom.zoomReset.addEventListener('click', () => {
      state.zoom = 1;
      state.panX = 0;
      state.panY = 0;
      state.positions.clear();
      state.userMoved.clear();
      ensurePositions();
      layoutGraph(240);
      renderGraph();
      renderGraphSummary();
    });
    dom.graphSvg.addEventListener('pointerdown', startPan);
    dom.graphSvg.addEventListener('wheel', (event) => {
      event.preventDefault();
      state.zoom = clamp(state.zoom * (event.deltaY > 0 ? 0.92 : 1.08), 0.45, 2.6);
      updateViewport();
    }, { passive: false });
    dom.graphSvg.addEventListener('click', (event) => {
      if (state.panJustMoved) return;
      if (event.target === dom.graphSvg) {
        const restoreOverview = Boolean(state.graph.focus) || state.threshold > 0;
        state.focus = null;
        state.selectedEdge = null;
        state.selectedRelation = null;
        renderGraph();
        renderGraphSummary();
        if (restoreOverview) loadGraph();
      }
    });
    dom.memorySearch.addEventListener('input', debounce(() => {
      state.library.q = dom.memorySearch.value.trim();
      state.library.page = 1;
      loadLibrary();
    }, 280));
    dom.memoryKind.addEventListener('change', () => {
      state.library.kind = dom.memoryKind.value;
      state.library.page = 1;
      loadLibrary();
    });
    dom.clearPersonFilter.addEventListener('click', () => {
      state.library.person = '';
      state.library.page = 1;
      loadLibrary();
    });
    dom.memoryPrev.addEventListener('click', () => {
      if (state.library.page > 1) { state.library.page -= 1; loadLibrary(); }
    });
    dom.memoryNext.addEventListener('click', () => {
      if (state.library.page < state.library.pages) { state.library.page += 1; loadLibrary(); }
    });
    dom.memoryClose.addEventListener('click', closeMemory);
    dom.memoryModal.addEventListener('click', (event) => {
      if (event.target === dom.memoryModal) closeMemory();
    });
    dom.tokenForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const token = dom.tokenInput.value.trim();
      if (token.length < 32) {
        dom.tokenError.hidden = false;
        dom.tokenError.textContent = '访问口令太短；请从本机入口重新打开，或完整复制本地访问口令。';
        return;
      }
      state.token = token;
      try { sessionStorage.setItem(LS_TOKEN, token); } catch (error) { /* private mode */ }
      dom.tokenInput.value = '';
      hideTokenGate();
      try {
        await loadAll();
        startPolling();
      } catch (error) {
        if (error instanceof AuthError) {
          showTokenGate('访问口令未通过验证，请从本机入口重新打开。');
        } else {
          showBanner('初始化失败：' + error.message, 'error');
        }
      }
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !dom.memoryModal.hidden) closeMemory();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && state.token && state.status) {
        // The five-second poll will pick it up; no extra data or model call.
        setOnline(true);
      }
    });
  }

  bootstrap();
})();
