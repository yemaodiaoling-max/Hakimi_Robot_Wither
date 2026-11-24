(function () {
  'use strict';

  var TOKEN_KEY = 'memoryConsoleToken';
  var OFFICIAL_URL = '/setup.html#sec-model';
  var REFRESH_MS = 60000;
  var bar = null;
  var started = false;
  var timer = null;

  function readToken() {
    try {
      return sessionStorage.getItem(TOKEN_KEY) || '';
    } catch (error) {
      return '';
    }
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function amount(value) {
    if (value === null || value === undefined || value === '') {
      return null;
    }
    var number = Number(value);
    if (!isFinite(number)) {
      return null;
    }
    if (Math.abs(number - Math.round(number)) < 0.000000001) {
      return '$' + String(Math.round(number));
    }
    return '$' + number.toFixed(2);
  }

  function addText(node, text) {
    node.appendChild(document.createTextNode(text));
  }

  function creditTime(value) {
    var text = String(value === null || value === undefined ? '' : value).trim();
    var match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|\+00:00)$/.exec(text);
    return match ? match[1] + ' ' + match[2] + ' UTC' : text;
  }

  function render(data) {
    if (!bar) {
      return;
    }
    clear(bar);
    var line = element('span', 'vertex-budget-main');
    var unknown = data.status !== 'ok' || !data.fresh;
    var blocked = data.blocked === true;
    var limit = amount(data.limit_usd);
    addText(line, limit === null ? '模型网关 · 月度上限在设置里调整' : '模型网关月限额 ' + limit);
    var spent = unknown ? null : amount(data.spent_usd);
    addText(line, spent === null ? ' · 本月本地估算暂不可用' : ' · 本月本地估算 ' + spent);
    if (blocked) addText(line, data.block_reason === 'manual_pause' ? ' · 已手动暂停' : ' · 通道暂不可用');
    bar.appendChild(line);

    var credits = data.credits || {};
    var official = credits.official_remaining_usd;
    var creditsLine = element('span', 'vertex-budget-credits');
    if (official === null || official === undefined) {
      addText(creditsLine, '花费按设置里的单价估算');
    } else {
      var officialAmount = amount(official);
      if (officialAmount === null) {
        addText(creditsLine, '花费按设置里的单价估算');
      } else {
        var observed = creditTime(credits.as_of);
        addText(creditsLine, '官方赠金快照 ' + officialAmount
          + (observed ? ' · 核对于 ' + observed : ' · 核对时间未知')
          + ' · 非实时' + (credits.stale === true ? ' · 快照已过期' : ''));
      }
    }
    bar.appendChild(creditsLine);

    var link = element('a', 'vertex-budget-link', '模型设置');
    link.href = OFFICIAL_URL;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    bar.appendChild(link);
    bar.hidden = false;
  }

  function hide() {
    if (bar) {
      bar.hidden = true;
    }
  }

  function refresh() {
    var token = readToken();
    if (!token) {
      hide();
      return;
    }
    fetch('/api/vertex-budget', {
      method: 'GET',
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/json'
      },
      cache: 'no-store',
      credentials: 'same-origin'
    }).then(function (response) {
      if (!response.ok) {
        throw new Error('status unavailable');
      }
      return response.json();
    }).then(function (body) {
      if (!body || body.ok !== true || !body.data) {
        throw new Error('status unavailable');
      }
      render(body.data);
    }).catch(function () {
      hide();
    });
  }

  function start() {
    if (started) {
      return;
    }
    started = true;
    refresh();
    timer = window.setInterval(refresh, REFRESH_MS);
  }

  function waitForLogin() {
    if (readToken()) {
      start();
      return;
    }
    window.setTimeout(waitForLogin, 2000);
  }

  function init() {
    bar = document.getElementById('vertexBudgetBar');
    if (!bar) {
      return;
    }
    if (readToken()) {
      start();
    } else {
      waitForLogin();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
