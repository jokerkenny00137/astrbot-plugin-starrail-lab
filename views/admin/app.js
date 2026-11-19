/* 星穹铁道面板 —— 绑定管理视图
 * 所有请求都通过 AstrBot 的 plugin view bridge 转发，不能直接 fetch。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);
const tbody = $("tbody");
const totalBadge = $("total");
const addMsg = $("add-msg");
const tableMsg = $("table-msg");

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

    const btnRefresh = document.createElement("button");
    btnRefresh.textContent = "刷新";
    btnRefresh.addEventListener("click", () => refreshOne(item, btnRefresh));

    const btnDel = document.createElement("button");
    btnDel.textContent = "删除";
    btnDel.className = "danger";
    btnDel.addEventListener("click", () => removeBinding(item, btnDel));

    td.appendChild(btnRefresh);
    td.appendChild(btnDel);
    tr.appendChild(td);

    tbody.appendChild(tr);
  }

  totalBadge.textContent = String(list.length);
}

async function loadList() {
  show(tableMsg, "加载中…");
  try {
    const resp = await get("bindings");
    const list = unwrap(resp, "bindings") || [];
    renderRows(list);
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
    show(tableMsg, "刷新失败：" + err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "刷新";
  }
}

async function removeBinding(item, btn) {
  const ok = window.confirm(
    `确定要解除 ${item.user_id} 与 UID ${item.uid} 的绑定吗？`
  );
  if (!ok) return;

  btn.disabled = true;
  try {
    await post("bindings/delete", {
      user_id: item.user_id,
      platform: item.platform || "",
    });
    show(tableMsg, "已删除", "ok");
    await loadList();
  } catch (err) {
    show(tableMsg, "删除失败：" + err.message, "err");
    btn.disabled = false;
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

$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();

  const user_id = $("input-qq").value.trim();
  const uid = $("input-uid").value.trim();
  const platform = $("input-platform").value.trim();

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
    await post("bindings/add", { user_id, uid, platform });
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

// 初始化：先等 Dashboard 把上下文发过来
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
