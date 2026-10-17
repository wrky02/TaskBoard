"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let capability =
  location.hash.slice(1) || sessionStorage.getItem("taskboard-entry") || "";
if (capability) sessionStorage.setItem("taskboard-entry", capability);
history.replaceState(null, "", "/");
let state,
  page = "board",
  query = "",
  tagFilter = "",
  bulk = false,
  selected = new Set(),
  draft = null,
  dirty = false,
  saving = false;
let month = new Date().getMonth(),
  year = new Date().getFullYear(),
  dragId = null,
  toastTimer,
  viewTimer,
  firstRun = false,
  lastFocus = null;
const titles = {
  board: "工作看板",
  calendar: "日历视图",
  reminders: "到期提醒",
  archive: "已完成归档",
  trash: "回收站",
  activity: "操作记录",
  tags: "标签管理",
  tools: "数据与设置",
};
const descriptions = {
  board: "把想法变成行动，让重要的事有序发生。",
  calendar: "让截止日期清晰可见，提前安排每一次交付。",
  reminders: "先照顾紧迫的事，再从容推进下一步。",
  archive: "每一项完成的任务，都值得被妥善保存。",
  trash: "给误删一次机会，恢复后继续推进。",
  activity: "记录每一次推进，让工作有迹可循。",
  tags: "用清晰的分类，整理不同的工作主题。",
  tools: "照顾好你的数据，让工作更安心。",
};
const paths = [
  "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  "M4 5h16v16H4z M4 10h16 M8 2v6 M16 2v6",
  "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  "M3 4h18v5H3z M5 9v12h14V9 M9 13h6",
  "M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7",
  "M4 6h16 M4 12h16 M4 18h12",
  "M3 3h8l10 10-8 8L3 11z M7 7h.01",
  "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2",
];
$$("#nav button span").forEach(
  (el, i) =>
    (el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[i]}"/></svg>`),
);
function notify(text) {
  clearTimeout(toastTimer);
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 4500);
}
async function api(path, data) {
  const options =
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-TaskBoard": capability,
          },
          body: JSON.stringify(data),
        };
  const r = await fetch("/api/" + path, options);
  const result = await r.json();
  if (!r.ok) {
    if (r.status === 401 && path !== "login") {
      saving = false;
      await closeDrawer(true);
      state = null;
      $("#pageContent").replaceChildren();
      showLogin(false);
    }
    throw new Error(result.error || "操作失败");
  }
  return result;
}
function report(error) {
  notify(error.message || String(error));
}
// Only the confirmation button submits; Enter must not activate Cancel.
$("#dialogCancel").onclick = () => $("#dialog").close("cancel");
let dialogComposing = false;
$("#dialogForm").addEventListener("compositionstart", () => {
  dialogComposing = true;
});
$("#dialogForm").addEventListener("compositionend", () => {
  dialogComposing = false;
});
$("#dialogForm").addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    (dialogComposing || event.isComposing || event.keyCode === 229)
  ) {
    event.preventDefault();
  }
});
async function dialog(title, message, fields = [], ok = "确定", cancel = true) {
  if ($("#dialog").open) return null;
  $("#dialogTitle").textContent = title;
  $("#dialogMessage").textContent = message;
  $("#dialogFields").innerHTML = fields
    .map(
      (f) =>
        `<label for="dlg-${esc(f.key)}">${esc(f.label)}</label>${f.options ? `<select id="dlg-${esc(f.key)}" name="${esc(f.key)}">${f.options.map((o) => `<option value="${esc(o.value)}" ${String(o.value) === String(f.value) ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>` : `<input id="dlg-${esc(f.key)}" name="${esc(f.key)}" value="${esc(f.value || "")}" type="${f.secret ? "password" : "text"}" maxlength="${f.maxLength || 240}" ${f.required ? "required" : ""}>`}`,
    )
    .join("");
  $("#dialogOk").textContent = ok;
  $("#dialogCancel").hidden = !cancel;
  $("#dialog").returnValue = "";
  dialogComposing = false;
  $("#dialog").showModal();
  ($("#dialogFields input, #dialogFields select") || $("#dialogOk")).focus();
  return new Promise((resolve) => {
    $("#dialog").addEventListener(
      "close",
      () => {
        const values = Object.fromEntries(new FormData($("#dialogForm")));
        resolve(
          $("#dialog").returnValue === "ok"
            ? fields.length
              ? values
              : true
            : null,
        );
      },
      { once: true },
    );
  });
}
async function confirmAction(message, title = "确认操作", ok = "确定") {
  return Boolean(await dialog(title, message, [], ok));
}
function showLogin(first) {
  firstRun = first;
  $("#login").hidden = false;
  $("#workspace").hidden = true;
  $("#loginHeading").textContent = first ? "创建你的工作区" : "欢迎回来";
  $("#loginHint").textContent = first
    ? "首次使用，请设置至少 8 位本机密码。"
    : "输入本机密码，进入你的工作区。";
  $("#confirmPassword").hidden = $("#confirmLabel").hidden = !first;
  $("#confirmPassword").required = first;
  $("#password").value = "";
  $("#confirmPassword").value = "";
  $("#password").focus();
}
function applyBrand() {
  const name = state.names.system_name || "任务看板系统";
  $("#brandName").textContent = name;
  document.title = name;
  $("#welcome").textContent = state.names.user_name || "个人工作区";
  $("#avatar").textContent = (state.names.user_name || "我").slice(0, 1);
}
async function enter(data) {
  state = data;
  query = state.view_state.query || "";
  tagFilter = state.view_state.tag_filter || "";
  if (tagFilter && !state.tags.some((t) => String(t.id) === tagFilter))
    tagFilter = "";
  $("#login").hidden = true;
  $("#workspace").hidden = false;
  applyBrand();
  render();
  if (state.reminder_mode === "login" && overdue().length)
    notify(`有 ${overdue().length} 项任务已逾期，可前往“到期提醒”查看。`);
}
$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  $("#loginError").textContent = "";
  const b = e.submitter;
  b.disabled = true;
  try {
    await enter(
      await api("login", {
        password: $("#password").value,
        confirm: $("#confirmPassword").value,
      }),
    );
    $("#password").value = "";
    $("#confirmPassword").value = "";
  } catch (err) {
    $("#loginError").textContent = err.message;
  } finally {
    b.disabled = false;
  }
};
function overdue() {
  return state.tasks.filter(
    (t) => t.status !== "完成" && t.due_date && t.due_date < state.today,
  );
}
function soon() {
  const end = new Date(state.today + "T12:00:00");
  end.setDate(end.getDate() + 3);
  const iso = localISO(end);
  return state.tasks.filter(
    (t) =>
      t.status !== "完成" &&
      t.due_date &&
      t.due_date >= state.today &&
      t.due_date <= iso,
  );
}
function localISO(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function filtered() {
  return state.tasks.filter(
    (t) =>
      (!query ||
        (t.title + " " + t.note)
          .toLocaleLowerCase()
          .includes(query.toLocaleLowerCase())) &&
      (!tagFilter || t.tags.some((v) => String(v.id) === String(tagFilter))),
  );
}
function tagOptions(empty = "全部标签") {
  return (
    `<option value="">${empty}</option>` +
    state.tags
      .map(
        (t) =>
          `<option value="${t.id}" ${String(t.id) === String(tagFilter) ? "selected" : ""}>${esc(t.name)}</option>`,
      )
      .join("")
  );
}
function card(t) {
  const check = t.checklist.length
    ? `${t.checklist.filter((c) => c.done).length}/${t.checklist.length} 检查项`
    : "尚无检查项";
  const expired = t.status !== "完成" && t.due_date && t.due_date < state.today;
  return `<article class="card" draggable="${!bulk}" data-task="${t.id}" tabindex="0" role="button" aria-label="查看任务：${esc(t.title)}">${bulk ? `<label class="card-select"><input type="checkbox" data-select="${t.id}" ${selected.has(t.id) ? "checked" : ""}> 选择任务</label>` : ""}<div>${t.tags.map((tag) => `<span class="tag">${esc(tag.name)}</span>`).join("")}</div><h3>${esc(t.title)}</h3><p>${esc(t.note || "还没有备注，记录下一步的想法…")}</p><div class="card-bottom"><span class="${expired ? "overdue" : ""}">${expired ? "已逾期 · " : ""}${esc(t.due_date || "未设截止日期")}</span><span>${t.files.length ? "附件 " + t.files.length + " · " : ""}${check}</span></div></article>`;
}
function board() {
  return `<section class="stats">${[
    ["全部任务", state.tasks.length, "▦"],
    ["正在进行", state.tasks.filter((t) => t.status === "进行中").length, "↗"],
    ["已经完成", state.tasks.filter((t) => t.status === "完成").length, "✓"],
    ["逾期待办", overdue().length, "◷"],
  ]
    .map(
      ([label, count, icon]) =>
        `<div class="stat"><div><small>${label}</small><strong>${count.toString().padStart(2, "0")}</strong></div><span class="stat-icon">${icon}</span></div>`,
    )
    .join(
      "",
    )}</section><div class="toolbar"><input id="search" value="${esc(query)}" placeholder="搜索任务标题或备注…" aria-label="搜索任务"><select id="tagFilter" aria-label="按标签筛选">${tagOptions()}</select><span class="spacer"></span><span class="count" id="resultCount"></span><button id="bulkToggle">${bulk ? "退出批量" : "批量选择"}</button></div><div id="bulkSlot"></div><div id="lanes" class="board"></div><p class="board-footnote">跨列拖动改变阶段 · 同列拖动调整顺序 · 所有数据保存在本机</p>`;
}
function drawLanes() {
  const tasks = filtered();
  $("#resultCount").textContent = `${tasks.length} 项任务`;
  $("#lanes").innerHTML = ["计划中", "进行中", "完成"]
    .map((status) => {
      const rows = tasks.filter((t) => t.status === status);
      return `<section class="lane" data-status="${status}"><div class="lane-header"><span class="lane-dot"></span>${status}<span class="lane-count">${rows.length}</span><button data-add="${status}" aria-label="在${status}新建任务">＋</button></div><div class="lane-items">${rows.map(card).join("") || '<div class="empty">留一点空间，<br>开始下一项值得做的事。</div>'}</div><button class="lane-add" data-add="${status}">＋ 添加任务</button></section>`;
    })
    .join("");
  bindCards();
  $$("[data-add]").forEach(
    (b) => (b.onclick = () => showTask(null, b.dataset.add)),
  );
  $$(".lane").forEach((lane) => {
    lane.ondragover = (e) => {
      if (!dragId) return;
      e.preventDefault();
      $$(".drag-target").forEach((c) => c.classList.remove("drag-target"));
      const target = e.target.closest(".card");
      if (target && Number(target.dataset.task) !== dragId)
        target.classList.add("drag-target");
    };
    lane.ondrop = async (e) => {
      e.preventDefault();
      if (!dragId) return;
      const id = dragId;
      dragId = null;
      const target = e.target.closest(".card");
      if (target && Number(target.dataset.task) === id) {
        drawLanes();
        return;
      }
      const full = state.tasks.filter(
        (t) => t.status === lane.dataset.status && t.id !== id,
      );
      let index = full.length;
      if (target && Number(target.dataset.task) !== id) {
        index = full.findIndex((t) => t.id === Number(target.dataset.task));
        if (
          e.clientY >
          target.getBoundingClientRect().top + target.offsetHeight / 2
        )
          index++;
      }
      try {
        state = await api("task/action", {
          id,
          action: "move",
          status: lane.dataset.status,
          index,
        });
        drawLanes();
        notify("任务顺序已保存");
      } catch (err) {
        report(err);
        drawLanes();
      }
    };
  });
  drawBulk();
}
function bindCards() {
  $$("[data-task]").forEach((c) => {
    c.onclick = (e) => {
      if (e.target.closest("[data-select]")) return;
      if (bulk) {
        const id = Number(c.dataset.task);
        selected.has(id) ? selected.delete(id) : selected.add(id);
        drawLanes();
      } else showTask(Number(c.dataset.task));
    };
    c.onkeydown = (e) => {
      if (e.key === "Enter" && e.target === c) c.click();
    };
    c.ondragstart = (e) => {
      dragId = Number(c.dataset.task);
      c.classList.add("dragging");
      e.dataTransfer.setData("text/plain", String(dragId));
      e.dataTransfer.effectAllowed = "move";
    };
    c.ondragend = () => {
      dragId = null;
      $$(".dragging,.drag-target").forEach((el) =>
        el.classList.remove("dragging", "drag-target"),
      );
    };
  });
  $$("[data-select]").forEach(
    (cb) =>
      (cb.onchange = () => {
        const id = Number(cb.dataset.select);
        cb.checked ? selected.add(id) : selected.delete(id);
        drawBulk();
      }),
  );
}
function drawBulk() {
  if (!$("#bulkSlot")) return;
  $("#bulkSlot").innerHTML = bulk
    ? `<div class="bulk-bar"><strong>已选 ${selected.size} 项</strong><select id="bulkStatus" aria-label="批量目标阶段"><option>计划中</option><option>进行中</option><option>完成</option></select><button id="bulkMove">变更阶段</button><select id="bulkTag" aria-label="批量添加标签">${tagOptions("选择标签")}</select><button id="bulkAddTag">添加标签</button><button id="bulkTrash" class="danger-text">移入回收站</button></div>`
    : "";
  if (!bulk) return;
  $("#bulkMove").onclick = () =>
    runBulk("status", { status: $("#bulkStatus").value });
  $("#bulkAddTag").onclick = () => {
    if (!$("#bulkTag").value) return notify("请先选择标签");
    runBulk("tag", { tag: Number($("#bulkTag").value) });
  };
  $("#bulkTrash").onclick = async () => {
    if (await confirmAction(`将选中的 ${selected.size} 项任务移入回收站？`))
      runBulk("trash");
  };
}
async function runBulk(action, extra = {}) {
  if (!selected.size) return notify("请先选择任务");
  try {
    const r = await api("bulk", { action, ids: [...selected], ...extra });
    state = r.state;
    selected = new Set(r.result.failed.map((f) => f.id));
    render();
    notify(
      `成功 ${r.result.ok.length} 项，失败 ${r.result.failed.length} 项${r.result.failed.length ? "：" + r.result.failed.map((f) => f.error).join("；") : ""}`,
    );
  } catch (err) {
    report(err);
  }
}
function taskList(tasks, kind = "normal") {
  if (!tasks.length) return '<div class="empty">这里暂时没有任务。</div>';
  return tasks
    .map(
      (t) =>
        `<div class="list-row"><div class="grow">${kind === "normal" ? `<button class="link-button" data-open="${t.id}">${esc(t.title)}</button>` : `<strong>${esc(t.title)}</strong>`}<p>${esc(t.status)} · ${esc(t.due_date || "未设截止日期")} · ${t.files.length} 个附件</p></div>${kind === "archive" ? `<button data-action="unarchive" data-id="${t.id}">取消归档</button>` : kind === "trash" ? `<button data-action="restore" data-id="${t.id}">恢复</button><button class="danger-text" data-action="erase" data-id="${t.id}">彻底删除</button>` : ""}</div>`,
    )
    .join("");
}
function calendar() {
  const first = new Date(year, month, 1),
    days = new Date(year, month + 1, 0).getDate();
  return `<div class="calendar-head"><button id="prevMonth" aria-label="上一月">←</button><h3>${year} 年 ${month + 1} 月</h3><button id="nextMonth" aria-label="下一月">→</button></div><div class="calendar-grid">${["一", "二", "三", "四", "五", "六", "日"].map((d) => `<div class="weekday">${d}</div>`).join("")}${"<div></div>".repeat((first.getDay() + 6) % 7)}${Array.from(
    { length: days },
    (_, n) => {
      const day = localISO(new Date(year, month, n + 1));
      return `<div class="calendar-day ${day === state.today ? "today" : ""}">${n + 1}${state.tasks
        .filter((t) => t.due_date === day)
        .map(
          (t) =>
            `<button class="calendar-task" data-open="${t.id}" title="${esc(t.title)}">${esc(t.title)}</button>`,
        )
        .join("")}</div>`;
    },
  ).join("")}</div>`;
}
function tools() {
  return `<div class="settings-grid"><section class="panel"><h3>工作区偏好</h3><form id="settingsForm"><label for="systemName">系统名称</label><input id="systemName" maxlength="60" value="${esc(state.names.system_name)}" placeholder="任务看板系统"><label for="userName">使用人姓名</label><input id="userName" maxlength="40" value="${esc(state.names.user_name)}"><label for="reminderMode">登录提醒</label><select id="reminderMode"><option value="login" ${state.reminder_mode === "login" ? "selected" : ""}>登录时提醒逾期任务</option><option value="off" ${state.reminder_mode === "off" ? "selected" : ""}>关闭提醒</option></select><button class="primary" type="submit">保存设置</button></form></section><section class="panel"><h3>备份与恢复</h3><p class="hint">完整备份包含数据库和全部附件（含归档与回收站）。恢复前会校验文件并自动保留恢复前副本。请定期把备份保存到其他磁盘。</p><div class="tools-actions"><button id="backupBtn">下载完整备份</button><button id="restoreBtn">从备份恢复</button></div><input type="file" id="restoreInput" accept=".zip" hidden><h3 class="section-label">任务清单导出</h3><p class="hint">CSV / Excel 包含当前及归档任务，不包含附件本体。</p><div class="tools-actions"><button id="csvBtn">导出 CSV</button><button id="xlsxBtn">导出 Excel</button></div></section><section class="panel"><h3>附件健康检查</h3><p class="hint">只读检查缺失附件与未引用文件，不自动删除任何文件。</p><button id="healthBtn">开始检查</button><div id="healthReport" class="health-report"></div></section><section class="panel"><h3>密码与安全</h3><p class="hint">密码用于本机工作区登录。退出程序会关闭本地服务；关闭浏览器窗口后服务仍可通过快捷方式重新打开。电脑的其他使用者应使用独立 Windows 账户。</p><button id="passwordBtn">修改本机密码</button></section></div>`;
}
function render() {
  if (!state) return;
  applyBrand();
  $("#pageTitle").textContent = $("#breadcrumb").textContent = titles[page];
  $("#pageDescription").textContent = descriptions[page];
  $("#newTask").hidden = page !== "board";
  $("#pageEyebrow").textContent =
    page === "board" ? "WORK OVERVIEW" : "YOUR WORKSPACE";
  $$("#nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.page === page),
  );
  const pages = {
    board,
    calendar,
    reminders: () =>
      `<section class="panel"><h3>已逾期 · ${overdue().length} 项</h3>${taskList(overdue())}</section><section class="panel"><h3>未来三天 · ${soon().length} 项</h3>${taskList(soon())}</section>`,
    archive: () =>
      `<section class="panel">${taskList(state.archived, "archive")}</section>`,
    trash: () =>
      `<section class="panel">${taskList(state.trash, "trash")}</section>`,
    activity: () =>
      `<section class="panel">${state.activity.map((a) => `<div class="list-row"><div class="grow"><strong>${esc(a.action)}</strong> · ${esc(a.title)}<p>${esc(a.detail)}</p></div><small class="muted">${esc(a.at.replace("T", " "))}</small></div>`).join("") || '<div class="empty">暂无记录。</div>'}</section>`,
    tags: () =>
      `<section class="panel"><div class="toolbar"><h3>全部标签</h3><span class="spacer"></span><button id="newTag" class="primary">＋ 新建标签</button></div>${state.tags.map((t) => `<div class="list-row"><span class="tag">${esc(t.name)}</span><span class="grow"></span><button data-tag-edit="${t.id}">重命名</button><button data-tag-delete="${t.id}" class="danger-text">删除</button></div>`).join("") || '<div class="empty">添加标签，整理你的工作。</div>'}</section>`,
    tools,
  };
  $("#pageContent").innerHTML = pages[page]();
  if (page === "board") {
    drawLanes();
    $("#search").oninput = (e) => {
      query = e.target.value;
      drawLanes();
      saveView();
    };
    $("#tagFilter").onchange = (e) => {
      tagFilter = e.target.value;
      drawLanes();
      saveView();
    };
    $("#bulkToggle").onclick = () => {
      bulk = !bulk;
      selected.clear();
      render();
    };
  }
  if (page === "calendar") {
    $("#prevMonth").onclick = () => {
      if (--month < 0) {
        month = 11;
        year--;
      }
      render();
    };
    $("#nextMonth").onclick = () => {
      if (++month > 11) {
        month = 0;
        year++;
      }
      render();
    };
  }
  $$("[data-open]").forEach(
    (b) => (b.onclick = () => showTask(Number(b.dataset.open))),
  );
  $$("[data-action]").forEach(
    (b) =>
      (b.onclick = () => taskAction(Number(b.dataset.id), b.dataset.action)),
  );
  if (page === "tags") bindTags();
  if (page === "tools") bindTools();
}
function saveView() {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(
    () => api("view", { query, tag_filter: tagFilter }).catch(report),
    300,
  );
}
async function navigate(next) {
  if (!(await closeDrawer())) return;
  page = next;
  bulk = false;
  selected.clear();
  render();
}
$$("#nav button").forEach((b) => (b.onclick = () => navigate(b.dataset.page)));
$("#newTask").onclick = () => showTask();
function showTask(id = null, status = "计划中") {
  if (draft) return;
  const t = id ? state.tasks.find((t) => t.id === id) : null;
  if (id && !t) return;
  lastFocus = document.activeElement;
  draft = t
    ? structuredClone(t)
    : {
        id: null,
        title: "",
        note: "",
        status,
        due_date: "",
        tags: [],
        files: [],
        checklist: [],
        milestones: [],
      };
  draft.new_files = [];
  draft.removed_files = [];
  dirty = false;
  $("#drawerTitle").textContent = id ? "任务详情" : "新建任务";
  $("#taskTitle").value = draft.title;
  $("#taskStatus").value = draft.status;
  $("#taskDue").value = draft.due_date || "";
  updateDueLabel();
  $("#taskNote").value = draft.note;
  $("#taskTags").innerHTML =
    state.tags
      .map(
        (t) =>
          `<label><input type="checkbox" value="${t.id}" ${draft.tags.some((v) => v.id === t.id) ? "checked" : ""}>${esc(t.name)}</label>`,
      )
      .join("") || '<span class="hint">暂无标签，可在标签管理中添加。</span>';
  $("#duplicateTask").hidden = $("#trashTask").hidden = !id;
  $("#archiveTask").hidden = !id || draft.status !== "完成";
  $("#taskHistory").textContent = id
    ? state.activity
        .filter((a) => a.task_id === id)
        .slice(0, 5)
        .map((a) => `${a.at.replace("T", " ")} · ${a.action}`)
        .join("\n")
    : "保存后开始记录任务历程。";
  $("#checkInput").value = $("#milestoneInput").value = "";
  drawChecks();
  drawMilestones();
  drawFiles();
  $("#drawer").hidden = false;
  $("#workspace").inert = true;
  $("#taskTitle").focus();
}
async function closeDrawer(force = false) {
  if (!draft) return true;
  if (saving) return false;
  if (
    !force &&
    dirty &&
    !(await confirmAction(
      "任务有未保存的修改，附件、检查项与里程碑也会被放弃。",
      "放弃未保存的修改？",
      "放弃修改",
    ))
  )
    return false;
  draft = null;
  dirty = false;
  $("#drawer").hidden = true;
  $("#workspace").inert = false;
  if (lastFocus?.isConnected) lastFocus.focus();
  return true;
}
$("#closeDrawer").onclick = $("#cancelTask").onclick = () => closeDrawer();
// No full-board overlay is drawn. The workspace is inert while the drawer is
// open; capture outside clicks so they cannot reach task cards underneath.
document.addEventListener(
  "pointerdown",
  (e) => {
    if (
      !draft ||
      $("#dialog").open ||
      $("#imageDialog").open ||
      $("#dateDialog").open
    )
      return;
    if (!e.target.closest("#drawer")) {
      e.preventDefault();
      e.stopImmediatePropagation();
      closeDrawer();
    }
  },
  true,
);
$("#taskForm").addEventListener("input", () => {
  dirty = true;
});
$("#taskForm").addEventListener("change", () => {
  dirty = true;
});
function drawChecks() {
  if (!draft) return;
  $("#checkCount").textContent =
    `${draft.checklist.filter((c) => c.done).length} / ${draft.checklist.length}`;
  $("#checklist").innerHTML = draft.checklist
    .map(
      (c, i) =>
        `<div class="check-row ${c.done ? "done" : ""}"><input type="checkbox" data-check="${i}" ${c.done ? "checked" : ""} aria-label="完成检查项 ${esc(c.label)}"><span>${esc(c.label)}</span><button type="button" data-check-remove="${i}" aria-label="删除检查项">×</button></div>`,
    )
    .join("");
  $$("[data-check]").forEach(
    (cb) =>
      (cb.onchange = () => {
        draft.checklist[Number(cb.dataset.check)].done = cb.checked;
        dirty = true;
        drawChecks();
      }),
  );
  $$("[data-check-remove]").forEach(
    (b) =>
      (b.onclick = () => {
        draft.checklist.splice(Number(b.dataset.checkRemove), 1);
        dirty = true;
        drawChecks();
      }),
  );
}
$("#addCheck").onclick = () => {
  const label = $("#checkInput").value.trim();
  if (!label) return;
  draft.checklist.push({ label, done: false });
  dirty = true;
  $("#checkInput").value = "";
  drawChecks();
  $("#checkInput").focus();
};
$("#checkInput").onkeydown = (e) => {
  if (e.key === "Enter" && !e.isComposing) {
    e.preventDefault();
    $("#addCheck").click();
  }
};
function drawMilestones() {
  if (!draft) return;
  $("#milestones").innerHTML = draft.milestones
    .map(
      (m, i) =>
        `<div class="check-row"><input type="checkbox" data-milestone="${i}" ${m.confirmed ? "checked" : ""} aria-label="确认里程碑 ${esc(m.name)}"><span>${esc(m.name)}<small>${m.confirmed ? (m.confirmed_at ? "已确认 · " + esc(m.confirmed_at.replace("T", " ")) : "保存时记录确认时间") : "待确认"}</small></span><button type="button" data-milestone-remove="${i}" aria-label="删除里程碑">×</button></div>`,
    )
    .join("");
  $$("[data-milestone]").forEach(
    (cb) =>
      (cb.onchange = () => {
        draft.milestones[Number(cb.dataset.milestone)].confirmed = cb.checked;
        dirty = true;
        drawMilestones();
      }),
  );
  $$("[data-milestone-remove]").forEach(
    (b) =>
      (b.onclick = () => {
        draft.milestones.splice(Number(b.dataset.milestoneRemove), 1);
        dirty = true;
        drawMilestones();
      }),
  );
}
$("#addMilestone").onclick = () => {
  const name = $("#milestoneInput").value.trim();
  if (!name) return;
  draft.milestones.push({ name, confirmed: false });
  dirty = true;
  $("#milestoneInput").value = "";
  drawMilestones();
};
$("#milestoneInput").onkeydown = (e) => {
  if (e.key === "Enter" && !e.isComposing) {
    e.preventDefault();
    $("#addMilestone").click();
  }
};
function drawFiles() {
  if (!draft) return;
  $("#files").innerHTML =
    draft.files
      .filter((f) => !draft.removed_files.includes(f.id))
      .map(
        (f) =>
          `<div class="file-row">${f.is_image ? `<img src="${f.url}" data-preview="${f.id}" alt="${esc(f.filename)}">` : ""}<div class="grow"><div class="file-name">${esc(f.filename)}</div><input data-file-label="${f.id}" maxlength="20" value="${esc(f.version_label)}" aria-label="附件版本标签"><input data-file-note="${f.id}" maxlength="200" value="${esc(f.version_note)}" placeholder="版本备注" aria-label="附件版本备注"></div><button type="button" data-download="${f.id}">下载</button><button type="button" data-remove-file="${f.id}" aria-label="移除附件">×</button></div>`,
      )
      .join("") +
    draft.new_files
      .map(
        (f, i) =>
          `<div class="file-row"><div class="grow"><div class="file-name">${esc(f.name)}</div><div class="pending-label">待保存 · ${(f.size / 1024).toFixed(1)} KB</div></div><button type="button" data-remove-new="${i}" aria-label="取消新增附件">×</button></div>`,
      )
      .join("");
  $$("[data-preview]").forEach(
    (b) =>
      (b.onclick = () =>
        previewFile(
          draft.files.find((f) => f.id === Number(b.dataset.preview)),
        )),
  );
  $$("[data-download]").forEach(
    (b) =>
      (b.onclick = () =>
        download("/api/file?id=" + b.dataset.download + "&download=1")),
  );
  $$("[data-remove-file]").forEach(
    (b) =>
      (b.onclick = () => {
        draft.removed_files.push(Number(b.dataset.removeFile));
        dirty = true;
        drawFiles();
      }),
  );
  $$("[data-remove-new]").forEach(
    (b) =>
      (b.onclick = () => {
        draft.new_files.splice(Number(b.dataset.removeNew), 1);
        dirty = true;
        drawFiles();
      }),
  );
  $$("[data-file-label]").forEach(
    (input) =>
      (input.oninput = () => {
        draft.files.find(
          (f) => f.id === Number(input.dataset.fileLabel),
        ).version_label = input.value;
        dirty = true;
      }),
  );
  $$("[data-file-note]").forEach(
    (input) =>
      (input.oninput = () => {
        draft.files.find(
          (f) => f.id === Number(input.dataset.fileNote),
        ).version_note = input.value;
        dirty = true;
      }),
  );
}
function previewFile(f) {
  $("#previewImage").src = f.url;
  $("#previewName").textContent = f.filename;
  $("#imageDialog").showModal();
}
$("#closeImage").onclick = () => $("#imageDialog").close();
$("#imageDialog").addEventListener("close", () =>
  $("#previewImage").removeAttribute("src"),
);
$("#pickFiles").onclick = () => $("#fileInput").click();
$("#fileInput").onchange = (e) => {
  queueFiles([...e.target.files]);
  e.target.value = "";
};
function queueFiles(files) {
  if (!draft) return;
  for (const f of files) {
    if (!f.size || f.size > 100 * 1024 * 1024) {
      notify("单个附件不能为空且不能超过 100 MB");
      continue;
    }
    if (
      draft.new_files.reduce((n, x) => n + x.size, 0) + f.size >
      100 * 1024 * 1024
    ) {
      notify("本次新增附件总大小不能超过 100 MB，请分次保存");
      continue;
    }
    draft.new_files.push(f);
    dirty = true;
  }
  drawFiles();
}
$("#taskNote").addEventListener("paste", (e) => {
  const files = [...e.clipboardData.items]
    .filter((i) => i.kind === "file" && i.type.startsWith("image/"))
    .map((i) => i.getAsFile())
    .filter(Boolean);
  if (files.length) {
    e.preventDefault();
    queueFiles(files);
  }
});
function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}
$("#taskForm").onsubmit = async (e) => {
  e.preventDefault();
  if (!draft || saving) return;
  saving = true;
  $("#saveTask").disabled = true;
  try {
    const new_files = [];
    for (const f of draft.new_files)
      new_files.push({ name: f.name, data: await fileBase64(f) });
    const payload = {
      id: draft.id,
      updated_at: draft.updated_at,
      title: $("#taskTitle").value,
      note: $("#taskNote").value,
      status: $("#taskStatus").value,
      due: $("#taskDue").value,
      tags: $$("#taskTags input:checked").map((x) => Number(x.value)),
      checklist: draft.checklist,
      milestones: draft.milestones,
      new_files,
      removed_files: draft.removed_files,
      file_versions: draft.files.filter(
        (f) => !draft.removed_files.includes(f.id),
      ),
    };
    state = await api("task/save", payload);
    saving = false;
    await closeDrawer(true);
    render();
    notify("任务及附件已保存");
  } catch (err) {
    report(err);
  } finally {
    saving = false;
    $("#saveTask").disabled = false;
  }
};
async function taskAction(id, action) {
  if (
    action === "erase" &&
    !(await confirmAction(
      "任务和所有附件将永久删除，无法撤销。",
      "彻底删除任务？",
      "永久删除",
    ))
  )
    return;
  if (
    action === "trash" &&
    !(await confirmAction("将任务移入回收站？之后可以恢复。"))
  )
    return;
  if (draft && !(await closeDrawer())) return;
  try {
    state = await api("task/action", { id, action });
    render();
    notify("操作已完成");
  } catch (err) {
    report(err);
  }
}
$("#duplicateTask").onclick = () => taskAction(draft.id, "duplicate");
$("#trashTask").onclick = () => taskAction(draft.id, "trash");
$("#archiveTask").onclick = () => taskAction(draft.id, "archive");
function bindTags() {
  $("#newTag").onclick = async () => {
    const r = await dialog("新建标签", "为任务添加一个清晰的分类。", [
      { key: "name", label: "标签名称", required: true, maxLength: 32 },
    ]);
    if (r)
      try {
        state = await api("tag", { action: "create", name: r.name });
        render();
      } catch (err) {
        report(err);
      }
  };
  $$("[data-tag-edit]").forEach(
    (b) =>
      (b.onclick = async () => {
        const id = Number(b.dataset.tagEdit);
        const t = state.tags.find((t) => t.id === id);
        const r = await dialog("重命名标签", "相关任务会同步显示新名称。", [
          {
            key: "name",
            label: "标签名称",
            value: t.name,
            required: true,
            maxLength: 32,
          },
        ]);
        if (r)
          try {
            state = await api("tag", { action: "rename", id, name: r.name });
            render();
          } catch (err) {
            report(err);
          }
      }),
  );
  $$("[data-tag-delete]").forEach(
    (b) =>
      (b.onclick = async () => {
        if (!(await confirmAction("删除标签不会删除关联任务。", "删除标签？")))
          return;
        try {
          state = await api("tag", {
            action: "delete",
            id: Number(b.dataset.tagDelete),
          });
          if (tagFilter === b.dataset.tagDelete) {
            tagFilter = "";
            saveView();
          }
          render();
        } catch (err) {
          report(err);
        }
      }),
  );
}
async function download(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) {
      const j = await r.json();
      throw new Error(j.error || "下载失败");
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    const name = r.headers.get("Content-Disposition") || "";
    a.download =
      (name.match(/filename="([^"]+)"/) || [])[1] ||
      decodeURIComponent(
        (name.match(/filename\*=UTF-8''(.+)/) || [])[1] || "attachment",
      );
    a.href = URL.createObjectURL(blob);
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (err) {
    report(err);
  }
}
function bindTools() {
  $("#settingsForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      state = await api("settings", {
        system_name: $("#systemName").value,
        user_name: $("#userName").value,
        reminder_mode: $("#reminderMode").value,
      });
      render();
      notify("设置已保存");
    } catch (err) {
      report(err);
    }
  };
  $("#backupBtn").onclick = () => download("/api/backup");
  $("#csvBtn").onclick = () => download("/api/export/csv");
  $("#xlsxBtn").onclick = () => download("/api/export/xlsx");
  $("#restoreBtn").onclick = () => $("#restoreInput").click();
  $("#restoreInput").onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    if (f.size > 2 * 1024 * 1024 * 1024) return notify("备份不能超过 2 GB");
    if (
      !(await confirmAction(
        "将用备份替换当前数据库和附件。系统会先自动保存恢复前副本。",
        "恢复完整备份？",
        "校验并恢复",
      ))
    )
      return;
    try {
      const response = await fetch("/api/restore", {
        method: "POST",
        headers: {
          "Content-Type": "application/zip",
          "X-TaskBoard": capability,
        },
        body: f,
      });
      const r = await response.json();
      if (!response.ok) throw new Error(r.error || "恢复失败");
      state = r.state;
      query = state.view_state.query || "";
      tagFilter = state.view_state.tag_filter || "";
      render();
      notify("恢复完成；恢复前副本：" + r.safety);
    } catch (err) {
      report(err);
    }
  };
  $("#healthBtn").onclick = async () => {
    try {
      const r = await api("health");
      $("#healthReport").textContent =
        `检查完成：${r.total_attachments} 条附件记录\n缺失 ${r.missing.length} 个，未引用 ${r.orphaned_files.length} 个\n` +
        r.missing
          .map((f) => `缺失：${f.task_title} / ${f.filename}`)
          .join("\n") +
        "\n" +
        r.orphaned_files.map((f) => `未引用：${f.stored_name}`).join("\n");
    } catch (err) {
      report(err);
    }
  };
  $("#passwordBtn").onclick = async () => {
    const r = await dialog(
      "修改本机密码",
      "修改后其他已登录页面会退出。",
      [
        { key: "old", label: "旧密码", secret: true, required: true },
        {
          key: "new",
          label: "新密码（至少 8 位）",
          secret: true,
          required: true,
        },
        { key: "confirm", label: "确认新密码", secret: true, required: true },
      ],
      "修改密码",
    );
    if (r)
      try {
        state = await api("password", r);
        notify("密码已修改");
      } catch (err) {
        report(err);
      }
  };
}
$("#lockBtn").onclick = async () => {
  if (!(await closeDrawer())) return;
  try {
    await api("lock", {});
    state = null;
    $("#pageContent").replaceChildren();
    showLogin(false);
  } catch (err) {
    report(err);
  }
};
$("#exitBtn").onclick = async () => {
  if (!(await closeDrawer())) return;
  if (
    !(await confirmAction(
      "本地服务将关闭。下次可通过桌面快捷方式重新启动。",
      "退出任务看板？",
      "退出程序",
    ))
  )
    return;
  try {
    await api("shutdown", {});
    state = null;
    $("#workspace").hidden = true;
    showLogin(false);
    $("#loginHeading").textContent = "工作区已关闭";
    $("#loginHint").textContent = "请关闭此窗口；下次通过快捷方式启动。";
    $("#loginForm button").disabled = true;
  } catch (err) {
    report(err);
  }
};
window.addEventListener("beforeunload", (e) => {
  if (dirty || saving) {
    e.preventDefault();
    e.returnValue = "";
  }
});
document.addEventListener("keydown", (e) => {
  if (
    e.key === "Escape" &&
    draft &&
    !$("#dialog").open &&
    !$("#dateDialog").open &&
    !$("#imageDialog").open
  ) {
    e.preventDefault();
    closeDrawer();
  }
});
api("status")
  .then(async (status) => {
    if (!capability) {
      showLogin(status.first);
      $("#loginError").textContent = "请通过桌面快捷方式或启动器打开工作区。";
      $("#loginForm button").disabled = true;
      return;
    }
    if (status.authenticated) await enter(await api("state"));
    else showLogin(status.first);
  })
  .catch(report);
