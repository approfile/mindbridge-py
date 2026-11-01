const AUTH_KEY = "mindbridge.auth";

const state = {
  profile: null,
  modelName: "mock"
};

const els = {
  serviceState: document.querySelector("#serviceState"),
  modelState: document.querySelector("#modelState"),
  activeAccount: document.querySelector("#activeAccount"),
  switchAccount: document.querySelector("#switchAccount"),
  refreshAdmin: document.querySelector("#refreshAdmin"),
  metricReports: document.querySelector("#metricReports"),
  metricHigh: document.querySelector("#metricHigh"),
  metricCases: document.querySelector("#metricCases"),
  metricExcel: document.querySelector("#metricExcel"),
  metricAlerts: document.querySelector("#metricAlerts"),
  cases: document.querySelector("#cases"),
  reports: document.querySelector("#reports"),
  conversationState: document.querySelector("#conversationState"),
  conversationDetail: document.querySelector("#conversationDetail"),
  knowledgeState: document.querySelector("#knowledgeState"),
  knowledgeUploadForm: document.querySelector("#knowledgeUploadForm"),
  knowledgeFile: document.querySelector("#knowledgeFile"),
  knowledgeUploadState: document.querySelector("#knowledgeUploadState"),
  rebuildVector: document.querySelector("#rebuildVector"),
  backupVector: document.querySelector("#backupVector"),
  privacyMode: document.querySelector('#privacyMode'),
  controlledRead: document.querySelector('#controlledRead'),
  accessPurpose: document.querySelector('#accessPurpose')
};

const usageEls = Object.fromEntries(['usageHours', 'usageState', 'usageMetrics', 'usageCaches', 'usageStages', 'usageRuns', 'usageDetail', 'usageDetailBody'].map(id => [id, document.getElementById(id)]));
const usageNumber = value => value == null ? '未知' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 1});
const usagePercent = value => value == null ? '无样本' : `${(value * 100).toFixed(1)}%`;
const usageMoney = cost => {
  const parts = Object.entries(cost?.knownAmounts || {}).map(([currency, amount]) => `${currency} ${Number(amount).toFixed(8)}`);
  return `${parts.join(' + ') || '无已知费用'}${cost?.unknownPriceAttempts ? ` · ${cost.unknownPriceAttempts} 笔未知` : ''}`;
};

function usageRow(body, values) {
  const row = document.createElement('tr');
  values.forEach(value => {
    const cell = document.createElement('td');
    if (value instanceof Node) cell.append(value); else cell.textContent = value;
    row.append(cell);
  });
  body.append(row);
}

async function loadModelUsage() {
  usageEls.usageState.textContent = '正在读取用量账本…';
  try {
    const [summaryResponse, runsResponse] = await Promise.all([
      api(`/api/admin/model-usage-summary?hours=${usageEls.usageHours.value}`), api('/api/admin/model-usage?limit=20')
    ]);
    const summary = await summaryResponse.json(), runs = await runsResponse.json();
    usageEls.usageState.textContent = `在线对话 · ${summary.runCount} 轮 · 用量覆盖 ${usagePercent(summary.usageCoverage)} · ${summary.unknownUsageAttempts} 笔用量未知 · ${summary.unfinishedRuns} 轮未完成${summary.truncated ? ' · 已达查询上限，统计不完整' : ''}${summary.ledgerWriteErrorsInFinalizedRuns ? ` · 检出 ${summary.ledgerWriteErrorsInFinalizedRuns} 次账本写入错误，请核对服务日志` : ''}。费用为有版本的公开价估算，USD / CNY 分开；未知不计为零。`;
    usageEls.usageMetrics.replaceChildren();
    const cards = [
      ['逻辑调用 / HTTP 尝试 / 重试', `${summary.logicalCalls} / ${summary.httpAttempts} / ${summary.retryAttempts}`],
      ['已知输入 / 输出 token', `${usageNumber(summary.tokens?.inputTokens)} / ${usageNumber(summary.tokens?.outputTokens)}`],
      ['已知费用（估算）', usageMoney(summary.cost)],
      ['轮次耗时 P50 / P95', `${usageNumber(summary.endToEndLatency.p50Ms)} / ${usageNumber(summary.endToEndLatency.p95Ms)} ms`]
    ];
    for (const [label, value] of cards) {
      const card = document.createElement('div'); card.className = 'metric';
      const title = document.createElement('span'), number = document.createElement('strong');
      title.textContent = label; number.textContent = value; card.append(title, number); usageEls.usageMetrics.append(card);
    }
    const cacheLabel = (key, label) => {
      const layer = summary.cacheLayers[key];
      return layer ? `${label} ${usagePercent(layer.hitRate)}（命中 ${layer.hits} / 未命中 ${layer.misses} / 错误 ${layer.errors} / 不可用 ${layer.bypasses}，${layer.unit}）` : `${label} 无样本`;
    };
    usageEls.usageCaches.textContent = `服务商前缀缓存 ${usagePercent(summary.providerCacheHitRate)}（用量覆盖 ${usagePercent(summary.cacheTokenCoverage)}，按输入 token 加权）；${cacheLabel('redis_session_history', 'Redis 历史')}；${cacheLabel('document_embedding_reuse', '文档向量复用')}。查询向量 / 检索结果 / 最终答案缓存：未实现。`;
    usageEls.usageStages.replaceChildren();
    for (const row of summary.byStage) usageRow(usageEls.usageStages, [
      `${row.agent} / ${row.stage}`, `${row.logicalCalls} / ${row.httpAttempts} / ${row.retryAttempts}`,
      `${usageNumber(row.tokens?.inputTokens)} / ${usageNumber(row.tokens?.outputTokens)}`, usageMoney(row.cost),
      `${usageNumber(row.latency.p50Ms)} / ${usageNumber(row.latency.p95Ms)} ms`
    ]);
    if (!summary.byStage.length) usageRow(usageEls.usageStages, ['暂无真实模型调用', '—', '—', '—', '—']);
    usageEls.usageRuns.replaceChildren();
    for (const row of runs) {
      const button = document.createElement('button'); button.className = 'ghost';
      button.textContent = `${displayTime(row.createdAt)} · ${row.runId.slice(0, 10)}`;
      button.addEventListener('click', async () => {
        usageEls.usageDetail.open = true; usageEls.usageDetailBody.textContent = '读取中…';
        try { const response = await api(`/api/admin/model-usage/${encodeURIComponent(row.runId)}`); usageEls.usageDetailBody.textContent = JSON.stringify(await response.json(), null, 2); }
        catch (error) { usageEls.usageDetailBody.textContent = `读取失败：${error.message}`; }
      });
      usageRow(usageEls.usageRuns, [button, row.finalized ? row.streamStatus : '未完成 / 待核对',
        `${usageNumber(row.tokens?.totalTokens)} / ${usagePercent(row.usageCoverage)}`, usageMoney(row.cost),
        `${usageNumber(row.durationMs)} / ${usageNumber(row.serverFirstTokenMs)} ms`]);
    }
    if (!runs.length) usageRow(usageEls.usageRuns, ['暂无账本记录', '—', '—', '—', '—']);
  } catch (error) { usageEls.usageState.textContent = `用量账本读取失败：${error.message}`; }
}

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function clearAuth() {
  sessionStorage.removeItem(AUTH_KEY);
}

function authHeader() {
  const auth = readAuth();
  if (!auth?.token) {
    window.location.replace("/");
    return "";
  }
  return `Basic ${auth.token}`;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: authHeader() };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    let message=text;
    try { const error=JSON.parse(text);message=typeof error.detail==='string'?error.detail:'请求未能完成。'; } catch (_) {}
    throw new Error(message || `${response.status} ${response.statusText}`);
  }
  return response;
}

function setPill(el, text, tone = "ok") {
  el.textContent = text;
  el.className = `pill ${tone}`;
}

function isAdmin(profile) {
  return profile.roles?.some((role) => role.authority === "ROLE_ADMIN");
}

function canReadSensitive() {
  return state.profile?.roles?.some(role=>role.authority==='ROLE_SENSITIVE_READER');
}

function controlledReadOptions(path) {
  return canReadSensitive()
    ? {path:`${path}?includeSensitive=true`,options:{headers:{'X-Access-Purpose':els.accessPurpose.value}}}
    : {path,options:{}};
}

function displayModel(model) {
  return (model || "").includes("mindbridge-qwen2.5-7b-ft") ? "微调 Qwen2.5-7B" : model;
}

function displayTime(value) {
  return value ? new Date(value).toLocaleString() : "";
}

function roleLabel(role) {
  const value = (role || "").toUpperCase();
  if (value === "USER") return "学生";
  if (value === "ASSISTANT") return "MindBridge";
  if (value === "SYSTEM") return "系统";
  return role || "未知角色";
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health");
    const body = await response.json();
    setPill(els.serviceState, body.status === "UP" ? "服务正常" : `服务 ${body.status}`, body.status === "UP" ? "ok" : "danger");
  } catch {
    setPill(els.serviceState, "服务 DOWN", "danger");
  }
}

async function loadProfile() {
  try {
    const response = await api("/api/profile");
    const profile = await response.json();
    if (!isAdmin(profile)) {
      window.location.replace("/student.html");
      return null;
    }
    state.profile = profile;
    els.controlledRead.hidden=!canReadSensitive();
    els.privacyMode.textContent=canReadSensitive()
      ? '列表只展示必要元数据；查看单条脱敏正文需选择用途，读取会留痕。'
      : '当前账号仅可查看统计、状态与去标识元数据，敏感正文已隐藏。';
    els.activeAccount.textContent = profile.displayName || profile.username;
    return profile;
  } catch {
    clearAuth();
    window.location.replace("/");
    return null;
  }
}

async function loadAgentStatus() {
  const response = await api("/api/agent/status");
  const status = await response.json();
  state.modelName = status.model || "mock";
  if (status.realModelEnabled) {
    setPill(els.modelState, `${status.provider} / ${displayModel(state.modelName)}`, "ok");
  } else {
    setPill(els.modelState, "mock 演示", "warn");
  }
}

async function loadAdminDashboard() {
  const [reportsRes, casesRes, excelRes, alertsRes] = await Promise.all([
    api("/api/admin/reports"),
    api("/api/admin/cases"),
    api("/api/admin/excel-records"),
    api("/api/admin/alerts")
  ]);
  const reports = await reportsRes.json();
  const cases = await casesRes.json();
  const excel = await excelRes.json();
  const alerts = await alertsRes.json();
  els.metricReports.textContent = reports.length;
  els.metricHigh.textContent = reports.filter((item) => item.riskLevel === "HIGH").length;
  els.metricCases.textContent = cases.length;
  els.metricExcel.textContent = excel.length;
  els.metricAlerts.textContent = alerts.length;
  renderCases(cases);
  renderReports(reports);
}

async function loadKnowledgeStatus() {
  try {
    const response = await api("/api/admin/knowledge/status");
    const status = await response.json();
    const vector = status.vectorAvailable ? `向量 ${status.vectorChunks ?? 0}` : "向量不可用";
    els.knowledgeState.textContent = `DB ${status.databaseChunks} 片段 · ${vector}`;
  } catch (error) {
    els.knowledgeState.textContent = `读取失败：${error.message}`;
  }
}

function renderCases(cases) {
  els.cases.innerHTML = "";
  if (!cases.length) {
    els.cases.innerHTML = `<div class="empty small"><strong>暂无个案</strong><p>中高风险报告会自动创建风险个案。</p></div>`;
    return;
  }
  for (const item of cases) {
    const card = document.createElement("article");
    card.className = `case-card risk-${item.riskLevel.toLowerCase()}`;

    const head = document.createElement("div");
    head.className = "case-head";
    const title = document.createElement("strong");
    title.textContent = `个案 #${item.id} · ${item.status}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.updatedAt);
    head.append(title, time);

    const meta = document.createElement("small");
    meta.textContent = `报告 #${item.reportId} · ${item.riskLevel} · 负责人 ${item.owner || "未分配"}`;
    const summary = document.createElement("p");
    summary.textContent = item.summary || "";
    const handoff = document.createElement("pre");
    handoff.textContent = item.handoffSummary || "";

    card.append(head, meta, summary, handoff);
    if(canReadSensitive()) {
      const read=document.createElement('button');read.type='button';read.className='ghost';read.textContent='按所选用途查看交接';
      read.addEventListener('click',async()=>{
        const request=controlledReadOptions(`/api/admin/cases/${item.id}`);
        try {
          const response=await api(request.path,request.options);const detail=await response.json();
          summary.textContent=detail.summary;handoff.textContent=detail.handoffSummary;
        } catch(error) {handoff.textContent=error.message;}
      });card.append(read);
    }
    els.cases.append(card);
  }
}

function renderReports(reports) {
  els.reports.innerHTML = "";
  if (!reports.length) {
    els.reports.innerHTML = `<div class="empty small"><strong>暂无报告</strong><p>学生咨询或风险场景会在这里沉淀记录。</p></div>`;
    return;
  }
  for (const item of reports) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = `report risk-${item.riskLevel.toLowerCase()}`;
    card.dataset.sessionId = item.sessionId;

    const head = document.createElement("div");
    head.className = "report-head";
    const title = document.createElement("strong");
    title.textContent = `${item.displayName} · ${item.riskLevel}`;
    const time = document.createElement("span");
    time.textContent = displayTime(item.createdAt);
    head.append(title, time);

    const summary = document.createElement("p");
    summary.textContent = item.summary;
    const content = document.createElement("small");
    content.textContent = item.content;
    const action = document.createElement("span");
    action.className = "report-action";
    action.textContent = canReadSensitive()?'按所选用途查看会话':'查看会话元数据';

    card.append(head, summary, content, action);
    card.addEventListener("click", () => loadConversation(item.sessionId));
    els.reports.append(card);
  }
}

async function loadConversation(sessionId) {
  if (!sessionId) {
    els.conversationState.textContent = "该报告缺少会话 ID";
    return;
  }
  els.conversationState.textContent = "正在读取...";
  els.conversationDetail.innerHTML = `<div class="empty small"><strong>加载中</strong><p>正在读取历史消息。</p></div>`;
  for (const card of els.reports.querySelectorAll(".report")) {
    card.classList.toggle("active", card.dataset.sessionId === sessionId);
  }
  try {
    const request=controlledReadOptions(`/api/admin/conversations/${encodeURIComponent(sessionId)}`);
    const response = await api(request.path,request.options);
    const conversation = await response.json();
    renderConversation(conversation);
  } catch (error) {
    els.conversationState.textContent = "读取失败";
    els.conversationDetail.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty small";
    const title = document.createElement("strong");
    title.textContent = "无法查看档案";
    const detail = document.createElement("p");
    detail.textContent = error.message;
    empty.append(title, detail);
    els.conversationDetail.append(empty);
  }
}

function renderConversation(conversation) {
  const messages = conversation.messages || [];
  els.conversationState.textContent = `${conversation.title || conversation.sessionId} · ${messages.length} 条消息`;
  els.conversationDetail.innerHTML = "";
  if (!messages.length) {
    els.conversationDetail.innerHTML = `<div class="empty small"><strong>暂无消息</strong><p>这个会话还没有写入消息记录。</p></div>`;
    return;
  }
  for (const message of messages) {
    const role = (message.role || "").toLowerCase();
    const row = document.createElement("article");
    row.className = `conversation-message ${role}`;

    const meta = document.createElement("div");
    meta.className = "conversation-meta";
    const label = document.createElement("strong");
    label.textContent = roleLabel(message.role);
    const time = document.createElement("span");
    time.textContent = displayTime(message.createdAt);
    meta.append(label, time);

    const bubble = document.createElement("div");
    bubble.className = "conversation-bubble";
    bubble.textContent = message.content || "";

    row.append(meta, bubble);
    els.conversationDetail.append(row);
  }
}

async function uploadKnowledgeFile(event) {
  event.preventDefault();
  const file = els.knowledgeFile.files?.[0];
  if (!file) {
    els.knowledgeUploadState.textContent = "请先选择文件";
    return;
  }
  const data = new FormData();
  data.append("file", file);
  els.knowledgeUploadState.textContent = "正在切分入库...";
  try {
    const response = await api("/api/admin/knowledge/file", { method: "POST", body: data });
    const result = await response.json();
    els.knowledgeUploadState.textContent = `${result.source} 已入库 ${result.chunks} 个片段`;
    els.knowledgeFile.value = "";
    loadKnowledgeStatus();
  } catch (error) {
    els.knowledgeUploadState.textContent = `上传失败：${error.message}`;
  }
}

async function runKnowledgeAction(kind) {
  const isBackup = kind === "backup";
  const button = isBackup ? els.backupVector : els.rebuildVector;
  const endpoint = isBackup ? "/api/admin/knowledge/backup" : "/api/admin/knowledge/rebuild-vector";
  const original = button.textContent;
  button.disabled = true;
  els.knowledgeUploadState.textContent = isBackup ? "正在备份向量索引..." : "正在重建向量索引...";
  try {
    const response = await api(endpoint, { method: "POST" });
    const result = await response.json();
    els.knowledgeUploadState.textContent = isBackup
      ? `备份完成：${result.snapshot}`
      : `重建完成：${result.indexedChunks} 个片段`;
    loadKnowledgeStatus();
  } catch (error) {
    els.knowledgeUploadState.textContent = `操作失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function logout() {
  clearAuth();
  window.location.assign("/");
}

els.switchAccount.addEventListener("click", logout);
els.refreshAdmin.addEventListener("click", () => {
  loadModelUsage();
  loadAdminDashboard();
  loadKnowledgeStatus();
});
els.knowledgeUploadForm.addEventListener("submit", uploadKnowledgeFile);
els.rebuildVector.addEventListener("click", () => runKnowledgeAction("rebuild"));
els.backupVector.addEventListener("click", () => runKnowledgeAction("backup"));

checkHealth();
usageEls.usageHours.addEventListener('change', loadModelUsage);
loadProfile().then((profile) => {
  if (!profile) return;
  loadAgentStatus();
  loadModelUsage();
  loadAdminDashboard();
  loadKnowledgeStatus();
});
