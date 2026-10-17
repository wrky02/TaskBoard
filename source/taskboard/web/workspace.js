"use strict";
let extraFilters = { priority: "", status: "", due: "", sort: "manual" };
let draftKey = null,
  draftQueue = Promise.resolve(),
  draftFingerprint = "",
  autosaveBusy = false;
const fileCache = new WeakMap();
let liveReminders = [],
  reminderBusy = false,
  imageIndex = 0,
  undoTimer;
const priorityNames = ["紧急", "高", "普通", "低"];
function optionDefaults() {
  return {
    priority: 2,
    today: "",
    repeat: "off",
    repeat_basis: "due",
    reminder: "off",
    reminder_time: "09:00",
  };
}
function readOptions() {
  return {
    priority: Number($("#taskPriority").value),
    today: $("#taskToday").checked ? state.today : "",
    repeat: $("#taskRepeat").value,
    repeat_basis: $("#repeatBasis").value,
    reminder: $("#taskReminder").value,
    reminder_time: $("#reminderTime").value,
  };
}
function fillOptions(task) {
  const o = { ...optionDefaults(), ...task.options };
  $("#taskPriority").value = o.priority;
  $("#taskToday").checked = o.today === state.today;
  $("#taskRepeat").value = o.repeat;
  $("#repeatBasis").value = o.repeat_basis;
  $("#taskReminder").value = o.reminder;
  $("#reminderTime").value = o.reminder_time;
  const ids = (task.dependencies || []).map((x) =>
    typeof x === "object" ? x.id : x,
  );
  const candidates = [
    ...state.tasks,
    ...state.archived,
    ...state.trash.filter((t) => ids.includes(t.id)),
  ].filter((t) => t.id !== task.id);
  $("#taskDependencies").innerHTML =
    candidates
      .map(
        (t) =>
          `<label><input type="checkbox" value="${t.id}" ${ids.includes(t.id) ? "checked" : ""}>#${t.id} ${esc(t.title)} · ${esc(t.deleted_at ? "回收站" : t.status)}</label>`,
      )
      .join("") || '<p class="hint">暂无其他任务。</p>';
  $("#dependencySearch").value = "";
  $("#attachmentSearch").value = "";
  draftKey = crypto.randomUUID();
  draftFingerprint = "";
  $("#draftStatus").textContent = "修改会自动保存为草稿；保存任务后正式生效。";
}
function relativeDue(t) {
  if (!t.due_date) return "未设截止日期";
  const n = Math.round(
    (Date.parse(t.due_date + "T12:00:00Z") -
      Date.parse(state.today + "T12:00:00Z")) /
      86400000,
  );
  return t.status === "完成"
    ? t.due_date
    : n === 0
      ? "今天截止"
      : n < 0
        ? `逾期 ${-n} 天`
        : `还剩 ${n} 天`;
}
function extraMatch(t) {
  const f = extraFilters;
  if (f.priority !== "" && String(t.options.priority) !== f.priority)
    return false;
  if (f.status && t.status !== f.status) return false;
  if (
    f.due === "overdue" &&
    !(t.status !== "完成" && t.due_date && t.due_date < state.today)
  )
    return false;
  if (f.due === "week") {
    const d = new Date(state.today + "T12:00:00");
    d.setDate(d.getDate() + 6 - ((d.getDay() + 6) % 7));
    if (!t.due_date || t.due_date < state.today || t.due_date > localISO(d))
      return false;
  }
  return true;
}
function sortTasks(tasks) {
  if (extraFilters.sort === "priority")
    return tasks.sort(
      (a, b) =>
        a.options.priority - b.options.priority || a.sort_order - b.sort_order,
    );
  if (extraFilters.sort === "due")
    return tasks.sort((a, b) =>
      (a.due_date || "9999").localeCompare(b.due_date || "9999"),
    );
  return tasks;
}
function workspaceToolbar() {
  return `<form id="quickForm" class="quick-add"><input id="quickTitle" maxlength="240" required placeholder="输入任务标题，回车快速添加…" aria-label="快速添加任务"><button class="primary">添加任务</button></form><div class="toolbar extra-filters"><select id="priorityFilter" aria-label="优先级筛选"><option value="">全部优先级</option>${priorityNames.map((n, i) => `<option value="${i}" ${extraFilters.priority === String(i) ? "selected" : ""}>${n}</option>`).join("")}</select><select id="statusFilter" aria-label="阶段筛选"><option value="">全部阶段</option>${["计划中", "进行中", "完成"].map((x) => `<option ${extraFilters.status === x ? "selected" : ""}>${x}</option>`).join("")}</select><select id="dueFilter" aria-label="截止筛选"><option value="">全部日期</option><option value="week" ${extraFilters.due === "week" ? "selected" : ""}>本周剩余日期</option><option value="overdue" ${extraFilters.due === "overdue" ? "selected" : ""}>已逾期</option></select><select id="sortFilter" aria-label="任务排序">${[
    ["manual", "手动排序"],
    ["priority", "优先级排序"],
    ["due", "截止日期排序"],
  ]
    .map(
      ([v, n]) =>
        `<option value="${v}" ${extraFilters.sort === v ? "selected" : ""}>${n}</option>`,
    )
    .join(
      "",
    )}</select><button id="saveView">保存视图</button><button id="clearFilters">清除筛选</button></div>`;
}
function bindWorkspace() {
  const views = state.views || [];
  $("#savedViews").innerHTML = views
    .map(
      (v) =>
        `<div class="saved-view"><button data-view="${v.id}">${esc(v.name)}</button><button data-delete-view="${v.id}" aria-label="删除视图 ${esc(v.name)}">×</button></div>`,
    )
    .join("");
  $$("[data-view]").forEach(
    (b) =>
      (b.onclick = () => {
        const v = views.find((x) => x.id === Number(b.dataset.view)).filters;
        query = v.query || "";
        tagFilter = v.tag || "";
        extraFilters = {
          priority: "",
          status: "",
          due: "",
          sort: "manual",
          ...v,
        };
        navigate("board");
      }),
  );
  $$("[data-delete-view]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          state = await api("saved-view", {
            action: "delete",
            id: Number(b.dataset.deleteView),
          });
          render();
        } catch (e) {
          report(e);
        }
      }),
  );
  if (page === "board") {
    $("#quickForm").onsubmit = async (e) => {
      e.preventDefault();
      const b = e.submitter;
      b.disabled = true;
      try {
        state = await api("task/save", {
          title: $("#quickTitle").value,
          status: "计划中",
        });
        render();
        $("#quickTitle").focus();
      } catch (err) {
        report(err);
      } finally {
        b.disabled = false;
      }
    };
    for (const [id, key] of [
      ["priorityFilter", "priority"],
      ["statusFilter", "status"],
      ["dueFilter", "due"],
      ["sortFilter", "sort"],
    ])
      $("#" + id).onchange = (e) => {
        extraFilters[key] = e.target.value;
        drawLanes();
      };
    $("#clearFilters").onclick = () => {
      extraFilters = { priority: "", status: "", due: "", sort: "manual" };
      query = "";
      tagFilter = "";
      render();
      saveView();
    };
    $("#saveView").onclick = async () => {
      const r = await dialog(
        "保存常用视图",
        "包含搜索、标签、优先级、阶段、日期和排序。",
        [{ key: "name", label: "视图名称", required: true, maxLength: 40 }],
      );
      if (!r) return;
      try {
        state = await api("saved-view", {
          name: r.name,
          filters: { ...extraFilters, query, tag: tagFilter },
        });
        render();
      } catch (e) {
        report(e);
      }
    };
  }
  if (page === "tools") {
    $("#migrationBtn").onclick = () => $("#migrationInput").click();
    $("#migrationInput").onchange = importDirectory;
    $("#diagnosticsBtn").onclick = () => download("/api/diagnostics");
    $("#closeBehavior").value = state.info.close_behavior;
  }
  if (page === "tags") {
    $$("[data-tag-color]").forEach(
      (b) =>
        (b.onchange = async () => {
          try {
            state = await api("tag", {
              action: "color",
              id: Number(b.dataset.tagColor),
              color: b.value,
            });
            render();
          } catch (e) {
            report(e);
          }
        }),
    );
    $$("[data-tag-merge]").forEach(
      (b) =>
        (b.onclick = async () => {
          const id = Number(b.dataset.tagMerge);
          const candidates = state.tags.filter((t) => t.id !== id);
          if (!candidates.length) return notify("请先创建其他标签。");
          const r = await dialog(
            "合并标签",
            "原标签将移除，任务会使用目标标签。",
            [
              {
                key: "target",
                label: "目标标签",
                options: candidates.map((t) => ({
                  value: t.id,
                  label: t.name,
                })),
              },
            ],
            "合并",
          );
          if (r)
            try {
              state = await api("tag", {
                action: "merge",
                id,
                target: Number(r.target),
              });
              render();
            } catch (e) {
              report(e);
            }
        }),
    );
  }
  refreshReminders();
}
function reminderMarkup() {
  return (
    liveReminders
      .map(
        (r) =>
          `<div class="reminder-row"><div class="grow"><strong>${esc(r.title)}</strong><small>截止 ${esc(r.due)}</small></div><button data-reminder-open="${r.id}">查看</button><button data-reminder-snooze="${r.id}">15 分钟后</button><button data-reminder-ack="${r.id}">知道了</button></div>`,
      )
      .join("") || '<p class="hint">当前没有需要处理的提醒。</p>'
  );
}
async function refreshReminders() {
  if (!state || reminderBusy || document.hidden) return;
  reminderBusy = true;
  try {
    const r = await api("reminders");
    if (!state) return;
    if (r.today && r.today !== state.today) {
      state.today = r.today;
      if (!draft) render();
    }
    liveReminders = r.reminders;
    const markup = reminderMarkup();
    $("#reminderBanner").hidden = !liveReminders.length;
    $("#reminderCount").textContent = `${liveReminders.length} 项任务需要留意`;
    if ($("#reminderItems") && $("#reminderItems").innerHTML !== markup) {
      $("#reminderItems").innerHTML = markup;
      bindReminderButtons();
    }
  } catch (e) {
    if (state)
      $("#reminderCount").textContent = "提醒暂时无法刷新，稍后自动重试";
  } finally {
    reminderBusy = false;
  }
}
function bindReminderButtons() {
  $$("[data-reminder-open]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          state = await api("state");
          showTask(Number(b.dataset.reminderOpen));
        } catch (e) {
          report(e);
        }
      }),
  );
  for (const action of ["snooze", "ack"])
    $$("[data-reminder-" + action + "]").forEach(
      (b) =>
        (b.onclick = async () => {
          const id = Number(b.getAttribute("data-reminder-" + action));
          const r = liveReminders.find((x) => x.id === id);
          try {
            await api("reminder", { id, key: r.key, action });
            await refreshReminders();
          } catch (e) {
            report(e);
          }
        }),
    );
}
function showUndo(key) {
  if (!key) return;
  clearTimeout(undoTimer);
  $("#undoBar").hidden = false;
  $("#undoButton").onclick = async () => {
    try {
      state = await api("undo", { key });
      render();
      $("#undoBar").hidden = true;
      notify("已撤销");
    } catch (e) {
      report(e);
    }
  };
  undoTimer = setTimeout(() => ($("#undoBar").hidden = true), 15000);
}
async function batchEdit() {
  const r = await dialog(
    "批量编辑",
    "只修改选择的字段；日期格式 YYYY-MM-DD，输入“清除”可移除日期。",
    [
      {
        key: "priority",
        label: "优先级",
        options: [
          { value: "", label: "保持不变" },
          ...priorityNames.map((n, i) => ({ value: i, label: n })),
        ],
      },
      {
        key: "status",
        label: "阶段",
        options: ["", "计划中", "进行中", "完成"].map((v) => ({
          value: v,
          label: v || "保持不变",
        })),
      },
      { key: "due", label: "截止日期" },
      {
        key: "tag",
        label: "替换全部标签",
        options: [
          { value: "", label: "保持不变" },
          { value: "clear", label: "清除标签" },
          ...state.tags.map((t) => ({ value: t.id, label: t.name })),
        ],
      },
    ],
    "应用修改",
  );
  if (!r) return;
  const changes = {};
  if (r.priority !== "") changes.priority = Number(r.priority);
  if (r.status) changes.status = r.status;
  if (r.due) changes.due = r.due === "清除" ? "" : r.due;
  if (r.tag) changes.tags = r.tag === "clear" ? [] : [Number(r.tag)];
  try {
    const result = await api("bulk", {
      action: "edit",
      ids: [...selected],
      changes,
    });
    state = result.state;
    render();
    notify(
      `成功 ${result.result.ok.length} 项，失败 ${result.result.failed.length} 项`,
    );
    if (result.result.failed.length)
      await dialog(
        "部分任务未修改",
        result.result.failed.map((x) => `#${x.id}：${x.error}`).join("\n"),
        [],
        "知道了",
        false,
      );
  } catch (e) {
    report(e);
  }
}
function uploadRequest(url, options) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    Object.entries(options.headers).forEach(([k, v]) =>
      xhr.setRequestHeader(k, v),
    );
    xhr.upload.onprogress = (e) => {
      $("#uploadProgress").hidden = false;
      $("#uploadProgress").value = e.lengthComputable ? e.loaded / e.total : 0;
      $("#uploadLabel").textContent = e.lengthComputable
        ? `正在上传任务与附件 ${Math.round((e.loaded / e.total) * 100)}%`
        : "正在上传…";
    };
    xhr.onload = () =>
      resolve({
        ok: xhr.status >= 200 && xhr.status < 300,
        status: xhr.status,
        json: async () => JSON.parse(xhr.responseText),
      });
    xhr.onerror = () => reject(new Error("上传中断，请重试；草稿仍保留。"));
    xhr.send(options.body);
  });
}
async function draftSnapshot() {
  const d = structuredClone({ ...draft, new_files: [] });
  Object.assign(d, {
    title: $("#taskTitle").value,
    note: $("#taskNote").value,
    status: $("#taskStatus").value,
    due_date: $("#taskDue").value,
    options: readOptions(),
    dependencies: $$("#taskDependencies input:checked").map((x) => ({
      id: Number(x.value),
    })),
    tags: $$("#taskTags input:checked").map((x) => ({ id: Number(x.value) })),
  });
  const files = [...draft.new_files];
  for (const file of files) {
    if (!fileCache.has(file)) fileCache.set(file, fileBase64(file));
    d.new_files.push({
      name: file.name,
      type: file.type,
      data: await fileCache.get(file),
    });
  }
  return d;
}
async function autosaveDraft() {
  if (!draft || !dirty || saving || autosaveBusy || !state) return;
  autosaveBusy = true;
  const key = draftKey;
  try {
    const d = await draftSnapshot();
    const json = JSON.stringify(d);
    if (key !== draftKey || json === draftFingerprint) return;
    draftQueue = draftQueue
      .catch(() => {})
      .then(() => api("draft", { key, draft: d }));
    await draftQueue;
    if (key === draftKey) {
      draftFingerprint = json;
      $("#draftStatus").textContent = "草稿已保存至本机 · 尚未正式提交";
    }
  } catch (e) {
    if (key === draftKey)
      $("#draftStatus").textContent = "草稿保存失败：" + e.message;
  } finally {
    autosaveBusy = false;
  }
}
async function clearDraft() {
  const key = draftKey;
  draftKey = null;
  await draftQueue.catch(() => {});
  if (key && state)
    try {
      await api("draft", { action: "delete", key });
    } catch (e) {
      notify("任务已处理，旧草稿未能清除，可在草稿列表删除。");
    }
}
async function loadDrafts() {
  if (!state) return;
  try {
    const result = await api("drafts");
    $("#draftRecovery").innerHTML = result.drafts.length
      ? `<details><summary>发现 ${result.drafts.length} 份未提交草稿</summary>${result.drafts.map((d) => `<div class="list-row"><span class="grow">${esc(d.title || "未命名任务")} · ${esc(d.updated)}</span><button data-recover="${esc(d.id)}">恢复编辑</button><button data-drop-draft="${esc(d.id)}">删除草稿</button></div>`).join("")}</details>`
      : "";
    $$("[data-recover]").forEach(
      (b) =>
        (b.onclick = async () => {
          if (draft) return;
          try {
            const result = await api(
              "draft?key=" + encodeURIComponent(b.dataset.recover),
            );
            const d = result.draft;
            if (d.id && !state.tasks.some((t) => t.id === d.id))
              return notify("原任务已归档或删除，请先恢复原任务，再恢复草稿。");
            showTask(d.id || null);
            const files = d.new_files.map(
              (f) =>
                new File(
                  [Uint8Array.from(atob(f.data), (c) => c.charCodeAt(0))],
                  f.name,
                  { type: f.type },
                ),
            );
            draft = { ...d, new_files: files };
            fillOptions(d);
            draftKey = b.dataset.recover;
            $("#taskTitle").value = d.title;
            $("#taskNote").value = d.note;
            $("#taskStatus").value = d.status;
            $("#taskDue").value = d.due_date || "";
            updateDueLabel();
            $$("#taskTags input").forEach(
              (x) => (x.checked = d.tags.some((t) => t.id === Number(x.value))),
            );
            drawChecks();
            drawMilestones();
            drawFiles();
            dirty = true;
            $("#draftStatus").textContent = "已恢复草稿；保存任务后正式生效。";
          } catch (e) {
            report(e);
          }
        }),
    );
    $$("[data-drop-draft]").forEach(
      (b) =>
        (b.onclick = async () => {
          if (!(await confirmAction("删除这份未提交草稿？"))) return;
          try {
            await api("draft", { action: "delete", key: b.dataset.dropDraft });
            await loadDrafts();
          } catch (e) {
            report(e);
          }
        }),
    );
  } catch (e) {
    report(e);
  }
}
function filterAttachments() {
  const q = $("#attachmentSearch").value.toLocaleLowerCase();
  $$("#files .file-row").forEach(
    (row) =>
      (row.hidden = !row
        .querySelector(".file-name")
        .textContent.toLocaleLowerCase()
        .includes(q)),
  );
}
function stepImage(delta) {
  const images = draft.files.filter(
    (f) => f.is_image && !draft.removed_files.includes(f.id),
  );
  if (!images.length) return;
  imageIndex = (imageIndex + delta + images.length) % images.length;
  $("#previewImage").src = images[imageIndex].url;
  $("#previewName").textContent =
    `${images[imageIndex].filename} · ${imageIndex + 1}/${images.length}`;
}
function initWorkspace() {
  $("#nav").insertAdjacentHTML(
    "afterbegin",
    '<button data-page="today"><span>☀</span>我的今天</button>',
  );
  $("[data-page=today]").onclick = () => navigate("today");
  titles.today = "我的今天";
  descriptions.today = "今天到期、已逾期和主动加入今日计划的任务。";
  $("#reminderGo").onclick = () => navigate("reminders");
  $("#dependencySearch").oninput = (e) =>
    $$("#taskDependencies label").forEach(
      (l) =>
        (l.hidden = !l.textContent
          .toLocaleLowerCase()
          .includes(e.target.value.toLocaleLowerCase())),
    );
  $("#attachmentSearch").oninput = filterAttachments;
  $("#previousImage").onclick = () => stepImage(-1);
  $("#nextImage").onclick = () => stepImage(1);
  $("#imageDialog").addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      stepImage(e.key === "ArrowLeft" ? -1 : 1);
    }
  });
  document.addEventListener("dragover", (e) => {
    if (e.dataTransfer.types.includes("Files")) e.preventDefault();
  });
  document.addEventListener(
    "drop",
    (e) => {
      if (e.dataTransfer.files.length) {
        e.preventDefault();
        if (draft && e.target.closest("#drawer")) {
          queueFiles([...e.dataTransfer.files]);
        } else notify("请先打开任务，再拖入附件。");
      }
    },
    true,
  );
  setInterval(autosaveDraft, 1500);
  setInterval(refreshReminders, 30000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshReminders();
  });
}

async function importDirectory(event) {
  const files = [...event.target.files];
  event.target.value = "";
  const databases = files.filter((f) => f.name === "tasks.sqlite3");
  if (databases.length !== 1)
    return notify("请选择包含唯一 tasks.sqlite3 的旧版 Data 目录。");
  const base = databases[0].webkitRelativePath.slice(
    0,
    -"tasks.sqlite3".length,
  );
  if (
    files.some(
      (f) => f.webkitRelativePath === base + "tasks.sqlite3-wal" && f.size,
    )
  )
    return notify(
      "检测到数据库 WAL 文件，请先关闭旧版并从旧版导出完整 ZIP 备份。",
    );
  const selected = files.filter(
    (f) =>
      f === databases[0] ||
      f.webkitRelativePath.startsWith(base + "attachments/"),
  );
  if (selected.reduce((n, f) => n + f.size, 0) > 100 * 1024 * 1024)
    return notify("目录超过 100 MB，请改用完整 ZIP 备份导入。");
  try {
    const payload = { files: [] };
    for (const f of selected)
      payload.files.push({
        name: f.webkitRelativePath.slice(base.length),
        data: await fileBase64(f),
      });
    const preview = await api("migration-preview", payload);
    if (
      !(await confirmAction(
        `已校验：任务 ${preview.tasks} 项，附件 ${preview.attachments} 个。导入会替换当前数据，系统先保存恢复前完整备份。`,
        "导入旧版数据",
        "备份并导入",
      ))
    )
      return;
    const result = await api("migration-import", payload);
    state = result.state;
    render();
    await loadDrafts();
    notify("旧版数据导入完成；恢复前副本：" + result.safety);
  } catch (e) {
    report(e);
  }
}
