// 兜底画幅与档位；正式数据由 /api/health 与 /api/models 提供。
const FALLBACK_RATIOS = ["auto", "1:1", "4:3", "3:4", "16:9", "9:16", "custom"];
const FALLBACK_K_LEVELS = ["1K", "1.5K", "2K"];

const els = {
  statusChip: document.getElementById("statusChip"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  promptBox: document.getElementById("promptBox"),
  clearPromptBtn: document.getElementById("clearPromptBtn"),
  referenceInput: document.getElementById("referenceInput"),
  referencePreview: document.getElementById("referencePreview"),
  clearReferenceBtn: document.getElementById("clearReferenceBtn"),
  modelSelect: document.getElementById("modelSelect"),
  refreshModelsBtn: document.getElementById("refreshModelsBtn"),
  modelHint: document.getElementById("modelHint"),
  ratioSelect: document.getElementById("ratioSelect"),
  customRatioField: document.getElementById("customRatioField"),
  customRatioInput: document.getElementById("customRatioInput"),
  kSelect: document.getElementById("kSelect"),
  presetSizeField: document.getElementById("presetSizeField"),
  sizeSelect: document.getElementById("sizeSelect"),
  qualitySelect: document.getElementById("qualitySelect"),
  countInput: document.getElementById("countInput"),
  apiKeyInput: document.getElementById("apiKeyInput"),
  keyToggle: document.getElementById("keyToggle"),
  baseUrlInput: document.getElementById("baseUrlInput"),
  generateBtn: document.getElementById("generateBtn"),
  generateLabel: document.getElementById("generateLabel"),
  proxyHint: document.getElementById("proxyHint"),
  outputCount: document.getElementById("outputCount"),
  resultState: document.getElementById("resultState"),
  loading: document.getElementById("loading"),
  loadingText: document.getElementById("loadingText"),
  imageGrid: document.getElementById("imageGrid"),
  historyPanel: document.getElementById("historyPanel"),
  historyGrid: document.getElementById("historyGrid"),
  historyCount: document.getElementById("historyCount"),
  clearHistoryBtn: document.getElementById("clearHistoryBtn"),
  lightbox: document.getElementById("lightbox"),
  lightboxImg: document.getElementById("lightboxImg"),
  lightboxClose: document.getElementById("lightboxClose"),
};

let busy = false;
let validating = false;
let modelRequestId = 0;
let modelItems = [];
let activeModel = null;
// 提供商地址与 API key 只在界面填写、只存在本机浏览器 localStorage，
// 后端不保存任何配置，因此“是否已配置”完全由这两个输入框决定。
let referenceFiles = [];

function fillSelect(select, options, labeler) {
  select.innerHTML = "";
  for (const option of options || []) {
    const value = typeof option === "object" ? option.value : option;
    const label = typeof option === "object" ? option.label : (labeler ? labeler(value) : value);
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = label;
    select.appendChild(opt);
  }
}

function ratioLabel(value) {
  if (value === "auto") return "auto · 自动";
  if (value === "custom") return "custom · 自定义比例";
  return value;
}

function qualityLabel(value) {
  const labels = {
    auto: "auto · 自动",
    low: "low · 快速",
    medium: "medium · 中等",
    high: "high · 高清",
    xhigh: "xhigh · 超高",
    max: "max · 最高",
    standard: "standard · 标准",
    hd: "hd · 高清",
  };
  return labels[value] || value;
}

function populateSizeControls(ratios, kLevels) {
  fillSelect(els.ratioSelect, ratios && ratios.length ? ratios : FALLBACK_RATIOS, ratioLabel);
  fillSelect(els.kSelect, kLevels && kLevels.length ? kLevels : FALLBACK_K_LEVELS);
}

function syncCustomRatioControl() {
  const enabled = activeModel
    && activeModel.capabilities.size_mode === "ratio_and_k"
    && activeModel.capabilities.ratios.includes("custom")
    && els.ratioSelect.value === "custom";
  els.customRatioField.hidden = !enabled;
  els.customRatioInput.disabled = !enabled;
}

function fillModelSelect(groups, items) {
  els.modelSelect.innerHTML = "";
  const byGroup = new Map();
  for (const item of items) {
    if (!byGroup.has(item.group)) byGroup.set(item.group, []);
    byGroup.get(item.group).push(item);
  }
  for (const group of groups) {
    const options = byGroup.get(group.key) || [];
    if (!options.length) continue;
    const optgroup = document.createElement("optgroup");
    optgroup.label = group.label;
    for (const item of options) {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.id;
      opt.title = item.label && item.label !== item.id ? item.label : item.id;
      optgroup.appendChild(opt);
    }
    els.modelSelect.appendChild(optgroup);
  }
  els.modelSelect.disabled = !items.length;
  els.refreshModelsBtn.disabled = !isReady();
}

function currentModelItem() {
  const id = els.modelSelect.value;
  return modelItems.find((item) => item.id === id) || null;
}

function isReferenceSupported() {
  if (!activeModel) return false;
  return activeModel.capabilities.edits !== false;
}

function updateModelHint() {
  if (!activeModel) {
    els.modelHint.textContent = isReady()
      ? "正在获取可用生图模型。"
      : "填写 API Key 与 Base URL 后自动获取可用生图模型。";
    return;
  }
  const caps = activeModel.capabilities;
  const parts = [];
  if (caps.edits === "unknown") {
    parts.push("未识别该模型能力，参考图可能不被支持。");
  } else if (caps.edits === false) {
    parts.push("该模型不支持参考图。");
  }
  if (!caps.qualities.length) {
    parts.push("该模型不发送质量参数。");
  }
  if (!parts.length) {
    parts.push("模型能力已识别，可直接开始生成。");
  }
  els.modelHint.textContent = parts.join(" ");
}

function syncReferenceControl() {
  const supported = isReferenceSupported();
  els.referenceInput.disabled = !supported;
  const maxReferences = activeModel ? (activeModel.capabilities.max_references || 0) : 0;
  els.referenceInput.multiple = maxReferences !== 1;
  if ((!supported || (maxReferences && referenceFiles.length > maxReferences)) && referenceFiles.length) {
    clearReference();
  }
}

function applyModelCapabilities() {
  activeModel = currentModelItem();
  const caps = activeModel ? activeModel.capabilities : null;
  if (!caps) {
    els.ratioSelect.disabled = true;
    els.customRatioField.hidden = true;
    els.customRatioInput.disabled = true;
    els.kSelect.disabled = true;
    els.sizeSelect.disabled = true;
    els.qualitySelect.disabled = true;
    els.presetSizeField.hidden = true;
    syncReferenceControl();
    updateModelHint();
    setGenerateEnabled(Boolean(els.promptBox.value.trim()));
    return;
  }

  const isPreset = caps.size_mode === "preset";
  els.presetSizeField.hidden = !isPreset;
  els.ratioSelect.disabled = isPreset || caps.size_mode === "none";
  els.customRatioField.hidden = true;
  els.customRatioInput.disabled = true;
  els.kSelect.disabled = isPreset || caps.size_mode === "none";
  els.sizeSelect.disabled = !isPreset;
  els.qualitySelect.disabled = !caps.qualities.length;

  if (caps.size_mode === "ratio_and_k") {
    populateSizeControls(caps.ratios, caps.k_levels);
    if (!caps.ratios.includes(els.ratioSelect.value)) {
      els.ratioSelect.value = caps.ratios[0] || "";
    }
    if (!caps.k_levels.includes(els.kSelect.value)) {
      els.kSelect.value = caps.k_levels[0] || "";
    }
    els.kSelect.disabled = els.ratioSelect.value === "auto";
    syncCustomRatioControl();
  } else if (isPreset) {
    fillSelect(els.sizeSelect, caps.preset_sizes);
  }

  fillSelect(els.qualitySelect, caps.qualities, qualityLabel);
  const count = Math.max(1, Math.min(caps.max_n || 1, parseInt(els.countInput.value, 10) || 1));
  els.countInput.value = count;
  els.countInput.max = caps.max_n || 1;

  syncReferenceControl();
  updateModelHint();
  setGenerateEnabled(Boolean(els.promptBox.value.trim()));
}

function readRatio() {
  return els.ratioSelect.value;
}

function readCustomRatio() {
  return els.customRatioInput.value.trim();
}

function readK() {
  return els.kSelect.value;
}

function readApiKey() {
  return els.apiKeyInput.value.trim();
}

function readBaseUrl() {
  return els.baseUrlInput.value.trim();
}

function readModel() {
  return els.modelSelect.value;
}

function isReady() {
  return Boolean(readApiKey() && readBaseUrl());
}

// 校验中/生成中不要覆盖状态灯文案，只在空闲时刷新配置提示。
function refreshStatus() {
  if (busy || validating) return;
  if (isReady() && activeModel) setStatus("ok", `模型可用 (${activeModel.id})`);
  else if (isReady()) setStatus("", "正在获取生图模型…");
  else setStatus("warn", "待配置：请填写 API Key 与提供商地址");
}

function loadBaseUrl() {
  return localStorage.getItem("img2_base_url") || "";
}

function saveBaseUrl(value) {
  const trimmed = (value || "").trim();
  if (trimmed) localStorage.setItem("img2_base_url", trimmed);
  else localStorage.removeItem("img2_base_url");
}

function loadApiKey() {
  return localStorage.getItem("img2_api_key") || "";
}

function saveApiKey(value) {
  const trimmed = (value || "").trim();
  if (trimmed) localStorage.setItem("img2_api_key", trimmed);
  else localStorage.removeItem("img2_api_key");
}

function loadModel() {
  return localStorage.getItem("img2_model") || "";
}

function saveModel(value) {
  const trimmed = (value || "").trim();
  if (trimmed) localStorage.setItem("img2_model", trimmed);
  else localStorage.removeItem("img2_model");
}

function invalidateModels() {
  modelRequestId += 1;
  modelItems = [];
  activeModel = null;
  fillModelSelect([], []);
  applyModelCapabilities();
}

function setStatus(kind, text) {
  els.statusChip.className = "status-chip";
  if (kind) els.statusChip.classList.add(kind);
  els.statusText.textContent = text;
}

const ICON_DOWNLOAD = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M7 11l5 5 5-5M5 21h14"></path></svg>`;
const ICON_ZOOM = `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"></path></svg>`;

async function checkHealth() {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    populateSizeControls(data.ratios, data.k_levels);
    refreshStatus();
  } catch {
    setStatus("err", "后端未启动");
  }
}

async function checkModels() {
  if (!isReady()) {
    modelRequestId += 1;
    validating = false;
    modelItems = [];
    fillModelSelect([], []);
    applyModelCapabilities();
    refreshStatus();
    return;
  }
  const requestId = ++modelRequestId;
  validating = true;
  els.refreshModelsBtn.disabled = true;
  setStatus("warn", "正在获取生图模型…");
  const qs = `?base_url=${encodeURIComponent(readBaseUrl())}`;
  const headers = { "X-Api-Key": readApiKey() };
  try {
    const res = await fetch(`/api/models${qs}`, { headers });
    const data = await res.json();
    if (requestId !== modelRequestId) return;
    if (!data.ok) throw new Error(data.error || "模型获取失败");

    modelItems = data.items || [];
    fillModelSelect(data.groups || [], modelItems);
    if (!modelItems.length) {
      saveModel("");
      applyModelCapabilities();
      setStatus("warn", "未发现可用的生图模型");
      return;
    }

    const remembered = loadModel();
    const candidate = modelItems.find((item) => item.id === remembered)
      || modelItems.find((item) => item.id === data.default_model)
      || modelItems[0];
    els.modelSelect.value = candidate.id;
    saveModel(candidate.id);
    applyModelCapabilities();
    setStatus("ok", `模型可用 (${candidate.id})`);
  } catch (err) {
    if (requestId !== modelRequestId) return;
    modelItems = [];
    fillModelSelect([], []);
    applyModelCapabilities();
    setStatus("err", err.message || "模型获取失败");
  } finally {
    if (requestId === modelRequestId) {
      validating = false;
      els.refreshModelsBtn.disabled = !isReady();
    }
  }
}

function setGenerateEnabled(enabled) {
  const validModel = Boolean(activeModel);
  els.generateBtn.disabled = !enabled || !validModel || busy;
}

function setBusy(value, label) {
  busy = value;
  if (busy) els.generateBtn.disabled = true;
  else setGenerateEnabled(Boolean(els.promptBox.value.trim()));
  els.generateLabel.textContent = value ? label : "开始生成";
}

function showResultState() {
  els.resultState.hidden = false;
}

function hideResultState() {
  els.resultState.hidden = true;
}

function clearGrid() {
  els.imageGrid.innerHTML = "";
  els.outputCount.textContent = "";
}

function addCard(url, meta) {
  const card = document.createElement("div");
  card.className = "card";

  const img = document.createElement("img");
  img.src = url;
  img.alt = meta.prompt || "生成图片";
  img.loading = "lazy";
  img.addEventListener("click", () => openLightbox(url));

  const foot = document.createElement("div");
  foot.className = "card-foot";

  const metaEl = document.createElement("span");
  metaEl.className = "card-meta";
  metaEl.textContent = [
    meta.model || "未知模型",
    meta.ratio || meta.size || "auto",
    meta.k || "",
    meta.quality || "",
  ].filter(Boolean).join(" · ");

  const dl = document.createElement("button");
  dl.className = "icon-btn";
  dl.title = "下载图片";
  dl.setAttribute("aria-label", "下载图片");
  dl.innerHTML = ICON_DOWNLOAD;
  dl.addEventListener("click", () => download(meta.filename));

  foot.appendChild(metaEl);
  foot.appendChild(dl);
  card.appendChild(img);
  card.appendChild(foot);
  els.imageGrid.appendChild(card);
}

function formatHistoryTime(value) {
  // 记录形如 "YYYY-MM-DD HH:MM:SS"，只展示到分钟更耐看。
  if (!value) return "";
  const m = String(value).match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : String(value);
}

function formatHistoryMeta(item) {
  const ratio = item.ratio || item.size || "auto";
  const ratioText = ratio === "auto" ? "自动" : ratio;
  const k = item.k && item.k !== "auto" ? item.k : "";
  const quality = item.quality;
  const qualityText = quality === "auto" ? "自动" : quality;
  return [item.model || "未知模型", ratioText, k, qualityText].filter(Boolean).join(" · ");
}

function buildHistoryCard(item) {
  const card = document.createElement("article");
  card.className = "history-card";

  const imgWrap = document.createElement("div");
  imgWrap.className = "history-img-wrap";
  const img = document.createElement("img");
  img.className = "history-img";
  img.src = item.url;
  img.alt = item.prompt || "历史图片";
  img.loading = "lazy";
  img.addEventListener("click", () => openLightbox(item.url));
  imgWrap.appendChild(img);

  const zoom = document.createElement("button");
  zoom.className = "history-zoom";
  zoom.type = "button";
  zoom.title = "放大查看";
  zoom.setAttribute("aria-label", "放大查看");
  zoom.innerHTML = ICON_ZOOM;
  zoom.addEventListener("click", () => openLightbox(item.url));
  imgWrap.appendChild(zoom);

  const info = document.createElement("div");
  info.className = "history-info";

  const promptEl = document.createElement("p");
  promptEl.className = "history-prompt";
  promptEl.textContent = item.prompt || "（无提示词）";
  promptEl.title = item.prompt || "";
  info.appendChild(promptEl);

  const metaEl = document.createElement("div");
  metaEl.className = "history-meta";
  const meta = document.createElement("span");
  meta.className = "history-meta-text";
  meta.textContent = formatHistoryMeta(item);
  metaEl.appendChild(meta);

  if (item.reference) {
    const badge = document.createElement("span");
    badge.className = "history-badge";
    badge.textContent = "参考图";
    badge.title = "本次生图使用了参考图";
    metaEl.appendChild(badge);
  }

  const time = document.createElement("span");
  time.className = "history-time";
  time.textContent = formatHistoryTime(item.created_at);
  metaEl.appendChild(time);
  info.appendChild(metaEl);

  const actions = document.createElement("div");
  actions.className = "history-actions";
  const dl = document.createElement("button");
  dl.className = "icon-btn";
  dl.type = "button";
  dl.title = "下载图片";
  dl.setAttribute("aria-label", "下载图片");
  dl.innerHTML = ICON_DOWNLOAD;
  dl.addEventListener("click", () => download(item.filename));
  actions.appendChild(dl);
  info.appendChild(actions);

  card.appendChild(imgWrap);
  card.appendChild(info);
  return card;
}

function renderHistoryItems(items) {
  els.historyGrid.innerHTML = "";
  if (!items || !items.length) {
    els.historyPanel.hidden = true;
    els.historyCount.textContent = "";
    return;
  }
  for (const item of items) {
    els.historyGrid.appendChild(buildHistoryCard(item));
  }
  els.historyCount.textContent = `· ${items.length}`;
  els.historyPanel.hidden = false;
}

async function loadHistory() {
  try {
    const res = await fetch("/api/history");
    const data = await res.json();
    renderHistoryItems(data.items || []);
  } catch {
    els.historyPanel.hidden = true;
  }
}

function updateReferencePreview() {
  if (!isReferenceSupported()) {
    clearReference();
    return;
  }
  referenceFiles = Array.from(els.referenceInput.files || []);
  els.referencePreview.innerHTML = "";
  els.referencePreview.hidden = referenceFiles.length === 0;
  els.clearReferenceBtn.hidden = referenceFiles.length === 0;
  for (const f of referenceFiles) {
    const img = document.createElement("img");
    img.className = "reference-thumb";
    img.src = URL.createObjectURL(f);
    img.alt = f.name || "参考图";
    img.title = f.name || f.type || "参考图";
    els.referencePreview.appendChild(img);
  }
}

function clearReference() {
  els.referenceInput.value = "";
  referenceFiles = [];
  els.referencePreview.innerHTML = "";
  els.referencePreview.hidden = true;
  els.clearReferenceBtn.hidden = true;
}

function download(filename) {
  const a = document.createElement("a");
  a.href = `/api/download/${encodeURIComponent(filename || "image.png")}`;
  a.download = filename || "image.png";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function openLightbox(url) {
  els.lightboxImg.src = url;
  els.lightbox.hidden = false;
}

function closeLightbox() {
  els.lightbox.hidden = true;
  els.lightboxImg.src = "";
}

function showError(message) {
  const existing = els.imageGrid.previousElementSibling;
  if (existing && existing.classList.contains("error")) existing.remove();
  const box = document.createElement("div");
  box.className = "error";
  box.textContent = message;
  els.imageGrid.parentNode.insertBefore(box, els.imageGrid);
}

function clearError() {
  const existing = els.imageGrid.previousElementSibling;
  if (existing && existing.classList.contains("error")) existing.remove();
}

async function generate() {
  const prompt = els.promptBox.value.trim();
  const baseUrl = readBaseUrl();
  const apiKey = readApiKey();
  if (!prompt) {
    els.promptBox.focus();
    showError("请先输入提示词。");
    return;
  }
  // 程序不内置中转站，缺任何一项都无法调用，先在前端拦下并聚焦到对应输入框。
  if (!apiKey) {
    els.apiKeyInput.focus();
    showError("请先填写 API Key。");
    return;
  }
  if (!baseUrl) {
    els.baseUrlInput.focus();
    showError("请先填写提供商地址（Base URL），例如 https://你的中转站地址/v1。");
    return;
  }
  if (!activeModel) {
    showError("请先获取并选择可用的生图模型。");
    return;
  }

  clearError();
  hideResultState();
  clearGrid();
  setBusy(true, "正在生成…");
  els.loading.hidden = false;
  els.loadingText.textContent = "正在生成，通常需要 20 秒~2 分钟，请耐心等待…";

  const hasRef = referenceFiles.length > 0;

  let headers;
  let body;
  if (hasRef) {
    const fd = new FormData();
    fd.append("prompt", prompt);
    fd.append("model", readModel());
    if (activeModel.capabilities.size_mode === "preset") {
      fd.append("size", els.sizeSelect.value);
    } else {
      fd.append("ratio", readRatio());
      fd.append("k", readK());
      if (readRatio() === "custom") fd.append("custom_ratio", readCustomRatio());
    }
    if (activeModel.capabilities.qualities.length) {
      fd.append("quality", els.qualitySelect.value);
    }
    fd.append("n", String(parseInt(els.countInput.value, 10) || 1));
    if (baseUrl) fd.append("base_url", baseUrl);
    if (apiKey) fd.append("api_key", apiKey);
    for (const f of referenceFiles) fd.append("image", f);
    body = fd;
  } else {
    const payload = {
      model: readModel(),
      prompt,
      n: parseInt(els.countInput.value, 10) || 1,
    };
    if (activeModel.capabilities.size_mode === "preset") {
      payload.size = els.sizeSelect.value;
    } else {
      payload.ratio = readRatio();
      payload.k = readK();
      if (readRatio() === "custom") payload.custom_ratio = readCustomRatio();
    }
    if (activeModel.capabilities.qualities.length) {
      payload.quality = els.qualitySelect.value;
    }
    if (baseUrl) payload.base_url = baseUrl;
    if (apiKey) payload.api_key = apiKey;
    headers = { "Content-Type": "application/json" };
    body = JSON.stringify(payload);
  }

  try {
    const res = await fetch("/api/generate", {
      method: "POST",
      headers,
      body,
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || `生成失败（HTTP ${res.status}）`);
    }

    els.outputCount.textContent = `本次 ${data.count} 张`;
    for (const img of data.images) {
      addCard(img.url, { filename: img.filename, model: data.meta.model, ratio: data.meta.ratio, k: data.meta.k, size: img.size, quality: img.quality, prompt: data.meta.prompt });
    }
    clearReference();
    loadHistory();
  } catch (err) {
    showResultState();
    showError(err.message || "生成失败，请稍后重试。");
  } finally {
    els.loading.hidden = true;
    setBusy(false, "再次生成");
  }
}

function bindEvents() {
  els.generateBtn.addEventListener("click", generate);
  els.modelSelect.addEventListener("change", () => {
    saveModel(readModel());
    applyModelCapabilities();
    refreshStatus();
  });
  els.refreshModelsBtn.addEventListener("click", checkModels);
  els.clearPromptBtn.addEventListener("click", () => {
    els.promptBox.value = "";
    els.promptBox.focus();
  });

  for (const btn of document.querySelectorAll(".step-btn")) {
    btn.addEventListener("click", () => {
      const delta = parseInt(btn.dataset.step, 10);
      let v = parseInt(els.countInput.value, 10) || 1;
      const max = parseInt(els.countInput.max, 10) || 1;
      v = Math.max(1, Math.min(max, v + delta));
      els.countInput.value = v;
    });
  }

  els.countInput.addEventListener("change", () => {
    let v = parseInt(els.countInput.value, 10) || 1;
    const max = parseInt(els.countInput.max, 10) || 1;
    v = Math.max(1, Math.min(max, v));
    els.countInput.value = v;
  });

  // 画幅 / 分辨率切换即保存并重新校验模型。
  els.ratioSelect.addEventListener("change", () => {
    els.kSelect.disabled = els.ratioSelect.value === "auto";
    syncCustomRatioControl();
  });

  els.referenceInput.addEventListener("change", updateReferencePreview);
  els.clearReferenceBtn.addEventListener("click", clearReference);

  // API Key：显示/隐藏、本地保存
  els.keyToggle.addEventListener("click", () => {
    const show = els.apiKeyInput.type === "password";
    els.apiKeyInput.type = show ? "text" : "password";
    els.keyToggle.setAttribute("aria-label", show ? "隐藏 API Key" : "显示/隐藏 API Key");
  });
  els.apiKeyInput.addEventListener("input", () => {
    saveApiKey(els.apiKeyInput.value);
    invalidateModels();
    refreshStatus();
  });
  els.apiKeyInput.addEventListener("change", () => {
    checkModels();
  });
  els.apiKeyInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (isReady()) checkModels();
      else refreshStatus();
    }
  });

  // API Base URL：本地保存 + 变更时重新校验模型
  els.baseUrlInput.addEventListener("input", () => {
    saveBaseUrl(els.baseUrlInput.value);
    invalidateModels();
    refreshStatus();
  });
  els.baseUrlInput.addEventListener("change", () => {
    checkModels();
  });
  els.baseUrlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (isReady()) checkModels();
      else refreshStatus();
    }
  });

  els.lightboxClose.addEventListener("click", closeLightbox);
  els.lightbox.addEventListener("click", (e) => {
    if (e.target === els.lightbox) closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeLightbox();
  });

  els.clearHistoryBtn.addEventListener("click", async () => {
    if (!window.confirm("确定清空全部历史记录？此操作只会移除列表记录，不会删除 outputs/generated 中已生成的图片文件。")) return;
    try {
      const res = await fetch("/api/history", { method: "DELETE" });
      if (res.ok) {
        els.historyCount.textContent = "";
      }
    } catch {}
    loadHistory();
  });

  els.promptBox.addEventListener("input", () => {
    setGenerateEnabled(Boolean(els.promptBox.value.trim()));
  });

  els.promptBox.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") generate();
  });
}

function init() {
  populateSizeControls();
  els.apiKeyInput.value = loadApiKey();
  els.baseUrlInput.value = loadBaseUrl();
  bindEvents();
  applyModelCapabilities();
  loadHistory();
  checkHealth().then(() => {
    checkModels();
  });
}

init();
