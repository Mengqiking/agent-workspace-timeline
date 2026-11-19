"""两个宿主和网页共用的工作区任务存储。"""
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_WORKSPACE = "default"
EDITABLE = {"title", "description", "workspace", "executor", "status", "planned_start", "duration_minutes"}
PLANNING = {"backlog", "todo"}
EXECUTORS = {"codex", "deepseek", "manual"}


class StoreError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _text(value, name, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise StoreError(f"{name}须为不超过 {limit} 字的文本")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise StoreError(f"{name}含有无效字符") from None
    if required and not value.strip():
        raise StoreError(f"{name}不能为空")
    return value.strip() if required else value


def _utc_string(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _planned_time(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            raise ValueError
        return _utc_string(parsed)
    except (ValueError, OverflowError):
        raise StoreError("计划开始时间须为带时区的 ISO 8601 时间，清空时传 null") from None


def _positive(value, name):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise StoreError(f"{name}须为正整数")
    return value


def _fields(data, allowed):
    if not isinstance(data, dict):
        raise StoreError("请求须为 JSON 对象")
    if set(data) - allowed:
        raise StoreError("请求含有不支持的字段")
    return dict(data)


def _editable(data):
    for key, limit, label in (("title", 240, "标题"), ("workspace", 1024, "工作区"), ("description", 20000, "描述")):
        if key in data:
            data[key] = _text(data[key], label, limit, required=key != "description")
    if "status" in data and (not isinstance(data["status"], str) or data["status"] not in PLANNING):
        raise StoreError("编辑时可选待规划或待办，执行状态请记录实际事件")
    if "executor" in data and (not isinstance(data["executor"], str) or data["executor"] not in EXECUTORS):
        raise StoreError("执行者须为 codex、deepseek 或 manual")
    if "planned_start" in data:
        data["planned_start"] = _planned_time(data["planned_start"])
    if "duration_minutes" in data:
        _positive(data["duration_minutes"], "预计时长（分钟）")
        if data["duration_minutes"] > 525600:
            raise StoreError("单个任务的预计时长最多为 365 天")
    return data


class Store:
    def __init__(self, path=None):
        configured = path or os.environ.get("TIMELINE_DB") or Path.home() / ".agent-workspace-timeline/tasks.sqlite3"
        self.path = Path(configured).expanduser().resolve()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # 创建时即限制数据库权限，已有文件沿用其所有者设置。
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with closing(self._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    workspace TEXT NOT NULL, status TEXT NOT NULL, executor TEXT NOT NULL,
                    planned_start TEXT, duration_minutes INTEGER NOT NULL,
                    actual_start TEXT, actual_end TEXT, result TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS tasks_by_workspace ON tasks(workspace);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    event TEXT NOT NULL, timestamp TEXT NOT NULL, result TEXT NOT NULL,
                    executor TEXT NOT NULL, version INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_task ON events(task_id, id);
            """)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _task(conn, task_id):
        task_id = _text(task_id, "任务 ID", 128, True)
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError("找不到任务", 404)
        return dict(row)

    @staticmethod
    def _check_version(task, version):
        if task["version"] != _positive(version, "版本"):
            raise StoreError("任务已更新，请刷新并查看最新内容后再修改", 409)

    @staticmethod
    def _save(conn, task_id, values):
        # 字段只能来自上面的编辑白名单或内部状态转换。
        assignments = ", ".join(f"{key}=?" for key in values)
        conn.execute(f"UPDATE tasks SET {assignments}, version=version+1 WHERE id=?", (*values.values(), task_id))
        return Store._task(conn, task_id)

    def list_workspaces(self):
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT DISTINCT workspace FROM tasks ORDER BY workspace")
            return [DEFAULT_WORKSPACE, *(row[0] for row in rows if row[0] != DEFAULT_WORKSPACE)]

    def list_tasks(self, workspace=None):
        name = _text(DEFAULT_WORKSPACE if workspace is None else workspace, "工作区", 1024, True)
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE workspace=? ORDER BY rowid", (name,))
            return [dict(row) for row in rows]

    def get_task(self, task_id):
        with closing(self._connect()) as conn:
            return self._task(conn, task_id)

    def list_events(self, task_id):
        with closing(self._connect()) as conn:
            self._task(conn, task_id)
            rows = conn.execute("SELECT event,timestamp,result,executor,version FROM events WHERE task_id=? ORDER BY id", (task_id,))
            return [dict(row) for row in rows]

    def create_task(self, data):
        values = {"workspace": DEFAULT_WORKSPACE, "status": "backlog", "executor": "manual",
                  "duration_minutes": 60, **_fields(data, EDITABLE)}
        _editable(values)
        if "title" not in values:
            raise StoreError("缺少标题")
        task_id = str(uuid.uuid4())
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with closing(self._connect()) as conn, conn:
            conn.execute(f"INSERT INTO tasks (id,{columns}) VALUES (?,{placeholders})", (task_id, *values.values()))
            return self._task(conn, task_id)

    def update_task(self, task_id, data):
        values = _fields(data, EDITABLE | {"version"})
        version = values.pop("version", None)
        _editable(values)
        if not values:
            raise StoreError("至少提供一个要更新的字段")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = self._task(conn, task_id)
            self._check_version(previous, version)
            if previous["status"] == "running":
                if "status" in values or values.get("executor", previous["executor"]) != previous["executor"]:
                    raise StoreError("先报告本轮执行结果，再改状态或执行者", 409)
            if "status" in values and previous["status"] in {"done", "failed"}:
                values.update(actual_start=None, actual_end=None, result="")
            return self._save(conn, task_id, values)

    def record_event(self, task_id, data):
        values = _fields(data, {"event", "version", "result"})
        event = values.get("event")
        if event not in ("start", "complete", "fail"):
            raise StoreError("事件须为 start、complete 或 fail")
        result = _text(values.get("result", ""), "执行结果", 20000)
        if event == "start" and result:
            raise StoreError("请在完成或失败时填写执行结果")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = self._task(conn, task_id)
            self._check_version(previous, values.get("version"))
            running = previous["status"] == "running"
            if (event == "start" and running) or (event != "start" and not running):
                raise StoreError("事件与当前执行状态冲突，请刷新后核对", 409)
            now = _utc_string(datetime.now(timezone.utc))
            if event == "start":
                changes = {"status": "running", "actual_start": now, "actual_end": None, "result": ""}
            else:
                changes = {"status": "done" if event == "complete" else "failed", "actual_end": now, "result": result}
            task = self._save(conn, task_id, changes)
            conn.execute("INSERT INTO events(task_id,event,timestamp,result,executor,version) VALUES (?,?,?,?,?,?)",
                         (task_id, event, now, result, task["executor"], task["version"]))
            return task
