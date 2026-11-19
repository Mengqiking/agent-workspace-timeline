"use strict";
const $ = (selector) => document.querySelector(selector);
const statuses = {
  backlog: "待规划",
  todo: "待办",
  running: "进行中",
  done: "已完成",
  failed: "已失败",
};
const owners = { manual: "手动", deepseek: "DeepSeek Harness", codex: "Codex" };
const events = { start: "记录开始", complete: "记录完成", fail: "记录失败" };
const minute = 60_000;
const url = new URL(location.href);
let workspace = url.searchParams.get("workspace")?.trim() || "default";
let view = url.searchParams.get("view") === "timeline" ? "timeline" : "board";
let tasks = [];
let selectedTask = null;
let pendingEvent = null;
let drag = null;
let resizing = false;
let loading = 0;
let noticeTimer;
const taskForm = $("#task-form");
const eventForm = $("#event-form");
const dateInput = $("#day");
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const hourFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function element(tag, className, value) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (value !== undefined) el.textContent = value;
  return el;
}
function action(label, className, onClick) {
  const button = element("button", className, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}
function dateString(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function localDateTime(value) {
  if (!value) return "";
  const d = new Date(value);
  return `${dateString(d)}T${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function dayBounds() {
  const start = new Date(`${dateInput.value}T00:00:00`);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { start: +start, end: +end, minutes: (end - start) / minute };
}
function announce(message, field = $("#notice")) {
  field.textContent = message;
  field.hidden = !message;
  if (field === $("#notice")) {
    clearTimeout(noticeTimer);
    if (message)
      noticeTimer = setTimeout(() => {
        field.hidden = true;
      }, 7000);
  }
}
async function request(path, method = "GET", data) {
  let response;
  try {
    response = await fetch(path, {
      method,
      cache: "no-store",
      headers: data ? { "Content-Type": "application/json" } : {},
      body: data ? JSON.stringify(data) : undefined,
    });
  } catch {
    throw new Error("无法连接本机任务服务，请确认服务仍在运行。");
  }
  const payload = await response.json();
  if (!response.ok)
    throw new Error(
      response.status === 409
        ? "其他窗口已修改这项任务，请关闭编辑框并重新打开。"
        : payload.error || "操作失败，请重试。",
    );
  return payload;
}
async function refresh(force = false) {
  if (!force && (document.hidden || drag || resizing || $("dialog[open]")))
    return;
  const seq = ++loading;
  try {
    const data = await request(
      `/api/tasks?workspace=${encodeURIComponent(workspace)}`,
    );
    if (seq !== loading) return;
    if (!force && (drag || resizing || $("dialog[open]"))) return;
    if (force || JSON.stringify(data.tasks) !== JSON.stringify(tasks)) {
      tasks = data.tasks;
      render();
    }
  } catch (error) {
    if (seq === loading) announce(error.message);
  }
}
async function updateWorkspaces() {
  try {
    const { workspaces } = await request("/api/workspaces");
    $("#workspaces").replaceChildren(
      ...workspaces.map((name) => {
        const option = element("option");
        option.value = name;
        return option;
      }),
    );
  } catch {
    /* 当前工作区也可以手动输入。 */
  }
}
function saveURL() {
  const locationURL = new URL(location.href);
  locationURL.searchParams.set("workspace", workspace);
  locationURL.searchParams.set("view", view);
  history.replaceState(null, "", locationURL);
}
function render() {
  const running = tasks.filter((task) => task.status === "running").length;
  const unplanned = tasks.filter((task) => !task.planned_start).length;
  $("#summary").textContent =
    `${workspace} · ${tasks.length} 项任务，${running} 项进行中，${unplanned} 项未排期`;
  $("#page-title").textContent = view === "board" ? "任务看板" : "小时排期";
  document.title = `${$("#page-title").textContent} · 工作区排期`;
  $("#board").hidden = view !== "board";
  $("#timeline").hidden = view !== "timeline";
  $("#date-nav").hidden = view !== "timeline";
  $("#show-board").setAttribute("aria-pressed", String(view === "board"));
  $("#show-timeline").setAttribute("aria-pressed", String(view === "timeline"));
  if (view === "board") renderBoard();
  else renderTimeline();
}
function selectView(next) {
  view = next;
  saveURL();
  render();
  if (view === "timeline") scrollToSchedule();
}
function setDrag(node, task, fromBar = false) {
  node.draggable = true;
  node.addEventListener("dragstart", (event) => {
    if (resizing || event.target.closest(".handle")) {
      event.preventDefault();
      return;
    }
    const rect = node.getBoundingClientRect();
    let offset = 0;
    if (fromBar) {
      const bounds = dayBounds();
      const track = node.parentElement.getBoundingClientRect();
      offset = Math.max(
        0,
        ((event.clientX - rect.left) / track.width) * bounds.minutes,
      );
    }
    drag = { task, offset };
    event.dataTransfer.setData("text/plain", task.id);
    event.dataTransfer.effectAllowed = "move";
    node.classList.add("dragging");
  });
  node.addEventListener("dragend", () => {
    drag = null;
    node.classList.remove("dragging");
    document
      .querySelectorAll(".drop-target")
      .forEach((el) => el.classList.remove("drop-target"));
  });
}
function dropZone(node, onDrop) {
  node.addEventListener("dragover", (event) => {
    if (!drag) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    node.classList.add("drop-target");
  });
  node.addEventListener("dragleave", (event) => {
    if (!node.contains(event.relatedTarget))
      node.classList.remove("drop-target");
  });
  node.addEventListener("drop", (event) => {
    if (!drag) return;
    event.preventDefault();
    node.classList.remove("drop-target");
    const item = drag;
    drag = null;
    onDrop(item, event);
  });
}
async function patchTask(task, fields) {
  try {
    await request(`/api/tasks/${encodeURIComponent(task.id)}`, "PATCH", {
      version: task.version,
      ...fields,
    });
  } catch (error) {
    announce(error.message);
  }
  await refresh(true);
}
function taskCard(task) {
  const card = element("article", `task ${task.status}`);
  setDrag(card, task);
  const name = action(task.title, "card-link", () => editTask(task));
  name.title = "编辑任务";
  card.append(name);
  if (task.description)
    card.append(element("p", "description", task.description));
  const meta = element("div", "card-foot");
  meta.append(
    element("span", "owner", owners[task.executor]),
    element(
      "span",
      "",
      task.planned_start
        ? dateFormat.format(new Date(task.planned_start))
        : "未排期",
    ),
  );
  card.append(meta);
  if (task.result) card.append(element("p", "result", `结果 · ${task.result}`));
  const controls = element("div", "event-actions");
  const actions = task.status === "running" ? ["complete", "fail"] : ["start"];
  for (const kind of actions)
    controls.append(action(events[kind], "", () => confirmEvent(task, kind)));
  card.append(controls);
  return card;
}
function renderBoard() {
  const columns = Object.entries(statuses).map(([status, label]) => {
    const group = tasks.filter((t) => t.status === status);
    const lane = element(
      "section",
      `lane ${status}${group.length ? "" : " empty"}`,
    );
    const head = element("div", "lane-head");
    const name = element("h2", "", label);
    name.append(element("span", "", group.length));
    head.append(name);
    if (status === "backlog" || status === "todo") {
      const add = action("＋", "lane-add", () => editTask(null, status));
      add.setAttribute("aria-label", `新增${label}任务`);
      head.append(add);
    }
    const body = element("div", "lane-body");
    if (group.length) body.append(...group.map(taskCard));
    else body.append(element("span", "lane-empty", "暂无任务"));
    lane.append(head, body);
    dropZone(lane, ({ task }) => {
      if (status === task.status) return;
      if (status === "running" && task.status !== "running")
        return confirmEvent(task, "start");
      if (task.status === "running" && ["done", "failed"].includes(status))
        return confirmEvent(task, status === "done" ? "complete" : "fail");
      if (task.status !== "running" && ["todo", "backlog"].includes(status))
        return patchTask(task, { status });
      announce(
        "请先记录实际开始，再记录完成或失败。进行中的任务请先记录收尾。",
      );
    });
    return lane;
  });
  $("#board").replaceChildren(...columns);
}
function dropAtTime(track, bounds) {
  dropZone(track, ({ task, offset }, event) => {
    const rect = track.getBoundingClientRect();
    const raw =
      ((event.clientX - rect.left) / rect.width) * bounds.minutes - offset;
    const minutes = Math.max(
      0,
      Math.min(bounds.minutes - 15, Math.round(raw / 15) * 15),
    );
    const planned = new Date(bounds.start + minutes * minute).toISOString();
    patchTask(task, { planned_start: planned });
  });
}
function addResize(bar, track, task, bounds) {
  const handle = element("span", "handle");
  handle.setAttribute("aria-hidden", "true");
  handle.title = "拖动调整时长";
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    resizing = true;
    bar.draggable = false;
    handle.setPointerCapture(event.pointerId);
    const initialX = event.clientX;
    const width = track.getBoundingClientRect().width;
    const taskStart = Date.parse(task.planned_start);
    let length = task.duration_minutes;
    const move = (e) => {
      const delta = ((e.clientX - initialX) / width) * bounds.minutes;
      length = Math.max(
        1,
        Math.min(525600, task.duration_minutes + Math.round(delta / 15) * 15),
      );
      const visible =
        Math.min(bounds.end, taskStart + length * minute) -
        Math.max(bounds.start, taskStart);
      bar.style.width = `${(Math.max(0, visible) / minute / bounds.minutes) * 100}%`;
      handle.title = `${length} 分钟`;
    };
    const finish = (e) => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      resizing = false;
      bar.draggable = true;
      if (e.type === "pointercancel") {
        renderTimeline();
        return;
      }
      if (length !== task.duration_minutes)
        patchTask(task, { duration_minutes: length });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  });
  bar.append(handle);
}
function renderTimeline() {
  const bounds = dayBounds();
  const grid = $("#timegrid");
  const cols = Math.ceil(bounds.minutes / 60);
  grid.style.setProperty("--cols", String(cols));
  const axis = element("div", "time-row axis");
  const ticks = element("div", "ticks");
  for (let time = bounds.start; time < bounds.end; time += 60 * minute)
    ticks.append(element("span", "", hourFormat.format(new Date(time))));
  axis.append(element("div", "axis-label", "任务 / 本地时间"), ticks);
  const rows = [axis];
  const dated = tasks
    .filter((t) => t.planned_start)
    .map((t) => ({
      task: t,
      start: Date.parse(t.planned_start),
      end: Date.parse(t.planned_start) + t.duration_minutes * minute,
    }))
    .filter((t) => t.start < bounds.end && t.end > bounds.start)
    .sort((a, b) => a.start - b.start);
  for (const { task, start, end } of dated) {
    const row = element("div", "time-row");
    const label = element("div", "row-name");
    label.append(action(task.title, "card-link", () => editTask(task)));
    const track = element("div", "track");
    const bar = element("div", `slot ${task.status}`);
    const overlapStart = Math.max(bounds.start, start),
      overlapEnd = Math.min(bounds.end, end);
    bar.style.left = `${((overlapStart - bounds.start) / (bounds.end - bounds.start)) * 100}%`;
    bar.style.width = `${((overlapEnd - overlapStart) / (bounds.end - bounds.start)) * 100}%`;
    bar.title = `${task.title} · ${dateFormat.format(start)} 至 ${dateFormat.format(end)} · ${task.duration_minutes} 分钟 · ${statuses[task.status]}`;
    bar.append(
      action(`${hourFormat.format(start)} · ${task.title}`, "card-link", () =>
        editTask(task),
      ),
    );
    setDrag(bar, task, true);
    if (end <= bounds.end) addResize(bar, track, task, bounds);
    track.append(bar);
    dropAtTime(track, bounds);
    row.append(label, track);
    rows.push(row);
  }
  const dropRow = element("div", "time-row timeline-drop");
  const dropTrack = element("div", "track");
  dropAtTime(dropTrack, bounds);
  dropRow.append(element("div", "axis-label", "拖到此行安排时间"), dropTrack);
  rows.push(dropRow);
  grid.replaceChildren(...rows);
  const unplanned = tasks.filter((t) => !t.planned_start);
  $("#unscheduled-count").textContent = `${unplanned.length} 项`;
  $("#unscheduled").replaceChildren(
    ...(unplanned.length
      ? unplanned.map(taskCard)
      : [element("p", "hint", "当前工作区没有未排期任务。")]),
  );
  $("#timezone").textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
}
function scrollToSchedule() {
  requestAnimationFrame(() => {
    if (view !== "timeline") return;
    const { start, end } = dayBounds();
    const hours = tasks
      .filter((t) => t.planned_start)
      .map((t) => ({
        start: Date.parse(t.planned_start),
        end: Date.parse(t.planned_start) + t.duration_minutes * minute,
      }))
      .filter((t) => t.start < end && t.end > start)
      .map((t) => Math.max(0, (t.start - start) / minute / 60));
    const tick = $(".ticks span");
    const firstHour = hours.length ? Math.min(...hours) : 9;
    if (tick)
      $("#timeline-scroll").scrollLeft = Math.max(
        0,
        (Math.floor(firstHour) - 1) * tick.getBoundingClientRect().width,
      );
  });
}
function changeDay(offset) {
  const date = new Date(`${dateInput.value}T12:00:00`);
  date.setDate(date.getDate() + offset);
  dateInput.value = dateString(date);
  renderTimeline();
  scrollToSchedule();
}
function editTask(task = null, initialStatus = "backlog") {
  selectedTask = task;
  const fields = taskForm.elements;
  $("#dialog-title").textContent = task ? "编辑任务" : "新建任务";
  fields.title.value = task?.title || "";
  fields.description.value = task?.description || "";
  fields.executor.value = task?.executor || "manual";
  fields.executor.disabled = task?.status === "running";
  const status = task?.status || initialStatus;
  const options =
    status === "running"
      ? ["running"]
      : [...new Set([status, "backlog", "todo"])];
  fields.status.replaceChildren(
    ...options.map((s) => {
      const option = element("option", "", statuses[s]);
      option.value = s;
      return option;
    }),
  );
  fields.status.value = status;
  fields.status.disabled = status === "running";
  fields.planned_start.value = localDateTime(task?.planned_start);
  fields.duration_minutes.value = task?.duration_minutes || 60;
  const actual = [];
  if (task?.actual_start)
    actual.push(`实际开始　${dateFormat.format(new Date(task.actual_start))}`);
  if (task?.actual_end)
    actual.push(`实际结束　${dateFormat.format(new Date(task.actual_end))}`);
  if (task?.result) actual.push(`结果摘要　${task.result}`);
  $("#actual").textContent = actual.join("\n");
  $("#actual").hidden = !actual.length;
  $("#history").open = false;
  $("#history").hidden = !task;
  $("#history-list").replaceChildren();
  announce("", $("#dialog-error"));
  $("#task-dialog").showModal();
  fields.title.focus();
}
function confirmEvent(task, kind) {
  pendingEvent = { task, kind };
  $("#event-title").textContent = events[kind];
  $("#event-description").textContent =
    kind === "start"
      ? `请确认「${task.title}」已经开始执行。本操作只记录实际开始时间。`
      : `将「${task.title}」标记为${kind === "complete" ? "已完成" : "已失败"}并记录实际结束时间。`;
  eventForm.elements.result.value = "";
  $("#result-label").hidden = kind === "start";
  announce("", $("#event-error"));
  $("#event-dialog").showModal();
}
$("#history").addEventListener("toggle", async () => {
  if (!$("#history").open || !selectedTask) return;
  const id = selectedTask.id;
  const list = $("#history-list");
  list.replaceChildren(element("li", "", "正在读取…"));
  try {
    const data = await request(`/api/tasks/${encodeURIComponent(id)}/events`);
    if (selectedTask?.id !== id) return;
    const entries = data.events
      .slice()
      .reverse()
      .map((entry) => {
        const label = events[entry.event] || entry.event;
        const detail = [
          dateFormat.format(new Date(entry.timestamp)),
          owners[entry.executor],
          label,
          entry.result,
        ].filter(Boolean);
        return element("li", "", detail.join(" · "));
      });
    list.replaceChildren(
      ...(entries.length ? entries : [element("li", "", "尚无执行记录。")]),
    );
  } catch (error) {
    if (selectedTask?.id === id)
      list.replaceChildren(element("li", "", error.message));
  }
});
taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const save = taskForm.querySelector("[type=submit]");
  if (save.disabled) return;
  const fields = taskForm.elements;
  const task = selectedTask;
  const data = {
    title: fields.title.value.trim(),
    description: fields.description.value,
    executor: fields.executor.value,
    duration_minutes: Number(fields.duration_minutes.value),
    planned_start: fields.planned_start.value
      ? new Date(fields.planned_start.value).toISOString()
      : null,
  };
  if (!data.title) {
    announce("请填写任务名称。", $("#dialog-error"));
    return;
  }
  if (!task || fields.status.value !== task.status)
    data.status = fields.status.value;
  save.disabled = true;
  try {
    if (task) {
      if (task.status === "running") delete data.executor;
      await request(`/api/tasks/${encodeURIComponent(task.id)}`, "PATCH", {
        ...data,
        version: task.version,
      });
    } else await request("/api/tasks", "POST", { ...data, workspace });
    $("#task-dialog").close();
    await refresh(true);
    await updateWorkspaces();
  } catch (error) {
    announce(error.message, $("#dialog-error"));
    await refresh(true);
  } finally {
    save.disabled = false;
  }
});
eventForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const save = eventForm.querySelector("[type=submit]");
  if (save.disabled || !pendingEvent) return;
  const { task, kind } = pendingEvent;
  save.disabled = true;
  try {
    const data = { event: kind, version: task.version };
    if (kind !== "start") data.result = eventForm.elements.result.value.trim();
    await request(
      `/api/tasks/${encodeURIComponent(task.id)}/events`,
      "POST",
      data,
    );
    $("#event-dialog").close();
    await refresh(true);
  } catch (error) {
    announce(error.message, $("#event-error"));
    await refresh(true);
  } finally {
    save.disabled = false;
  }
});
$("#workspace-input").value = workspace;
$("#workspace-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("#workspace-input").value.trim();
  if (!name || name === workspace) return;
  workspace = name;
  tasks = [];
  saveURL();
  render();
  await refresh(true);
  await updateWorkspaces();
  if (view === "timeline") scrollToSchedule();
});
$("#new-task").addEventListener("click", () => editTask());
$("#show-board").addEventListener("click", () => selectView("board"));
$("#show-timeline").addEventListener("click", () => selectView("timeline"));
$("#previous-day").addEventListener("click", () => changeDay(-1));
$("#next-day").addEventListener("click", () => changeDay(1));
$("#today").addEventListener("click", () => {
  dateInput.value = dateString(new Date());
  renderTimeline();
  scrollToSchedule();
});
dateInput.addEventListener("change", () => {
  if (!dateInput.value) dateInput.value = dateString(new Date());
  renderTimeline();
  scrollToSchedule();
});
for (const btn of document.querySelectorAll("button.close"))
  btn.addEventListener("click", () => btn.closest("dialog").close());
dateInput.value = dateString(new Date());
updateWorkspaces();
refresh(true).then(() => {
  if (view === "timeline") scrollToSchedule();
});
setInterval(refresh, 5000);
window.addEventListener("focus", () => refresh());
