(() => {
  'use strict';

  const fragment = new URLSearchParams(location.hash.slice(1));
  let token = fragment.get('token') || '';
  try {
    if (fragment.has('token')) sessionStorage.setItem('memoryConsoleToken', token);
    else token = sessionStorage.getItem('memoryConsoleToken') || '';
  } catch (error) {
    // A token supplied in this page's fragment still works with browser storage blocked.
  }
  if (fragment.has('token')) history.replaceState(null, '', location.pathname + location.search);

  const form = document.getElementById('drawing-form');
  const fields = document.getElementById('drawing-fields');
  const endpoint = document.getElementById('drawing-endpoint');
  const model = document.getElementById('drawing-model');
  const key = document.getElementById('drawing-key');
  const keyHelp = document.getElementById('drawing-key-help');
  const enabled = document.getElementById('drawing-enabled');
  const quotaStatus = document.getElementById('drawing-quota-status');
  const quotaTime = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'America/New_York', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  });
  const budget = {
    reserve_usd: document.getElementById('drawing-reserve'),
    daily_usd_limit: document.getElementById('drawing-daily-limit'),
    monthly_usd_limit: document.getElementById('drawing-monthly-limit'),
  };
  const status = document.getElementById('drawing-status');
  const retry = document.getElementById('drawing-retry');
  const tokenHelp = '请使用带本机面板令牌的地址打开此页。';

  function showStatus(message, error = false) {
    status.textContent = message;
    status.dataset.error = String(error);
  }

  function renderQuota(quota) {
    if (!quota) {
      quotaStatus.textContent = '暂时无法读取剩余次数。';
      return;
    }
    let text = `读取时本轮剩余 ${quota.window_remaining} 次，今日剩余 ${quota.daily_remaining} 次。`;
    if (Number.isFinite(quota.available_at)) {
      text += `最早可于 ${quotaTime.format(quota.available_at * 1000)} 再次提交（纽约时间）。`;
    }
    quotaStatus.textContent = text;
  }

  function render(settings) {
    endpoint.value = settings.endpoint || '';
    model.value = settings.model || '';
    enabled.checked = settings.enabled;
    key.value = '';
    keyHelp.textContent = settings.has_api_key
      ? '已保存密钥。留空可保留原密钥。更换 API 域名或端口时，请重新填写密钥。'
      : '尚未保存密钥。启用绘图前请填写该站点的 API Key。';
    for (const [name, input] of Object.entries(budget)) input.value = settings[name] ?? '';
    renderQuota(settings.quota);
  }

  async function request(settings) {
    const options = {
      headers: {Authorization: 'Bearer ' + token},
      cache: 'no-store',
      mode: 'same-origin',
      redirect: 'error',
    };
    if (settings) {
      options.method = 'POST';
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(settings);
    }
    const response = await fetch('/api/drawing', options);
    if (response.status === 401) {
      token = '';
      throw new Error(tokenHelp);
    }
    const body = await response.json().catch(() => null);
    if (!response.ok || !body?.ok) {
      throw new Error(body?.error?.message || '面板暂时不可用，请重试。');
    }
    return body.data;
  }

  async function load() {
    retry.hidden = true;
    if (!token) {
      showStatus(tokenHelp, true);
      return;
    }
    showStatus('正在读取绘图设置…');
    try {
      const settings = await request();
      render(settings);
      fields.disabled = false;
      showStatus(settings.enabled ? 'QQ 绘图已启用。' : 'QQ 绘图已停用。');
    } catch (error) {
      showStatus(error instanceof TypeError ? '无法读取绘图设置，请重试。' : error.message, true);
      retry.hidden = !token;
      keyHelp.textContent = '密钥状态暂不可用。';
    }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (fields.disabled) return;
    const settings = {endpoint: endpoint.value.trim(), model: model.value.trim(), enabled: enabled.checked};
    if (key.value.trim()) settings.api_key = key.value.trim();
    key.value = '';
    for (const [name, input] of Object.entries(budget)) settings[name] = input.value.trim();
    fields.disabled = true;
    showStatus('正在保存绘图设置…');
    try {
      const saved = await request(settings);
      render(saved);
      showStatus(saved.enabled ? '设置已保存，QQ 绘图已启用。' : '设置已保存，QQ 绘图已停用。');
    } catch (error) {
      showStatus(error instanceof TypeError ? '无法保存绘图设置，请重试。' : error.message, true);
    } finally {
      fields.disabled = !token;
    }
  });

  retry.addEventListener('click', load);
  window.addEventListener('pagehide', () => { key.value = ''; });
  load();
})();
