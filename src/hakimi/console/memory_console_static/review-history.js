'use strict';
(function () {
  const $ = id => document.getElementById(id);
  const state = { token: '', page: 1, view: 'changes', request: 0, groups: new Map() };
  const sources = { note: '人物笔记复核', window: '回复窗口', daily: '日常互动', legacy: '较早记录' };
  const text = (tag, value, className) => { const el = document.createElement(tag); el.textContent = String(value); if (className) el.className = className; return el; };
  const time = value => value ? new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '时间未知';
  const score = value => value === null || value === undefined ? '未知' : String(value);
  const groupName = session => state.groups.get(session) || (session ? window.panelSessionName({session}) : '来源已清理');
  function gate(message) { $('tokenGate').hidden = false; $('tokenError').hidden = !message; $('tokenError').textContent = message || ''; }
  async function api(path) {
    const response = await fetch(path, { headers: { Authorization: 'Bearer ' + state.token }, cache: 'no-store' });
    if (response.status === 401) { gate('请重新输入本地访问口令'); throw new Error('需要访问口令'); }
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error?.message || '读取失败');
    return data.data;
  }
  function dateBound(id, end) { const value = $(id).value; if (!value) return ''; return new Date(value + (end ? 'T23:59:59' : 'T00:00:00')).getTime() / 1000; }
  function changeCell(row, field) {
    const cell = document.createElement('td');
    const numbers = text('div', '', 'review-numbers');
    if (row.recovery && row[field + '_before'] == null && row[field + '_after'] == null) numbers.append(text('span', row.review_result?.status === 'unassessed' ? '无法评定' : row.review_result?.status === 'unknown' ? '未留存' : Number(row[field + '_delta'] || 0) === 0 ? '未加分' : (Number(row[field + '_delta']) > 0 ? '+' : '') + row[field + '_delta']));
    else numbers.append(text('span', score(row[field + '_before'])), text('span', '→', 'review-arrow'), text('span', score(row[field + '_after'])));
    cell.append(numbers);
    const delta = Number(row[field + '_delta'] || 0);
    if (row.recovery && row[field + '_delta'] == null) { cell.append(text('span', '未保存实际变化，无法推算', 'review-sub')); return cell; }
    cell.append(text('span', (row.recovery ? '本次实际变化 ' : row.legacy ? '保留效应 ' : state.view === 'daily' ? '合计变化 ' : '净变化 ') + (delta > 0 ? '+' : '') + delta, 'review-delta ' + (delta > 0 ? 'positive' : delta < 0 ? 'negative' : 'neutral')));
    return cell;
  }
  function renderHealth(health) {
    const banner = $('historyHealth');
    const summary = health?.summary;
    banner.hidden = !summary;
    if (!summary) return;
    const errors = Number(summary.errors || 0);
    const unresolved = Number(summary.unresolved_jobs ?? errors);
    const total = Number(summary.audit_total || 0);
    const unchanged = Number(summary.no_change_or_unscored || 0);
    banner.classList.toggle('error', unresolved > 0);
    const headline = unresolved > 0 ? '全局复盘处理状态：仍有待修复任务'
      : Number(summary.pending_recovery_jobs || 0) > 0 ? '全局复盘处理状态：正在补跑历史任务'
      : Number(summary.recovered_jobs || 0) > 0 ? '全局复盘处理状态：失败任务已补记，当前无失败待办'
      : total > 0 && unchanged === total ? '全局复盘处理状态：没有新增分数变化，部分事件未计分'
      : total > 0 ? '全局复盘处理状态：近期已有处理记录'
      : '全局复盘处理状态：近 1 小时暂无处理记录';
    banner.replaceChildren(text('strong', headline));
    banner.append(text('div', '近 1 小时审计 ' + total + ' 条 · 无变化或未计分 ' + unchanged
      + ' 条 · 历史失败尝试 ' + errors + ' 条 · 未结案任务 ' + unresolved + ' 个 · 待处理窗口 ' + Number(summary.pending_windows || 0), 'review-sub'));
    banner.append(text('div', '最近尝试：' + (summary.last_attempt_at ? time(summary.last_attempt_at) : '暂无')
      + ' · 全局状态不受人物、群或历史筛选影响', 'review-sub'));
    if (summary.recovered_jobs || summary.source_unavailable_jobs) banner.append(text('div', '已恢复或核对 ' + Number(summary.recovered_jobs || 0) + ' 个 · 原文已清理而无法重新判分 ' + Number(summary.source_unavailable_jobs || 0) + ' 个 · 等待补跑 ' + Number(summary.pending_recovery_jobs || 0) + ' 个（按原时间补记）', 'review-sub'));
    if (unresolved > 0) for (const error of health.error_codes || []) {
      banner.append(text('div', error.label + '（' + error.code + '，' + error.count + ' 次）', 'review-sub'));
    }
  }
  function render(data) {
    renderHealth(data.health);
    const body = $('historyRows'); body.replaceChildren();
    for (const row of data.items) {
      const tr = document.createElement('tr');
      const when = text('td', state.view === 'daily' ? row.day : time(row.at));
      when.append(text('span', sources[row.source] || row.source, 'review-source'));
      if (row.revision) when.append(text('span', '修订' + (row.revisions ? ' ×' + row.revisions : ''), 'review-revision'));
      if (state.view === 'daily') when.append(text('span', row.summary_events + ' 次复盘', 'review-sub'));
      const who = text('td', row.name || '用户' + row.target);
      who.append(text('span', groupName(row.session), 'review-sub'), text('span', 'ID ' + row.target, 'review-sub'));
      const why = text('td', ''); why.append(text('p', row.reason || (row.legacy ? '较早事件的简短原因已清理，保留已结算的影响。' : '未记录原因'), 'review-reason'));
      if (row.recovery && row.review_result) {
        why.replaceChildren(text('strong', row.review_result.label));
        for (const event of row.review_result.events || []) {
          if (event.excerpt) why.append(text('p', '原话：' + event.excerpt, 'review-reason'));
          why.append(text('p', '事件：' + event.description, 'review-reason'));
          why.append(text('p', '判定：' + event.decision_reason, 'review-reason'));
          if (event.details?.reviewer === 'operator_context_review') why.append(text('span', '依据当时原话和实际回复补判；保留原复盘结论', 'review-sub'));
          if (event.details?.previous_decision) why.append(text('span', '原复盘：' + event.details.previous_decision, 'review-sub'));
          if (event.event_id) why.append(text('span', '事件 M' + event.event_id, 'review-sub'));
          if (event.details?.reaction_evidence) why.append(text('p', '回应依据：' + event.details.reaction_evidence, 'review-reason'));
        }
      }
      if (row.legacy) why.append(text('span', '已浓缩 · 前后分数未知', 'review-sub'));
      else if (row.recovery) why.append(text('span', ['first_audit','first_matching_audit'].includes(row.time_basis) ? '按原首次复盘时间补记' : '首次复盘时间缺失，按原任务时间补记', 'review-sub'));
      else if (state.view === 'daily') why.append(text('span', '当天 ' + row.summary_events + ' 次复盘 · 显示最近一次简短原因', 'review-sub'));
      else why.append(text('span', '事件 M' + row.event_id + (row.original_id ? ' · 人物笔记 #' + row.original_id : ''), 'review-sub'));
      tr.append(when, who, changeCell(row, 'affinity'), changeCell(row, 'closeness'), why); body.append(tr);
    }
    $('historyEmpty').hidden = data.items.length > 0;
    $('historyCount').textContent = '共 ' + data.total + ' 条' + (state.view === 'daily' ? '日摘要' : '变化记录');
    $('historyPage').textContent = '第 ' + data.page + ' / ' + data.pages + ' 页';
    $('historyPrev').disabled = data.page <= 1; $('historyNext').disabled = !data.has_more;
    const saved = data.scores?.[0];
    $('historyScore').textContent = saved ? '累计好感 ' + score(saved.affinity) + ' · 累计亲近 ' + score(saved.closeness) : '累计分数独立于记录保留期限';
    $('historyStatus').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  }
  async function load() {
    if (!state.token) { gate(); return; }
    const request = ++state.request;
    $('historyStatus').textContent = '正在读取…'; $('historyError').hidden = true;
    const query = new URLSearchParams({ view: state.view, page: state.page, page_size: 30 });
    const filters = { person: $('historyPerson').value.trim(), group: $('historyGroup').value, source: $('historySource').value, since: dateBound('historySince', false), until: dateBound('historyUntil', true) };
    for (const [key, value] of Object.entries(filters)) if (value !== '') query.set(key, value);
    try { const data = await api('/api/review-history?' + query); if (request === state.request) render(data); }
    catch (error) { if (request !== state.request) return; $('historyError').hidden = false; $('historyError').textContent = error.message; $('historyStatus').textContent = '读取失败'; }
  }
  async function groups() {
    try {
      const data = await api('/api/groups');
      for (const row of data.groups || []) {
        const session = row.session || row.id;
        if (!session || state.groups.has(session)) continue;
        const name = window.panelSessionName(row);
        state.groups.set(session, name); const option = text('option', name); option.value = session; $('historyGroup').append(option);
      }
      const params=new URLSearchParams(location.search);$('historyGroup').value=params.get('group')||'';$('historyPerson').value=params.get('person')||'';
    } catch (error) { /* filters remain usable when the group directory is unavailable */ }
  }
  function applyFilters() {
    const url = new URL(location.href);
    for (const [key, control] of [['group', 'historyGroup'], ['person', 'historyPerson']]) {
      const value = $(control).value.trim();
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    history.replaceState(null, '', url.pathname + url.search + url.hash);
    window.dispatchEvent(new Event('panel-context-change'));
    state.page = 1;
    load();
  }
  $('historyFilters').addEventListener('submit', event => { event.preventDefault(); applyFilters(); });
  $('historyReset').addEventListener('click', () => { $('historyFilters').reset(); applyFilters(); });
  $('refreshHistory').addEventListener('click', load);
  $('historyPrev').addEventListener('click', () => { state.page = Math.max(1, state.page - 1); load(); });
  $('historyNext').addEventListener('click', () => { state.page += 1; load(); });
  for (const button of document.querySelectorAll('[data-history-view]')) button.addEventListener('click', () => {
    state.view = button.dataset.historyView; state.page = 1;
    for (const tab of document.querySelectorAll('[data-history-view]')) { const active = tab === button; tab.classList.toggle('active', active); tab.setAttribute('aria-selected', String(active)); }
    load();
  });
  $('tokenForm').addEventListener('submit', event => {
    event.preventDefault(); const token = $('tokenInput').value.trim();
    if (token.length < 32) { gate('请完整输入本地访问口令'); return; }
    state.token = token; try { sessionStorage.setItem('memoryConsoleToken', token); } catch (error) { /* private mode */ }
    $('tokenInput').value = ''; $('tokenGate').hidden = true; groups().then(load);
  });
  const fragment = new URLSearchParams(location.hash.slice(1));
  state.token = fragment.get('token') || '';
  if (state.token) { try { sessionStorage.setItem('memoryConsoleToken', state.token); } catch (error) { /* private mode */ } history.replaceState(null, '', location.pathname + location.search); }
  if (!state.token) { try { state.token = sessionStorage.getItem('memoryConsoleToken') || ''; } catch (error) { /* private mode */ } }
  if (state.token) groups().then(load); else gate();
})();
