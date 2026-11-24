'use strict';

(function () {
  const LS_TOKEN = 'memoryConsoleToken';
  const $ = (id) => document.getElementById(id);
  const state = {
    token: '',
    catalog: null,
    links: null,
    revision: 0,
    oldGroup: null,
    newGroup: null,
    oldMember: null,
    newMember: null,
    groupPreview: null,
    memberPreview: null,
    audit: [],
    matchTab: 'groupLinks',
    offline: false,
    request: 0,
    toastTimer: null,
  };

  const dom = {
    banner: $('banner'), statusDot: $('statusDot'), statusText: $('statusText'),
    revisionLabel: $('revisionLabel'), revisionTag: $('revisionTag'),
    refreshAll: $('refreshAll'),
    oldGroupSearch: $('oldGroupSearch'), newGroupSearch: $('newGroupSearch'),
    oldGroupList: $('oldGroupList'), newGroupList: $('newGroupList'),
    oldGroupCount: $('oldGroupCount'), newGroupCount: $('newGroupCount'),
    groupLinkCount: $('groupLinkCount'), memberLinkCount: $('memberLinkCount'),
    groupSelection: $('groupSelection'), memberSelection: $('memberSelection'),
    groupPreview: $('groupPreview'), memberPreview: $('memberPreview'),
    previewGroup: $('previewGroup'), saveGroup: $('saveGroup'),
    previewMember: $('previewMember'), saveMember: $('saveMember'),
    memberGroupPair: $('memberGroupPair'),
    oldMemberSearch: $('oldMemberSearch'), newMemberSearch: $('newMemberSearch'),
    oldMemberList: $('oldMemberList'), newMemberList: $('newMemberList'),
    oldMemberCount: $('oldMemberCount'), newMemberCount: $('newMemberCount'),
    manualOldUser: $('manualOldUser'), useManualOldUser: $('useManualOldUser'),
    groupLinksPane: $('groupLinksPane'), memberLinksPane: $('memberLinksPane'),
    auditPane: $('auditPane'), unlinkPreview: $('unlinkPreview'),
    tokenGate: $('tokenGate'), tokenForm: $('tokenForm'), tokenInput: $('tokenInput'),
    tokenError: $('tokenError'), legacyModal: $('legacyModal'), legacyBody: $('legacyBody'),
    legacyTitle: $('legacyTitle'), legacyClose: $('legacyClose'), toast: $('toast'),
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

  function shortId(value) {
    const text = String(value || '');
    return text.length > 14 ? text.slice(0, 6) + '…' + text.slice(-6) : text;
  }

  function formatTime(value) {
    if (!value) return '—';
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return '—';
    return date.toLocaleString('zh-CN', { hour12: false });
  }

  const SOURCE_LABELS = {
    config: '旧系统群资料',
    group_profiles: '旧系统群资料',
    review: '旧摘要',
    memory: '旧聊天记录',
    memory_md: '旧版手记',
    legacy_handwritten: '旧版手记',
    runtime_records: '新通道聊天记录',
    official_route: '新通道已出现的群',
    old_records: '旧聊天记录',
    user_bias: '旧昵称资料',
  };

  function sourceLabels(items) {
    const labels = [];
    for (const item of items || []) {
      const label = SOURCE_LABELS[item] || '旧记录';
      if (!labels.includes(label)) labels.push(label);
    }
    return labels.join('、');
  }

  function showBanner(text) {
    dom.banner.textContent = text;
    dom.banner.hidden = false;
  }

  function hideBanner() {
    dom.banner.hidden = true;
  }

  function toast(text) {
    dom.toast.textContent = text;
    dom.toast.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { dom.toast.hidden = true; }, 3600);
  }

  function setOnline(online) {
    state.offline = !online;
    dom.statusDot.classList.toggle('online', online);
    dom.statusDot.classList.toggle('offline', !online);
    if (!online) dom.statusText.textContent = '连接中断，请检查本地服务';
  }

  async function apiGet(path, params) {
    if (!state.token) throw new AuthError('missing token');
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    }
    const url = path + (query.toString() ? '?' + query.toString() : '');
    let response;
    try {
      response = await fetch(url, {
        method: 'GET', cache: 'no-store', credentials: 'same-origin',
        headers: { Authorization: 'Bearer ' + state.token, Accept: 'application/json' },
      });
    } catch (error) {
      setOnline(false);
      showBanner('无法连接本地服务。');
      throw error;
    }
    return parseResponse(response);
  }

  async function apiPost(path, body) {
    if (!state.token) throw new AuthError('missing token');
    let response;
    try {
      response = await fetch(path, {
        method: 'POST', cache: 'no-store', credentials: 'same-origin',
        headers: {
          Authorization: 'Bearer ' + state.token,
          'Content-Type': 'application/json',
          Accept: 'application/json',
        },
        body: JSON.stringify(body),
      });
    } catch (error) {
      setOnline(false);
      showBanner('无法连接本地服务。');
      throw error;
    }
    return parseResponse(response);
  }

  async function parseResponse(response) {
    if (response.status === 401) {
      loseToken();
      throw new AuthError('unauthorised');
    }
    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!response.ok || !payload || payload.ok !== true) {
      setOnline(true);
      const message = payload && payload.error && payload.error.message
        ? payload.error.message : '请求失败（HTTP ' + response.status + '）';
      throw new Error(message);
    }
    setOnline(true);
    hideBanner();
    return payload.data;
  }

  function loseToken() {
    state.token = '';
    try { sessionStorage.removeItem(LS_TOKEN); } catch (error) { /* private mode */ }
    showTokenGate('访问口令无效或已更换，请重新输入。');
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

  function setRevision(value) {
    state.revision = Number(value || 0);
    dom.revisionLabel.textContent = '版本 ' + state.revision;
    dom.revisionTag.textContent = '版本 ' + state.revision;
  }

  function updateCounts() {
    const catalog = state.catalog || { old_groups: [], new_groups: [] };
    const links = state.links || { group_links: [], member_links: [] };
    dom.oldGroupCount.textContent = (catalog.old_groups || []).length + ' 个旧群';
    dom.newGroupCount.textContent = (catalog.new_groups || []).length + ' 个新群';
    dom.groupLinkCount.textContent = '已匹配 ' + (links.group_links || []).length + ' 组';
    dom.memberLinkCount.textContent = '已匹配 ' + (links.member_links || []).length + ' 人';
  }

  // ---------------------------------------------------------------- rendering
  function copyButton(text) {
    return make('button', {
      type: 'button', className: 'copy-button', text: '复制',
      title: '复制编号 ' + text,
      onClick: (event) => {
        event.stopPropagation();
        copyText(String(text));
      },
    });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制 ' + text);
      return;
    } catch (error) { /* fall through */ }
    try {
      const area = make('textarea', { value: text, attrs: { readonly: 'readonly' } });
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.append(area);
      area.select();
      document.execCommand('copy');
      area.remove();
      toast('已复制 ' + text);
    } catch (error) {
      toast('复制失败，请手动选择编号');
    }
  }

  function idLine(id, name, selected) {
    return make('div', { className: 'id-line' + (selected ? ' selected' : '') }, [
      make('span', { className: 'id-code', text: shortId(id), title: id }),
      make('span', { className: 'id-name', text: name || '' }),
      copyButton(id),
    ]);
  }

  function groupName(id, side, fallback) {
    const groups = (state.catalog || {})[side === 'old' ? 'old_groups' : 'new_groups'] || [];
    const item = groups.find(group => String(group.id) === String(id));
    return window.panelSessionName({session: 'group:' + id, label: item && item.label,
      name: item && item.name, title: fallback});
  }

  function groupText(text) {
    let value = String(text || '');
    for (const side of ['old', 'new']) {
      for (const item of (state.catalog || {})[side + '_groups'] || []) {
        if (item.id) value = value.split(String(item.id)).join(groupName(item.id, side));
      }
    }
    return value;
  }

  function saveGroupContext() {
    const url = new URL(location.href);
    if (state.newGroup) url.searchParams.set('identity_group', 'group:' + state.newGroup.id);
    else url.searchParams.delete('identity_group');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
    window.dispatchEvent(new Event('panel-context-change'));
  }

  function renderGroupLists() {
    const catalog = state.catalog || {};
    const oldQuery = dom.oldGroupSearch.value.trim().toLowerCase();
    const newQuery = dom.newGroupSearch.value.trim().toLowerCase();
    const oldGroups = (catalog.old_groups || []).filter((item) =>
      !oldQuery || String(item.id).includes(oldQuery) || String(item.name || '').toLowerCase().includes(oldQuery));
    const newGroups = (catalog.new_groups || []).filter((item) =>
      !newQuery || String(item.id).includes(newQuery) || String(item.name || '').toLowerCase().includes(newQuery));

    clear(dom.oldGroupList);
    if (!oldGroups.length) {
      append(dom.oldGroupList, make('div', { className: 'empty-inline', text: '没有匹配的旧群' }));
    }
    for (const item of oldGroups) {
      const selected = state.oldGroup && state.oldGroup.id === item.id;
      const button = make('div', {
        className: 'pick-item' + (selected ? ' selected' : ''),
        attrs: { role: 'button', tabindex: '0' },
        onClick: () => selectOldGroup(item),
      }, [
        make('div', { className: 'pick-title', text: groupName(item.id, 'old') }),
        make('div', {className: 'pick-meta', text: (item.records || 0) + ' 条 · ' + (item.notes || 0) + ' 条笔记'}),
        make('div', { className: 'pick-meta', text: '来源：' + sourceLabels(item.sources) +
          (item.review_summaries ? ' · 含 ' + item.review_summaries + ' 条旧摘要' : '') +
          (item.handwritten_notes ? ' · 含 ' + item.handwritten_notes + ' 条旧版手记' : '') }),
      ]);
      append(dom.oldGroupList, button);
    }

    clear(dom.newGroupList);
    if (!newGroups.length) {
      append(dom.newGroupList, make('div', { className: 'empty-inline', text: '没有匹配的新群' }));
    }
    for (const item of newGroups) {
      const selected = state.newGroup && state.newGroup.id === item.id;
      const button = make('div', {
        className: 'pick-item' + (selected ? ' selected' : ''),
        attrs: { role: 'button', tabindex: '0', 'data-group-id': item.id },
        onClick: () => selectNewGroup(item),
      }, [
        make('div', { className: 'pick-title', text: groupName(item.id, 'new') }),
        make('div', {className: 'pick-meta', text: (item.records || 0) + ' 条记录'}),
        make('div', { className: 'pick-meta', text: '来源：' + sourceLabels(item.sources) +
          (item.route_active ? ' · 新通道已出现' : '') }),
      ]);
      append(dom.newGroupList, button);
    }
  }

  function selectOldGroup(item) {
    state.oldGroup = item;
    state.oldMember = null;
    state.newMember = null;
    state.groupPreview = null;
    renderGroupLists();
    renderSelections();
    loadMembers();
  }

  function selectNewGroup(item) {
    state.newGroup = item;
    saveGroupContext();
    state.oldMember = null;
    state.newMember = null;
    state.groupPreview = null;
    renderGroupLists();
    renderSelections();
    loadMembers();
  }

  function renderSelections() {
    const oldGroup = state.oldGroup;
    const newGroup = state.newGroup;
    dom.groupSelection.textContent = oldGroup && newGroup
      ? groupName(oldGroup.id, 'old') + ' → ' + groupName(newGroup.id, 'new')
      : (oldGroup ? '已选旧群，等待选择新群' : (newGroup ? '已选新群，等待选择旧群' : '尚未选择旧群和新群'));
    dom.previewGroup.disabled = !(oldGroup && newGroup);
    const gp = state.groupPreview;
    dom.saveGroup.disabled = !(oldGroup && newGroup && gp && gp.can_apply &&
      gp.old_group === oldGroup.id && gp.new_group === newGroup.id &&
      Number(gp.revision) === state.revision);
    const memberReady = !!(oldGroup && newGroup);
    dom.memberGroupPair.textContent = memberReady
      ? '当前成员匹配将使用：' + groupName(oldGroup.id, 'old') + ' → ' + groupName(newGroup.id, 'new')
      : '请先完成群匹配，或在上面选择一组旧群/新群。';
    dom.previewMember.disabled = !(memberReady && state.oldMember && state.newMember);
    const mp = state.memberPreview;
    dom.saveMember.disabled = !(memberReady && state.oldMember && state.newMember &&
      mp && mp.can_apply && mp.old_group === oldGroup.id && mp.new_group === newGroup.id &&
      mp.old_user === state.oldMember.old_user && mp.new_user === state.newMember.new_user &&
      Number(mp.revision) === state.revision);
    dom.memberSelection.textContent = state.oldMember && state.newMember
      ? '旧QQ ' + state.oldMember.old_user + '（' + (state.oldMember.old_nickname || '未命名') + '） → 新成员 ' +
        state.newMember.new_user + '（' + (state.newMember.new_nickname || '未命名') + '）'
      : (state.oldMember ? '已选旧QQ，等待选择新成员' : (state.newMember ? '已选新成员，等待选择旧QQ' : '尚未选择旧成员和新成员'));
  }

  function renderPreview(mount, preview, kind) {
    clear(mount);
    if (!preview) {
      mount.hidden = true;
      return;
    }
    mount.hidden = false;
    const conflicts = Array.isArray(preview.conflicts) ? preview.conflicts : [];
    const head = make('div', { className: 'preview-head' }, [
      make('strong', { text: groupText(preview.summary) || '匹配预览' }),
      make('span', { className: 'tag ' + (preview.can_apply ? 'ok' : 'bad'),
        text: preview.can_apply ? '可以保存' : (preview.already_linked ? '已经匹配' : '有冲突') }),
    ]);
    append(mount, head);
    if (preview.identity_label) append(mount, make('p', { className: 'muted', text: groupText(preview.identity_label) }));
    if (conflicts.length) {
      const list = make('ul', { className: 'conflict-list' });
      for (const conflict of conflicts) {
        append(list, make('li', { text: groupText(conflict.message || conflict.code) }));
      }
      append(mount, list);
    }
    if (kind === 'group') {
      const notes = preview.old_notes || {};
      append(mount, make('p', { className: 'preview-line',
        text: '旧群笔记预览：' + (notes.count || 0) + ' 条，显示 ' + ((notes.preview || []).length) + ' 条' }));
      renderNotePreview(mount, notes.preview || [], !!preview.group_linked);
      append(mount, make('p', { className: 'muted',
        text: '保存后，关系面板会把已确认成员的旧笔记标为“历史原文/旧摘要”，不会改写成当前新话。' }));
    } else {
      const notes = preview.old_notes || {};
      const noNotes = notes.no_old_notes || (notes.count === 0);
      append(mount, make('p', { className: 'preview-line' + (noNotes ? ' warn' : ''),
        text: noNotes ? '尚无旧笔记：仍可保存这条成员匹配，之后不会凭空生成旧内容。'
                      : '该旧QQ可预览旧笔记 ' + (notes.count || 0) + ' 条。' }));
      append(mount, make('p', { className: 'muted',
        text: '旧昵称：' + (preview.old_nickname || '未知') + ' · 新昵称：' + (preview.new_nickname || '未知') +
          ' · 旧成员目录：' + (preview.old_member_known ? '已存在' : '手填（尚无旧笔记）') +
          ' · 新成员本群记录：' + (preview.new_member_observed_in_group ? '有' : '未确认') }));
      renderNotePreview(mount, notes.preview || [], !!preview.group_linked);
    }
    if (kind === 'unlink' && preview.member_links_to_invalidate !== undefined) {
      append(mount, make('p', { className: 'warn',
        text: '解除预览：会失效 ' + preview.member_links_to_invalidate + ' 条成员匹配。' }));
      const list = make('ul', { className: 'member-invalidate-list' });
      for (const item of preview.member_links || []) {
        append(list, make('li', { text: '旧QQ ' + item.old_user + ' → 新成员 ' + item.new_user }));
      }
      append(mount, list);
      append(mount, make('button', {
        type: 'button', className: 'danger-button', text: '确认解除群匹配',
        onClick: () => applyUnlink('unlink_group', preview),
      }));
    }
    if (kind === 'unlink-member' && preview.old_user) {
      append(mount, make('button', {
        type: 'button', className: 'danger-button', text: '确认解除成员匹配',
        onClick: () => applyUnlink('unlink_member', preview),
      }));
    }
  }

  function renderNotePreview(mount, notes, canOpen) {
    if (!notes || !notes.length) {
      append(mount, make('p', { className: 'empty-inline', text: '没有可预览的旧笔记' }));
      return;
    }
    const list = make('div', { className: 'note-preview-list' });
    for (const note of notes) {
      // The operator may verify a known old note before any mapping is saved.
      // The admin read path is independent from the model's mapped reader.
      const open = note.memory_id
        ? make('button', {
            type: 'button', className: 'ghost-button small', text: '查看旧原文',
            onClick: () => openLegacy(note.memory_id),
          })
        : null;
      append(list, make('div', { className: 'note-preview' }, [
        make('div', { className: 'note-meta' }, [
          make('span', { className: 'tag', text: note.origin || '旧记录' }),
          make('span', { className: 'id-code', text: note.memory_id || '' }),
          make('span', { className: 'muted', text: note.at ? formatTime(note.at) : '' }),
          open,
        ]),
        make('p', { text: note.preview || '' }),
      ]));
    }
    append(mount, list);
  }

  function renderMemberLists() {
    const catalog = state.catalog || {};
    const oldMembers = (catalog.__oldMembers || []).filter((item) =>
      !dom.oldMemberSearch.value.trim() || String(item.old_user).includes(dom.oldMemberSearch.value.trim()) ||
      String(item.old_nickname || '').toLowerCase().includes(dom.oldMemberSearch.value.trim().toLowerCase()));
    const newMembers = (catalog.__newMembers || []).filter((item) =>
      !dom.newMemberSearch.value.trim() || String(item.new_user).includes(dom.newMemberSearch.value.trim()) ||
      String(item.new_nickname || '').toLowerCase().includes(dom.newMemberSearch.value.trim().toLowerCase()));
    dom.oldMemberCount.textContent = oldMembers.length + ' 人';
    dom.newMemberCount.textContent = newMembers.length + ' 人';
    clear(dom.oldMemberList);
    if (!oldMembers.length) {
      append(dom.oldMemberList, make('div', { className: 'empty-inline', text: '没有旧成员记录；可以在下面手填旧QQ。' }));
    }
    for (const item of oldMembers) {
      const selected = state.oldMember && state.oldMember.old_user === item.old_user;
      append(dom.oldMemberList, make('div', {
        className: 'pick-item compact' + (selected ? ' selected' : ''),
        attrs: { role: 'button', tabindex: '0' },
        onClick: () => selectOldMember(item),
      }, [
        make('div', { className: 'pick-title', text: item.old_nickname || ('旧QQ ' + item.old_user) }),
        idLine(item.old_user, (item.records || 0) + ' 条记录 · ' + (item.notes || 0) + ' 条笔记', selected),
        make('div', { className: 'pick-meta' + (item.has_notes ? '' : ' warn'),
          text: item.has_notes ? '旧笔记：' + item.notes + ' 条' : '尚无旧笔记' }),
      ]));
    }
    clear(dom.newMemberList);
    if (!newMembers.length) {
      append(dom.newMemberList, make('div', { className: 'empty-inline',
        text: '该新群还没有可核对的本群成员记录。' }));
    }
    for (const item of newMembers) {
      const selected = state.newMember && state.newMember.new_user === item.new_user;
      append(dom.newMemberList, make('div', {
        className: 'pick-item compact' + (selected ? ' selected' : ''),
        attrs: { role: 'button', tabindex: '0' },
        onClick: () => selectNewMember(item),
      }, [
        make('div', { className: 'pick-title', text: item.new_nickname || ('用户 ' + item.new_user) }),
        idLine(item.new_user, (item.records || 0) + ' 条本群记录', selected),
      ]));
    }
    renderSelections();
  }

  function selectOldMember(item) {
    state.oldMember = item;
    state.memberPreview = null;
    dom.memberPreview.hidden = true;
    renderMemberLists();
  }

  function selectNewMember(item) {
    state.newMember = item;
    state.memberPreview = null;
    dom.memberPreview.hidden = true;
    renderMemberLists();
  }

  function renderLinks() {
    const links = state.links || { group_links: [], member_links: [], audit: [] };
    updateCounts();
    clear(dom.groupLinksPane);
    if (!(links.group_links || []).length) {
      append(dom.groupLinksPane, make('div', { className: 'empty-inline', text: '还没有群匹配。' }));
    }
    for (const item of links.group_links || []) {
      append(dom.groupLinksPane, make('div', { className: 'link-row' }, [
        make('div', { className: 'link-main' }, [
          make('strong', { text: groupName(item.old_group, 'old', item.old_name) + ' → ' + groupName(item.new_group, 'new', item.new_name) }),
          make('div', { className: 'id-line' }, [
            make('span', {text: groupName(item.old_group, 'old', item.old_name)}),
            make('span', { className: 'arrow', text: '→' }),
            make('span', {text: groupName(item.new_group, 'new', item.new_name)}),
          ]),
        ]),
        make('div', { className: 'button-row' }, [
          make('button', {
            type: 'button', className: 'ghost-button small', text: '选择这组',
            onClick: () => selectMatchedGroup(item),
          }),
          make('button', {
            type: 'button', className: 'danger-button small', text: '解除 / 纠错',
            onClick: () => beginUnlink('unlink_group', {
              old_group: item.old_group, new_group: item.new_group,
            }),
          }),
        ]),
      ]));
    }

    clear(dom.memberLinksPane);
    if (!(links.member_links || []).length) {
      append(dom.memberLinksPane, make('div', { className: 'empty-inline', text: '还没有成员匹配。' }));
    }
    for (const item of links.member_links || []) {
      append(dom.memberLinksPane, make('div', { className: 'link-row' }, [
        make('div', { className: 'link-main' }, [
          make('strong', { text: '旧QQ ' + item.old_user + ' → 新成员编号 ' + item.new_user }),
          make('div', { className: 'id-line' }, [
            make('span', { className: 'id-code', text: item.old_user }),
            copyButton(item.old_user),
            make('span', { className: 'arrow', text: '→' }),
            make('span', { className: 'id-code', text: item.new_user }),
            copyButton(item.new_user),
          ]),
          make('div', { className: 'pick-meta', text: groupName(item.old_group, 'old', item.old_group_name) + ' → ' + groupName(item.new_group, 'new', item.new_group_name) }),
        ]),
        make('div', { className: 'button-row' }, [
          make('button', {
            type: 'button', className: 'ghost-button small', text: '选择这组',
            onClick: () => {
              const oldGroup = (state.catalog.old_groups || []).find((group) => group.id === item.old_group);
              const newGroup = (state.catalog.new_groups || []).find((group) => group.id === item.new_group);
              if (oldGroup) state.oldGroup = oldGroup;
              if (newGroup) state.newGroup = newGroup;
              state.oldMember = (state.catalog.__oldMembers || []).find(
                (member) => member.old_user === item.old_user) || {
                old_user: item.old_user, old_nickname: '旧QQ ' + item.old_user, has_notes: true,
              };
              state.newMember = (state.catalog.__newMembers || []).find(
                (member) => member.new_user === item.new_user) || {
                new_user: item.new_user, new_nickname: '新成员 ' + item.new_user,
              };
              renderGroupLists();
              renderMemberLists();
              loadMembers();
            },
          }),
          make('button', {
            type: 'button', className: 'danger-button small', text: '解除',
            onClick: () => beginUnlink('unlink_member', {
              old_group: item.old_group, new_group: item.new_group,
              old_user: item.old_user, new_user: item.new_user,
            }),
          }),
        ]),
      ]));
    }

    clear(dom.auditPane);
    const audit = links.audit || [];
    if (!audit.length) {
      append(dom.auditPane, make('div', { className: 'empty-inline', text: '还没有映射操作。' }));
    }
    for (const item of audit) {
      const actionLabel = {
        link_group: '匹配群', unlink_group: '解除群匹配',
        link_member: '匹配成员', unlink_member: '解除成员匹配',
      }[item.action] || item.action;
      append(dom.auditPane, make('div', { className: 'audit-row' }, [
        make('strong', { text: actionLabel }),
        make('span', { className: 'muted', text: formatTime(item.at) }),
        make('span', { className: 'id-code', text: '第 ' + item.revision + ' 次' }),
        make('p', { text: groupName(item.old_group, 'old', item.old_group_name) + ' → ' + groupName(item.new_group, 'new', item.new_group_name) +
          (item.old_user ? ' · 旧QQ ' + item.old_user + ' → 新ID ' + (item.new_user || '—') : '') +
          ' · 操作人：你手动确认' }),
      ]));
    }
    setRevision(links.revision);
  }

  function selectMatchedGroup(item) {
    const oldGroup = (state.catalog.old_groups || []).find((group) => group.id === item.old_group);
    const newGroup = (state.catalog.new_groups || []).find((group) => group.id === item.new_group);
    if (oldGroup) state.oldGroup = oldGroup;
    if (newGroup) state.newGroup = newGroup;
    saveGroupContext();
    renderGroupLists();
    renderSelections();
    loadMembers();
  }

  function restoreGroupContext(catalog, links) {
      const params = new URLSearchParams(location.search);
      const requested = params.get('identity_group') || params.get('group') || '';
      if (requested && !requested.startsWith('private:')) {
        const id = requested.replace(/^group:/, '');
        state.newGroup = (catalog.new_groups || []).find(group => String(group.id) === id) || null;
        const link = (links.group_links || []).find(row => String(row.new_group) === id);
        if (link) state.oldGroup = (catalog.old_groups || []).find(group => group.id === link.old_group) || null;
      }
  }

  // ------------------------------------------------------------------ loading
  async function loadAll() {
    const request = ++state.request;
    try {
      const [catalog, links] = await Promise.all([
        apiGet('/api/identity/catalog'),
        apiGet('/api/identity/links'),
      ]);
      if (request !== state.request) return;
      state.catalog = catalog;
      state.links = links;
      restoreGroupContext(catalog, links);
      setRevision(links.revision || catalog.revision || 0);
      dom.statusText.textContent = '目录已就绪';
      renderGroupLists();
      renderLinks();
      await loadMembers();
      renderSelections();
    } catch (error) {
      if (error instanceof AuthError) return;
      showBanner('加载失败：' + error.message);
    }
  }

  async function loadMembers() {
    if (!state.oldGroup || !state.newGroup) {
      state.catalog.__oldMembers = [];
      state.catalog.__newMembers = [];
      renderMemberLists();
      return;
    }
    const request = ++state.request;
    try {
      const [oldMembers, newMembers] = await Promise.all([
        apiGet('/api/identity/members', {
          side: 'old', group: state.oldGroup.id, q: dom.oldMemberSearch.value.trim(),
        }),
        apiGet('/api/identity/members', {
          side: 'new', group: state.newGroup.id, q: dom.newMemberSearch.value.trim(),
        }),
      ]);
      if (request !== state.request) return;
      state.catalog.__oldMembers = oldMembers.members || [];
      state.catalog.__newMembers = newMembers.members || [];
      if (state.oldMember && !state.catalog.__oldMembers.some(
        (item) => item.old_user === state.oldMember.old_user)) {
        state.oldMember = null;
      }
      if (state.newMember && !state.catalog.__newMembers.some(
        (item) => item.new_user === state.newMember.new_user)) {
        state.newMember = null;
      }
      renderMemberLists();
    } catch (error) {
      if (error instanceof AuthError) return;
      showBanner('成员目录加载失败：' + error.message);
    }
  }

  // ------------------------------------------------------------------ actions
  async function previewGroup() {
    if (!(state.oldGroup && state.newGroup)) return;
    try {
      const preview = await apiPost('/api/identity/preview', {
        action: 'link_group',
        old_group: state.oldGroup.id,
        new_group: state.newGroup.id,
        expected_revision: state.revision,
      });
      state.groupPreview = preview;
      renderPreview(dom.groupPreview, preview, 'group');
      dom.saveGroup.disabled = !preview.can_apply;
      if (preview.already_linked) toast('这组群已经匹配');
      else if (!preview.can_apply) toast('预览发现冲突，不能保存');
    } catch (error) {
      if (!(error instanceof AuthError)) toast('预览失败：' + error.message);
    }
  }

  async function saveGroup() {
    if (!(state.oldGroup && state.newGroup)) return;
    try {
      const result = await apiPost('/api/identity/apply', {
        action: 'link_group',
        old_group: state.oldGroup.id,
        new_group: state.newGroup.id,
        expected_revision: state.revision,
        confirm: true,
      });
      toast(result.changed ? '群匹配已保存' : '这组群已经匹配');
      await loadAll();
      if (result.changed && state.oldGroup && state.newGroup) {
        await loadMembers();
      }
    } catch (error) {
      if (!(error instanceof AuthError)) {
        toast('保存失败：' + error.message);
        if (/revision/i.test(error.message)) await loadAll();
      }
    }
  }

  async function previewMember() {
    if (!(state.oldGroup && state.newGroup && state.oldMember && state.newMember)) return;
    try {
      const preview = await apiPost('/api/identity/preview', {
        action: 'link_member',
        old_group: state.oldGroup.id,
        new_group: state.newGroup.id,
        old_user: state.oldMember.old_user,
        new_user: state.newMember.new_user,
        expected_revision: state.revision,
      });
      state.memberPreview = preview;
      renderPreview(dom.memberPreview, preview, 'member');
      dom.saveMember.disabled = !preview.can_apply;
      if (preview.old_notes && preview.old_notes.no_old_notes) toast('这个旧QQ尚无旧笔记，但仍可保存匹配');
      else if (preview.already_linked) toast('这条成员匹配已存在');
    } catch (error) {
      if (!(error instanceof AuthError)) toast('预览失败：' + error.message);
    }
  }

  async function saveMember() {
    if (!(state.oldGroup && state.newGroup && state.oldMember && state.newMember)) return;
    try {
      const result = await apiPost('/api/identity/apply', {
        action: 'link_member',
        old_group: state.oldGroup.id,
        new_group: state.newGroup.id,
        old_user: state.oldMember.old_user,
        new_user: state.newMember.new_user,
        expected_revision: state.revision,
        confirm: true,
      });
      toast(result.changed ? '成员匹配已保存' : '这条成员匹配已经存在');
      await loadAll();
    } catch (error) {
      if (!(error instanceof AuthError)) {
        toast('保存失败：' + error.message);
        if (/revision/i.test(error.message)) await loadAll();
      }
    }
  }

  async function beginUnlink(action, target) {
    try {
      const preview = await apiPost('/api/identity/preview', {
        action, old_group: target.old_group, new_group: target.new_group,
        old_user: target.old_user, new_user: target.new_user,
        expected_revision: state.revision,
      });
      renderPreview(dom.unlinkPreview, preview, action === 'unlink_group' ? 'unlink' : 'unlink-member');
      dom.unlinkPreview.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (error) {
      if (!(error instanceof AuthError)) toast('解除预览失败：' + error.message);
    }
  }

  async function applyUnlink(action, preview) {
    const body = {
      action, old_group: preview.old_group, new_group: preview.new_group,
      expected_revision: state.revision, confirm: true,
    };
    if (action === 'unlink_member') {
      body.old_user = preview.old_user;
      body.new_user = preview.new_user;
    }
    try {
      const result = await apiPost('/api/identity/apply', body);
      toast(result.changed
        ? (action === 'unlink_group'
          ? '群匹配已解除；失效成员匹配 ' + result.invalidated_members + ' 条'
          : '成员匹配已解除')
        : '没有变化');
      dom.unlinkPreview.hidden = true;
      await loadAll();
    } catch (error) {
      if (!(error instanceof AuthError)) {
        toast('解除失败：' + error.message);
        if (/revision/i.test(error.message)) await loadAll();
      }
    }
  }

  function useManualOldUser() {
    const value = dom.manualOldUser.value.trim();
    if (!/^[1-9][0-9]{0,19}$/.test(value)) {
      toast('请输入数字旧QQ号');
      return;
    }
    const existing = (state.catalog.__oldMembers || []).find((item) => item.old_user === value);
    state.oldMember = existing || {
      old_user: value, old_nickname: '手填旧QQ', records: 0, notes: 0,
      has_notes: false, manual: true,
    };
    renderMemberLists();
    toast('已选旧QQ ' + value + (existing && existing.has_notes ? '' : '（尚无旧笔记）'));
  }

  async function openLegacy(memoryId) {
    openLegacyModal();
    clear(dom.legacyBody).append(make('div', { className: 'empty-inline', text: '正在读取旧原文…' }));
    try {
      const data = await apiGet('/api/identity/memory', {
        id: memoryId, group: state.newGroup ? state.newGroup.id : '',
        limit: 6000,
      });
      const body = clear(dom.legacyBody);
      dom.legacyTitle.textContent = data.origin_label + ' · ' + data.id;
      append(body, make('div', { className: 'legacy-meta' }, [
        make('span', { className: 'tag', text: data.origin_label }),
        make('span', { text: data.identity_label || '' }),
        make('span', { className: 'muted', text: data.time ? formatTime(data.time) : '' }),
      ]));
      append(body, make('pre', { className: 'legacy-text', text: data.text || '' }));
      append(body, make('div', { className: 'legacy-meta muted' }, [
        make('span', { text: '原文校验值 ' + (data.hash || '—') }),
        make('span', { text: '旧记录编号 ' + data.id }),
      ]));
      if (data.has_more) {
        append(body, make('p', { className: 'warn', text: '原文还有后续；这里只显示前 6000 字符。' }));
      }
    } catch (error) {
      if (!(error instanceof AuthError)) {
        clear(dom.legacyBody).append(make('div', { className: 'empty-inline', text: '读取失败：' + error.message }));
      }
    }
  }

  function openLegacyModal() {
    dom.legacyModal.hidden = false;
  }

  function closeLegacy() {
    dom.legacyModal.hidden = true;
    clear(dom.legacyBody);
  }

  function switchMatchTab(tab) {
    state.matchTab = tab;
    for (const button of document.querySelectorAll('[data-match-tab]')) {
      button.classList.toggle('active', button.dataset.matchTab === tab);
    }
    dom.groupLinksPane.hidden = tab !== 'groupLinks';
    dom.memberLinksPane.hidden = tab !== 'memberLinks';
    dom.auditPane.hidden = tab !== 'audit';
  }

  function wire() {
    dom.refreshAll.addEventListener('click', () => { loadAll(); toast('正在刷新目录'); });
    dom.oldGroupSearch.addEventListener('input', renderGroupLists);
    dom.newGroupSearch.addEventListener('input', renderGroupLists);
    dom.oldMemberSearch.addEventListener('input', () => { loadMembers(); });
    dom.newMemberSearch.addEventListener('input', () => { loadMembers(); });
    dom.previewGroup.addEventListener('click', previewGroup);
    dom.saveGroup.addEventListener('click', saveGroup);
    dom.previewMember.addEventListener('click', previewMember);
    dom.saveMember.addEventListener('click', saveMember);
    dom.useManualOldUser.addEventListener('click', useManualOldUser);
    dom.legacyClose.addEventListener('click', closeLegacy);
    dom.legacyModal.addEventListener('click', (event) => {
      if (event.target === dom.legacyModal) closeLegacy();
    });
    for (const button of document.querySelectorAll('[data-match-tab]')) {
      button.addEventListener('click', () => switchMatchTab(button.dataset.matchTab));
    }
    dom.tokenForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const token = dom.tokenInput.value.trim();
      if (token.length < 32) {
        dom.tokenError.hidden = false;
        dom.tokenError.textContent = '访问口令太短；请完整复制本地访问口令。';
        return;
      }
      state.token = token;
      try { sessionStorage.setItem(LS_TOKEN, token); } catch (error) { /* private mode */ }
      dom.tokenInput.value = '';
      hideTokenGate();
      await loadAll();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeLegacy();
    });
    window.addEventListener('focus', () => {
      if (state.token && state.catalog) loadAll();
    });
  }

  async function boot() {
    wire();
    consumeFragmentToken();
    if (!state.token) {
      try { state.token = sessionStorage.getItem(LS_TOKEN) || ''; } catch (error) { state.token = ''; }
    }
    if (!state.token) {
      showTokenGate('');
      return;
    }
    hideTokenGate();
    await loadAll();
  }

  boot();
})();

