/* 星穹铁道面板 —— 控制面板
 * 所有请求都通过 AstrBot 的 plugin view bridge 转发，不能直接 fetch。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

function show(el, text, kind) {
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

function cell(text, cls) {
  const td = document.createElement("td");
  td.textContent = text == null ? "" : String(text);
  if (cls) td.className = cls;
  return td;
}

function fmtTime(ts) {
  if (!ts) return "未更新";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(
    d.getHours()
  )}:${pad(d.getMinutes())}`;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = String(text);
  return node;
}

async function get(endpoint, params) {
  if (!bridge) throw new Error("未检测到插件桥接，请从 AstrBot WebUI 的插件详情页打开");
  return await bridge.apiGet(endpoint, params || {});
}

async function post(endpoint, body) {
  if (!bridge) throw new Error("未检测到插件桥接，请从 AstrBot WebUI 的插件详情页打开");
  return await bridge.apiPost(endpoint, body || {});
}

/** 兼容后端返回 {bindings:x} 或 {status:"ok", data:{bindings:x}} 两种情况 */
function unwrap(resp, key) {
  if (!resp) return undefined;
  if (resp[key] !== undefined) return resp[key];
  if (resp.data && resp.data[key] !== undefined) return resp.data[key];
  return undefined;
}

/* ============================ 标签页 ============================ */

function switchTab(name) {
  for (const btn of document.querySelectorAll(".tab")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.classList.toggle("active", panel.id === `tab-${name}`);
  }
  if (name === "players") loadPanels();
  if (name === "settings") loadSettings();
  if (name === "diagnose" && !$("checks").dataset.loaded) runDiagnose();
}

for (const btn of document.querySelectorAll(".tab")) {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
}

/* ============================ 绑定管理 ============================ */

const tbody = $("tbody");
const totalBadge = $("total");
const addMsg = $("add-msg");
const tableMsg = $("table-msg");

function renderRows(list) {
  tbody.innerHTML = "";

  if (!list.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "empty";
    td.textContent = "还没有绑定记录，在上面添加一条吧";
    tr.appendChild(td);
    tbody.appendChild(tr);
    totalBadge.textContent = "0";
    return;
  }

  for (const item of list) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(item.user_id));
    tr.appendChild(cell(item.uid));
    tr.appendChild(cell(item.nickname || "—"));
    tr.appendChild(cell(fmtTime(item.updated_at)));

    const td = document.createElement("td");
    td.className = "acts";

    const btnView = el("button", "", "数据");
    btnView.addEventListener("click", () => {
      switchTab("players");
      openDetail(item.uid);
    });

    const btnRefresh = el("button", "", "刷新");
    btnRefresh.addEventListener("click", () => refreshOne(item, btnRefresh));

    const btnDel = el("button", "danger", "删除");
    btnDel.addEventListener("click", () => removeBinding(item, btnDel));

    td.append(btnView, btnRefresh, btnDel);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  totalBadge.textContent = String(list.length);
}

async function loadList() {
  show(tableMsg, "加载中…");
  try {
    const resp = await get("bindings");
    renderRows(unwrap(resp, "bindings") || []);
    show(tableMsg, "");
  } catch (err) {
    show(tableMsg, "加载失败：" + err.message, "err");
  }
}

async function refreshOne(item, btn) {
  btn.disabled = true;
  btn.textContent = "刷新中";
  show(tableMsg, `正在刷新 UID ${item.uid}，可能需要十几秒…`);
  try {
    const resp = await post("refresh", { uid: item.uid });
    const nickname = unwrap(resp, "nickname") || "";
    const chars = unwrap(resp, "characters");
    show(
      tableMsg,
      `UID ${item.uid} 刷新完成${nickname ? "（" + nickname + "）" : ""}${
        chars != null ? "，共 " + chars + " 个角色" : ""
      }`,
      "ok"
    );
    await loadList();
  } catch (err) {
    show(
      tableMsg,
      "刷新失败：" + err.message + "（原有本地数据不会被覆盖）",
      "err"
    );
  } finally {
    btn.disabled = false;
    btn.textContent = "刷新";
  }
}

async function removeBinding(item, btn) {
  // 插件视图运行在受限 iframe 里，window.confirm 会被拦截，用点两次确认代替
  if (btn.dataset.confirm !== "1") {
    btn.dataset.confirm = "1";
    btn.textContent = "确认删除?";
    setTimeout(() => {
      btn.dataset.confirm = "";
      btn.textContent = "删除";
    }, 4000);
    return;
  }

  btn.disabled = true;
  try {
    await post("bindings/delete", { user_id: item.user_id });
    show(tableMsg, `已解除 ${item.user_id} 与 UID ${item.uid} 的绑定`, "ok");
    await loadList();
  } catch (err) {
    show(tableMsg, "删除失败：" + err.message, "err");
    btn.disabled = false;
    btn.dataset.confirm = "";
    btn.textContent = "删除";
  }
}

async function refreshAll(btn) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "已提交";
  try {
    const resp = await post("refresh_all", {});
    const msg = unwrap(resp, "message") || "已开始后台刷新";
    show(tableMsg, msg + "，稍后点「重新加载」查看结果", "ok");
  } catch (err) {
    show(tableMsg, "提交失败：" + err.message, "err");
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = old;
    }, 3000);
  }
}

/* ============================ 玩家数据 ============================ */

const panelsTbody = $("panels-tbody");
const panelsMsg = $("panels-msg");
const detailCard = $("detail-card");

const detail = {
  uid: "",
  nickname: "",
  name: "",
  data: null,
};

async function loadPanels() {
  show(panelsMsg, "加载中…");
  try {
    const resp = await get("panels");
    const list = unwrap(resp, "panels") || [];
    renderPanels(list);

    const at = unwrap(resp, "last_update_at");
    const note = unwrap(resp, "last_update_msg");
    const bits = [];
    if (at) bits.push(`最近一次更新：${fmtTime(at)}`);
    if (note) bits.push(note);
    $("last-update").textContent = bits.join(" · ");
    show(panelsMsg, list.length ? "" : "还没有任何本地存档，先在绑定页刷新一次");
  } catch (err) {
    show(panelsMsg, "加载失败：" + err.message, "err");
  }
}

function renderPanels(list) {
  panelsTbody.innerHTML = "";
  $("panel-total").textContent = String(list.length);

  if (!list.length) {
    const tr = document.createElement("tr");
    const td = el("td", "empty", "还没有存档，点绑定页的「刷新」拉取数据");
    td.colSpan = 5;
    tr.appendChild(td);
    panelsTbody.appendChild(tr);
    return;
  }

  for (const item of list) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(item.uid));
    tr.appendChild(cell(item.nickname || "—"));
    tr.appendChild(cell(item.characters != null ? item.characters : "—"));
    tr.appendChild(cell(fmtTime(item.updated_at)));

    const td = document.createElement("td");
    td.className = "acts";

    const btnView = el("button", "", "查看");
    btnView.addEventListener("click", () => openDetail(item.uid));

    const btnRefresh = el("button", "", "刷新");
    btnRefresh.addEventListener("click", () =>
      refreshPanel(item.uid, btnRefresh)
    );

    td.append(btnView, btnRefresh);
    tr.appendChild(td);
    panelsTbody.appendChild(tr);
  }
}

async function refreshPanel(uid, btn) {
  btn.disabled = true;
  btn.textContent = "刷新中";
  show(panelsMsg, `正在刷新 UID ${uid}…`);
  try {
    const resp = await post("refresh", { uid });
    const nickname = unwrap(resp, "nickname") || "";
    const chars = unwrap(resp, "characters");
    show(
      panelsMsg,
      `UID ${uid} 刷新完成${nickname ? "（" + nickname + "）" : ""}${
        chars != null ? "，共 " + chars + " 个角色" : ""
      }`,
      "ok"
    );
    await loadPanels();
    if (detail.uid === uid) await openDetail(uid);
  } catch (err) {
    show(panelsMsg, "刷新失败：" + err.message + "（旧数据保留）", "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "刷新";
  }
}

async function openDetail(uid, name) {
  detail.uid = uid;
  detail.data = null;
  detail.name = "";
  detailCard.hidden = false;
  $("detail-title").textContent = `UID ${uid}`;
  $("char-list").innerHTML = "";
  $("preview-text").hidden = true;
  $("preview-frame").hidden = true;
  $("preview-img-wrap").hidden = true;
  $("preview-actions").hidden = true;
  $("preview-tip").hidden = false;
  $("preview-tip").textContent = "加载中…";

  try {
    const resp = await post("panel", { uid });
    const data = unwrap(resp, "characters") !== undefined ? resp : resp.data || {};
    const chars =
      (data.data && data.data.characters) ||
      data.characters ||
      unwrap(resp, "characters") ||
      [];
    detail.data = { ...data, characters: chars };
    detail.nickname = (data.data && data.data.nickname) || data.nickname || "";

    $("detail-title").textContent = `UID ${uid}${
      detail.nickname ? " · " + detail.nickname : ""
    }`;

    renderCharList(chars);
    $("preview-tip").textContent = chars.length
      ? "选择左侧角色查看角色卡"
      : "这个玩家没有角色数据";

    const target = name || (chars[0] && chars[0].name);
    if (target) await loadCard(target);
  } catch (err) {
    $("preview-tip").textContent = "读取失败：" + err.message;
  }
}

function renderCharList(chars) {
  const box = $("char-list");
  box.innerHTML = "";

  chars.forEach((c, i) => {
    const btn = el("button", "char-item");
    const title = el("span", "char-name", c.name || `角色 ${i + 1}`);
    const meta = el(
      "span",
      "char-meta",
      `Lv${c.level || "-"} · ${c.element || ""} · ${c.weapon_name || "无光锥"}`
    );
    btn.append(title, meta);
    btn.addEventListener("click", () => {
      for (const other of box.querySelectorAll(".char-item")) {
        other.classList.remove("active");
      }
      btn.classList.add("active");
      loadCard(c.name || String(i + 1));
    });
    box.appendChild(btn);
  });
}

async function loadCard(name) {
  if (!detail.uid) return;
  detail.name = name;
  $("preview-tip").hidden = false;
  $("preview-tip").textContent = "生成角色卡中…（首次可能要几秒）";
  $("preview-img-wrap").hidden = true;
  $("preview-frame").hidden = true;
  $("preview-text").hidden = true;

  try {
    const resp = await post("card", { uid: detail.uid, name });
    const payload = unwrap(resp, "html") !== undefined ? resp : resp.data || {};
    detail.card = payload;

    $("preview-actions").hidden = false;
    if (payload.image) {
      showCardImage(payload.image);
    } else if (payload.html) {
      showCardHtml(payload.html);
    } else {
      $("preview-tip").textContent = "没有生成角色卡，请查看自检页";
      $("preview-actions").hidden = true;
    }
  } catch (err) {
    $("preview-tip").textContent = "生成失败：" + err.message;
  }
}

function showCardImage(src) {
  $("preview-tip").hidden = true;
  $("preview-frame").hidden = true;
  $("preview-text").hidden = true;
  $("preview-img").src = src;
  $("preview-img-wrap").hidden = false;
}

function showCardHtml(html) {
  $("preview-tip").hidden = true;
  $("preview-img-wrap").hidden = true;
  $("preview-text").hidden = true;
  const frame = $("preview-frame");
  frame.srcdoc = html;
  frame.hidden = false;
}

function showCardText() {
  $("preview-tip").hidden = true;
  $("preview-img-wrap").hidden = true;
  $("preview-frame").hidden = true;
  $("preview-text").textContent =
    (detail.card && detail.card.text) || "没有文字面板";
  $("preview-text").hidden = false;
}

/* ============================ 设置 ============================ */

const settingsForm = $("settings-form");
const settingsMsg = $("settings-msg");
let settingsSpecs = {};

async function loadSettings() {
  show(settingsMsg, "读取中…");
  try {
    const resp = await get("settings");
    const payload = unwrap(resp, "values") !== undefined ? resp : resp.data || {};
    const values = payload.values || {};
    settingsSpecs = payload.specs || {};

    settingsForm.innerHTML = "";
    for (const [key, spec] of Object.entries(settingsSpecs)) {
      settingsForm.appendChild(buildSettingRow(key, spec, values[key]));
    }

    if (payload.persist_supported === false) {
      show(
        settingsMsg,
        "当前 AstrBot 版本的配置对象不支持保存，改动只在本次运行内生效",
        "warn"
      );
    } else {
      show(settingsMsg, "");
    }
  } catch (err) {
    show(settingsMsg, "读取失败：" + err.message, "err");
  }
}

function buildSettingRow(key, spec, value) {
  const row = el("div", "setting-row");

  const label = el("label", "setting-label");
  label.textContent = spec.label || key;
  label.htmlFor = `set-${key}`;

  const control = el("div", "setting-control");
  let input;

  if (spec.type === "select") {
    input = document.createElement("select");
    for (const option of spec.options || []) {
      const opt = document.createElement("option");
      opt.value = option;
      opt.textContent = option;
      if (option === value) opt.selected = true;
      input.appendChild(opt);
    }
  } else if (spec.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
  } else {
    input = document.createElement("input");
    input.type = "number";
    if (spec.min != null) input.min = spec.min;
    if (spec.max != null) input.max = spec.max;
    input.value = value == null ? "" : value;
  }

  input.id = `set-${key}`;
  input.dataset.key = key;
  input.dataset.type = spec.type || "int";
  control.appendChild(input);

  const hint = el("p", "setting-hint", spec.hint || "");
  row.append(label, control, hint);
  return row;
}

async function saveSettings() {
  const btn = $("btn-settings-save");
  const values = {};

  for (const input of settingsForm.querySelectorAll("[data-key]")) {
    const key = input.dataset.key;
    const type = input.dataset.type;
    if (type === "bool") values[key] = input.checked;
    else if (type === "select") values[key] = input.value;
    else values[key] = input.value === "" ? 0 : Number(input.value);
  }

  btn.disabled = true;
  show(settingsMsg, "保存中…");
  try {
    const resp = await post("settings/save", { values });
    const payload = unwrap(resp, "ok") !== undefined ? resp : resp.data || {};
    if (payload.saved === false) {
      show(
        settingsMsg,
        "设置已生效，但当前 AstrBot 无法写入配置文件，重启后会恢复原值",
        "warn"
      );
    } else {
      show(settingsMsg, "已保存并生效", "ok");
    }
    await loadSettings();
  } catch (err) {
    show(settingsMsg, "保存失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ============================ 自检 ============================ */

const checksBox = $("checks");
const diagInfo = $("diag-info");

const INFO_LABELS = {
  library_version: "解析库版本",
  render_mode: "输出方式",
  data_dir: "数据目录",
  db_path: "数据库",
  cache_dir: "原始数据缓存",
  card_cache_dir: "角色卡缓存",
  card_log_file: "渲染日志",
  cached_cards: "已缓存角色卡",
  last_render_error: "最近渲染错误",
  last_render_success_at: "最近渲染成功",
  bindings: "绑定数",
  panels: "本地存档数",
  auto_update_minutes: "自动更新间隔（分钟）",
  auto_update_running: "自动更新任务",
  last_update_at: "最近一次更新",
  last_update_msg: "最近更新结果",
};

const TIME_KEYS = new Set([
  "last_render_success_at",
  "last_update_at",
]);

async function runDiagnose() {
  checksBox.innerHTML = "";
  diagInfo.innerHTML = "";
  const btn = $("btn-diagnose");
  btn.disabled = true;
  btn.textContent = "检测中…";

  try {
    const resp = await get("diagnose");
    const payload = unwrap(resp, "checks") !== undefined ? resp : resp.data || {};
    const checks = payload.checks || payload.data?.checks || [];
    const info = payload.info || payload.data?.info || {};
    renderChecks(checks);
    renderInfo(info);
    checksBox.dataset.loaded = "1";
  } catch (err) {
    checksBox.innerHTML = "";
    checksBox.appendChild(el("p", "msg err", "自检失败：" + err.message));
  } finally {
    btn.disabled = false;
    btn.textContent = "运行自检";
  }
}

function renderChecks(checks) {
  checksBox.innerHTML = "";
  for (const check of checks) {
    const item = el("div", "check " + (check.ok ? "ok" : "bad"));
    const mark = el("span", "check-mark", check.ok ? "✓" : "✗");
    const name = el("span", "check-name", check.name);
    const detail = el("span", "check-detail", check.detail || "");
    item.append(mark, name, detail);
    checksBox.appendChild(item);
  }
}

function renderInfo(info) {
  diagInfo.innerHTML = "";
  for (const [key, value] of Object.entries(info)) {
    if (value === "" || value == null) continue;
    const tr = document.createElement("tr");
    tr.appendChild(el("th", "", INFO_LABELS[key] || key));

    let text = value;
    if (TIME_KEYS.has(key) && typeof value === "number" && value > 0) {
      text = fmtTime(value);
    } else if (typeof value === "boolean") {
      text = value ? "是" : "否";
    } else if (key === "last_update_msg" && !value) {
      text = "—";
    }
    tr.appendChild(el("td", "", text));
    diagInfo.appendChild(tr);
  }
}

/* ============================ 事件绑定 ============================ */

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();

  const user_id = $("input-qq").value.trim();
  const uid = $("input-uid").value.trim();

  if (!/^\d+$/.test(user_id)) {
    show(addMsg, "QQ 号必须是纯数字", "err");
    return;
  }
  if (!/^\d+$/.test(uid)) {
    show(addMsg, "UID 必须是纯数字", "err");
    return;
  }

  const btn = e.target.querySelector("button");
  btn.disabled = true;
  show(addMsg, "保存中…");
  try {
    await post("bindings/add", { user_id, uid });
    show(addMsg, `已绑定：${user_id} → ${uid}`, "ok");
    $("input-qq").value = "";
    $("input-uid").value = "";
    await loadList();
  } catch (err) {
    show(addMsg, "添加失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

$("btn-reload").addEventListener("click", loadList);
$("btn-refresh-all").addEventListener("click", (e) => refreshAll(e.target));
$("btn-panels-reload").addEventListener("click", loadPanels);
$("btn-settings-reload").addEventListener("click", loadSettings);
$("btn-settings-save").addEventListener("click", saveSettings);
$("btn-diagnose").addEventListener("click", runDiagnose);

$("btn-detail-close").addEventListener("click", () => {
  detailCard.hidden = true;
});

$("btn-detail-refresh").addEventListener("click", () => {
  if (detail.uid) refreshPanel(detail.uid, $("btn-detail-refresh"));
});

$("btn-show-image").addEventListener("click", () => {
  if (detail.card && detail.card.image) showCardImage(detail.card.image);
  else if (detail.card && detail.card.html) showCardHtml(detail.card.html);
});

$("btn-show-html").addEventListener("click", () => {
  if (detail.card && detail.card.html) showCardHtml(detail.card.html);
});

$("btn-show-text").addEventListener("click", showCardText);

/* ============================ 初始化 ============================ */

if (bridge && typeof bridge.ready === "function") {
  try {
    await bridge.ready();
  } catch (err) {
    console.warn("bridge.ready() 失败", err);
  }
}

if (!bridge) {
  show(tableMsg, "未检测到 AstrBot 插件桥接，请从插件详情页打开本视图", "err");
} else {
  await loadList();
}
