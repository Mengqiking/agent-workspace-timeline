"""对真实 stdio 子进程做协议与跨会话数据验证。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, resources, database):
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_server.py")],
            env={**os.environ, "TIMELINE_DB": str(database), "TIMELINE_PORT": "8765"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        )
        resources.callback(self.close)
        self.counter = 0

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()

    def request(self, method, params):
        self.counter += 1
        request = {"jsonrpc": "2.0", "id": self.counter, "method": method, "params": params}
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def initialize(self, version):
        result = self.request("initialize", {"protocolVersion": version, "clientInfo": {"name": "验收", "version": "1"},
                                             "capabilities": {}})["result"]
        self.process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()
        return result

    def tool(self, name, **arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def value(self, name, **arguments):
        result = self.tool(name, **arguments)
        if result.get("isError"):
            raise AssertionError(result["content"])
        return json.loads(result["content"][0]["text"])


class McpTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.db = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "tasks.sqlite3"

    def test_cross_host_schedule_and_events(self):
        a, b = (Client(self.stack, self.db) for _ in range(2))
        self.assertEqual(a.initialize("2025-06-18")["protocolVersion"], "2025-06-18")
        self.assertEqual(b.initialize("2099-01-01")["protocolVersion"], "2025-11-25")
        tools = a.request("tools/list", {})["result"]["tools"]
        self.assertEqual(len(tools), 7)
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))

        work = "测试 环境"
        task = a.value("create_task", title="联测", workspace=work, executor="deepseek")
        self.assertEqual(b.value("list_tasks", workspace=work)["tasks"][0]["id"], task["id"])
        self.assertIn(work, b.value("list_workspaces")["workspaces"])
        new = b.value("update_task", id=task["id"], version=task["version"], planned_start="2026-11-21T16:00:00+08:00")
        self.assertIsNone(new["actual_start"])
        conflict = a.tool("update_task", id=task["id"], version=task["version"], duration_minutes=90)
        self.assertTrue(conflict["isError"])
        self.assertIn("409", conflict["content"][0]["text"])
        start = a.value("report_event", id=task["id"], version=new["version"], event="start")
        b.value("report_event", id=task["id"], version=start["version"], event="complete", result="已验证")
        observed = a.value("get_task", id=task["id"])
        self.assertEqual([e["event"] for e in observed["events"]], ["start", "complete"])
        self.assertEqual(observed["task"]["result"], "已验证")
        self.assertEqual(b.value("board_link", workspace=work)["url"],
                         "http://127.0.0.1:8765/?workspace=%E6%B5%8B%E8%AF%95+%E7%8E%AF%E5%A2%83")

    def test_reject_invalid_protocol_and_arguments(self):
        client = Client(self.stack, self.db)
        self.assertIn("error", client.request("tools/list", {}))
        client.initialize("2025-11-25")
        self.assertEqual(client.request("tools/promote", {})["error"]["code"], -32601)
        self.assertEqual(client.request("tools/call", {"name": "absent"})["error"]["code"], -32602)
        self.assertTrue(client.tool("get_task", id=[])["isError"])
        self.assertTrue(client.tool("create_task", title="不可写字段", actual_start="now")["isError"])
        self.assertEqual(client.value("list_tasks")["tasks"], [])


if __name__ == "__main__":
    unittest.main()
