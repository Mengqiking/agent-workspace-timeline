"""本地看板的 stdio MCP 入口，每行传输一条 JSON-RPC 消息。"""
import json
import os
import sqlite3
import sys
from urllib.parse import urlencode

from store import DEFAULT_WORKSPACE, Store, StoreError

# 两个宿主均支持此组有状态 stdio 协议，较新客户端可在初始化时协商降级。
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TEXT = {"type": "string"}
ID = {"type": "string", "minLength": 1, "description": "任务 ID，由创建或查询返回。"}
VERSION = {"type": "integer", "minimum": 1, "description": "最新读取的任务版本。冲突时重新读取。"}
WORKSPACE = {"type": "string", "minLength": 1, "description": "工作区名称，省略时使用 default。"}
TASK = {
    "title": {"type": "string", "minLength": 1, "maxLength": 240},
    "description": TEXT,
    "workspace": WORKSPACE,
    "status": {"type": "string", "enum": ["backlog", "todo"]},
    "executor": {"type": "string", "enum": ["manual", "codex", "deepseek"]},
    "planned_start": {"type": ["string", "null"], "description": "带时区的 ISO 8601 时间，null 表示清空排期。"},
    "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 525600},
}


def tool(name, description, properties, required=()):
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return {"name": name, "description": description, "inputSchema": schema}


TOOLS = [
    tool("board_link", "返回工作区本地网页地址。先在服务所在电脑启动 server.py。", {"workspace": WORKSPACE}),
    tool("list_workspaces", "列出工作区名称。", {}),
    tool("list_tasks", "列出一个工作区的任务、排期、实际执行时间和当前版本。", {"workspace": WORKSPACE}),
    tool("get_task", "读取任务和历次执行上报记录。", {"id": ID}, ["id"]),
    tool("create_task", "新建任务，可填写计划时间、时长和执行者。创建后由用户或宿主另行执行。", TASK, ["title"]),
    tool("update_task", "调整已有任务的资料或排期。只传需要修改的字段。", {"id": ID, "version": VERSION, **TASK}, ["id", "version"]),
    tool("report_event", "实际开始工作时上报 start，实际结束时上报 complete 或 fail，可附结果。由调用方据实上报。",
         {"id": ID, "version": VERSION, "event": {"type": "string", "enum": ["start", "complete", "fail"]},
          "result": TEXT}, ["id", "version", "event"]),
]
SCHEMAS = {entry["name"]: entry["inputSchema"] for entry in TOOLS}


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def invoke(store, name, arguments):
    schema = SCHEMAS.get(name)
    if schema is None:
        raise ProtocolError(-32602, "不存在这个工具")
    if not isinstance(arguments, dict):
        raise ProtocolError(-32602, "工具参数必须是对象")
    if set(arguments) - schema["properties"].keys() or not set(schema.get("required", [])).issubset(arguments):
        raise StoreError("工具参数含未知字段或缺少必填字段")
    data = dict(arguments)
    task_id = data.pop("id", None)
    if "id" in arguments and (not isinstance(task_id, str) or not task_id.strip()):
        raise StoreError("需要有效的任务 ID")
    if name == "list_workspaces":
        return {"workspaces": store.list_workspaces()}
    if name == "list_tasks":
        if "workspace" in data and data["workspace"] is None:
            raise StoreError("工作区须为非空字符串")
        return {"tasks": store.list_tasks(data.get("workspace", DEFAULT_WORKSPACE))}
    if name == "get_task":
        task = store.get_task(task_id)
        # 读记录期间可能已有另一个进程更新任务，只返回本次版本包含的事件。
        events = [event for event in store.list_events(task_id) if event["version"] <= task["version"]]
        return {"task": task, "events": events}
    if name == "create_task":
        return store.create_task(data)
    if name == "update_task":
        return store.update_task(task_id, data)
    if name == "report_event":
        return store.record_event(task_id, data)
    workspace = data.get("workspace", DEFAULT_WORKSPACE)
    if not isinstance(workspace, str) or not workspace.strip() or len(workspace) > 1024:
        raise StoreError("工作区须为 1 至 1024 个字符")
    port = int(os.environ.get("TIMELINE_PORT", "8765"))
    if not 1 <= port <= 65535:
        raise ValueError("TIMELINE_PORT 须为有效端口")
    return {"url": f"http://127.0.0.1:{port}/?" + urlencode({"workspace": workspace.strip()}),
            "usage": "请在运行看板服务的电脑打开网页。", "auto_dispatch": False}


class Session:
    def __init__(self, store):
        self.store = store
        self.initialized = False

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise ProtocolError(-32600, "需要 JSON-RPC 2.0 请求对象")
        if "id" not in request:
            return None
        request_id = request["id"]
        if type(request_id) not in (str, int):
            raise ProtocolError(-32600, "请求 ID 必须是字符串或整数")
        method = request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolError(-32602, "参数须为对象")
        if method == "initialize":
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                raise ProtocolError(-32602, "需要指定协议版本")
            self.initialized = True
            result = {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "agent-workspace-timeline", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            raise ProtocolError(-32002, "需要先初始化会话")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise ProtocolError(-32602, "需要指定工具名称")
            try:
                value = invoke(self.store, name, params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except StoreError as exc:
                result = {"isError": True, "content": [{"type": "text", "text": f"{exc.status} {exc}"}]}
        else:
            raise ProtocolError(-32601, "不支持的协议方法")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    session = Session(Store())
    for line in sys.stdin.buffer:
        request = None
        try:
            request = json.loads(line)
            response = session.handle(request)
        except (ValueError, RecursionError):
            code = -32700 if request is None else -32603
            response = error_response(request, code, "无法解析请求" if code == -32700 else "本地设置无效")
        except ProtocolError as exc:
            response = error_response(request, exc.code, str(exc))
        except (OSError, sqlite3.Error):
            response = error_response(request, -32603, "本地存储不可用")
        if response is not None:
            print(json.dumps(response, ensure_ascii=True), flush=True)


def error_response(request, code, message):
    request_id = request.get("id") if isinstance(request, dict) else None
    return {"jsonrpc": "2.0", "id": request_id if type(request_id) in (str, int) else None,
            "error": {"code": code, "message": message}}


if __name__ == "__main__":
    main()
