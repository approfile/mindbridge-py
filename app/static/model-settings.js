/* Keys exist only in password inputs/request bodies, never browser storage. */
(() => {
  const el = id => document.getElementById(id);
  let connections = [], profile = {revision: 0}, busy = false;
  async function api(path, options = {}) {
    let auth;
    try { auth = JSON.parse(sessionStorage.getItem('mindbridge.auth') || 'null'); } catch (_) {}
    if (!auth?.token) { window.location.replace('/'); throw new Error('请先登录'); }
    const response = await fetch('/api/model-profile' + path, {...options, headers: {
      Authorization: `Basic ${auth.token}`, 'Content-Type': 'application/json'
    }});
    if (response.status === 401) { window.location.replace('/'); throw new Error('请重新登录'); }
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '参数不合法，请检查输入。');
    return data;
  }
  function selectionChanged(operation, resetModel = true) {
    const connection = connections.find(c => c.id === el(operation + 'Connection').value);
    if (resetModel) el(operation + 'Model').value = connection?.defaultModel || '';
    const hosted = el('runtimeMode').value === 'selfhost' || connection?.kind === 'selfhost';
    el(operation + 'KeyLabel').hidden = hosted;
    el(operation + 'Key').value = '';
  }
  function renderSelections(useSaved = false) {
    const mode = el('runtimeMode').value;
    for (const operation of ['chat', 'embedding']) {
      const select = el(operation + 'Connection');
      select.replaceChildren();
      for (const connection of connections.filter(c => c.operation === operation && (mode !== 'selfhost' || c.kind === 'selfhost'))) {
        const option = document.createElement('option');
        option.value = connection.id;
        option.textContent = connection.label;
        select.append(option);
      }
      if (!select.options.length) {
        const option = document.createElement('option');
        option.value = ''; option.textContent = '尚未配置自部署服务'; option.disabled = true;
        select.append(option);
      }
      if (useSaved && profile[operation]) select.value = profile[operation].connection;
      selectionChanged(operation);
      if (useSaved && profile[operation]) el(operation + 'Model').value = profile[operation].model;
    }
    el('modeHelp').textContent = mode === 'selfhost'
      ? '生成、审核、摘要和知识检索全部使用管理员配置的自部署服务，不会自动切换到云端。若没有选项，请联系部署管理员配置服务。'
      : '对话和知识检索分别选择服务，各账号的 Key 加密隔离保存。';
    refreshAvailability();
  }
  function refreshAvailability() {
    const unavailable = ['chat','embedding'].some(op => !connections.some(c => c.id === el(op + 'Connection').value));
    el('testModels').disabled = busy || unavailable;
    document.querySelector('#modelSettingsForm button[type="submit"]').disabled = busy || unavailable;
  }
  async function load() {
    [connections, profile] = await Promise.all([api('/connections'), api('')]);
    el('runtimeMode').value = profile.mode === 'selfhost' ? 'selfhost' : 'api';
    el('currentMode').textContent = `当前：${({selfhost:'完整自部署',api:'个人 API 配置',deployment_default:'部署默认配置'})[profile.mode]}；版本 ${profile.revision}`;
    el('embeddingDimensions').value = profile.embeddingDimensions || 1024;
    el('resetModels').disabled = profile.mode === 'deployment_default';
    renderSelections(true);
  }
  function body() {
    const data = {expected_revision: profile.revision, mode: el('runtimeMode').value,
      embedding_dimensions: Number(el('embeddingDimensions').value)};
    for (const operation of ['chat', 'embedding']) {
      data[operation] = {connection: el(operation + 'Connection').value, model: el(operation + 'Model').value.trim()};
      if (el(operation + 'Key').value) data[operation].api_key = el(operation + 'Key').value;
    }
    return data;
  }
  async function action(kind) {
    if (busy) return;
    busy = true;
    const buttons = [...document.querySelectorAll('button')];
    buttons.forEach(button => { button.disabled = true; });
    el('settingsMessage').textContent = kind === 'reset' ? '正在撤销…' : '正在验证模型能力，请稍候。保存时还会建立知识索引…';
    try {
      if (kind === 'reset') {
        await api('?revision=' + profile.revision, {method:'DELETE'});
      } else {
        await api(kind === 'test' ? '/test' : '', {method:kind === 'test' ? 'POST' : 'PUT', body:JSON.stringify(body())});
      }
      if (kind !== 'test') await load();
      el('settingsMessage').textContent = ({test:'连接测试通过，尚未保存。',save:'配置和知识索引已保存，下一轮对话生效。',reset:'个人凭据已撤销，下一轮恢复部署默认配置。'})[kind];
    } catch (error) {
      el('settingsMessage').textContent = error.message;
    } finally {
      if (kind !== 'test') el('chatKey').value = el('embeddingKey').value = '';
      busy = false;
      buttons.forEach(button => { button.disabled = false; });
      refreshAvailability();
      el('resetModels').disabled = profile.mode === 'deployment_default';
    }
  }
  el('runtimeMode').addEventListener('change', () => renderSelections());
  for (const op of ['chat','embedding']) el(op + 'Connection').addEventListener('change', () => selectionChanged(op));
  el('testModels').addEventListener('click', () => { if (el('modelSettingsForm').reportValidity()) action('test'); });
  el('resetModels').addEventListener('click', () => action('reset'));
  el('modelSettingsForm').addEventListener('submit', event => { event.preventDefault(); action('save'); });
  load().catch(error => { el('settingsMessage').textContent = error.message; });
})();
