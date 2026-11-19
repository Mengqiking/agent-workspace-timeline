"""验证跨端共享、计划与执行分离、并发写入和本机 HTTP 契约。"""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

from server import make_server
from store import Store, StoreError


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name, "tasks.db")
        self.store = Store(self.path)

    def test_workspace_views_share_one_persistent_task(self):
        self.assertEqual(self.store.list_tasks(), [])
        task = self.store.create_task({"title": "测试", "workspace": "开发", "executor": "deepseek"})
        self.assertEqual(task["version"], 1)
        self.assertIsNone(task["planned_start"])
        second = Store(self.path)
        update = second.update_task(task["id"], {"planned_start": "2026-11-20T15:00:00+08:00", "version": 1})
        self.assertEqual(update["planned_start"], "2026-11-20T07:00:00.000Z")
        self.assertEqual(self.store.list_tasks("开发"), [update])
        self.assertEqual(self.store.list_workspaces(), ["default", "开发"])
        self.assertEqual(self.store.list_tasks("其他工作区"), [])
        self.assertEqual(update["status"], "backlog")
        self.assertIsNone(update["actual_start"])
        self.assertEqual(second.list_events(task["id"]), [])
        cleared = second.update_task(task["id"], {"planned_start": None, "version": 2})
        self.assertIsNone(cleared["planned_start"])

    def test_invalid_task_fields_leave_store_unchanged(self):
        for data in ({"title": " "}, {"title": chr(0xD800)}, {"title": "甲", "duration_minutes": True},
                     {"title": "甲", "duration_minutes": 0}, {"title": "甲", "executor": []},
                     {"title": "甲", "planned_start": "2026-11-20T16:00"},
                     {"title": "甲", "status": "running"}, {"title": "甲", "actual_start": "伪造"}):
            with self.subTest(data=data), self.assertRaises(StoreError):
                self.store.create_task(data)
        self.assertEqual(self.store.list_tasks(), [])

    def test_racing_plan_updates_do_not_overwrite(self):
        task = self.store.create_task({"title": "测试"})
        barrier = threading.Barrier(2)

        def edit(title):
            client = Store(self.path)
            barrier.wait(timeout=3)
            try:
                return client.update_task(task["id"], {"version": 1, "title": title})["version"]
            except StoreError as exc:
                return exc.status

        with ThreadPoolExecutor(2) as clients:
            self.assertCountEqual(clients.map(edit, ["修改甲", "修改乙"]), [2, 409])
        self.assertEqual(self.store.get_task(task["id"])["version"], 2)

    def test_report_failed_retry_then_complete_preserves_history(self):
        task_id = self.store.create_task({"title": "集成测试"})["id"]
        def report(event, version, result=""):
            return self.store.record_event(task_id, {"event": event, "version": version, "result": result})
        with self.assertRaises(StoreError):
            report("complete", 1)
        active = report("start", 1)
        self.assertIsNotNone(active["actual_start"])
        with self.assertRaises(StoreError) as stale:
            report("start", 1)
        self.assertEqual(stale.exception.status, 409)
        with self.assertRaises(StoreError):
            self.store.update_task(task_id, {"version": 2, "status": "todo"})
        failed = report("fail", 2, "服务离线")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["result"], "服务离线")
        queued = self.store.update_task(task_id, {"version": 3, "status": "todo"})
        self.assertIsNone(queued["actual_start"])
        report("start", 4)
        done = report("complete", 5, "通过")
        self.assertIsNotNone(done["actual_end"])
        self.assertEqual(done["status"], "done")
        self.assertEqual([e["event"] for e in self.store.list_events(task_id)], ["start", "fail", "start", "complete"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = make_server(0, Path(tmp.name, "test.db"))
        self.port = server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

    def request(self, method, path, data=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            body = json.dumps(data).encode() if data is not None else None
            conn.request(method, path, body, {"Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_full_http_path(self):
        status, response = self.request("POST", "/api/tasks", {"title": "文档", "workspace": "docs"},
                                        {"Origin": self.origin})
        self.assertEqual(status, 201)
        task_id = json.loads(response)["task"]["id"]
        self.assertEqual(len(json.loads(self.request("GET", "/api/tasks?workspace=docs")[1])["tasks"]), 1)
        self.assertEqual(json.loads(self.request("GET", "/api/tasks")[1])["tasks"], [])
        item = "/api/tasks/" + task_id
        self.assertEqual(self.request("PATCH", item, {"duration_minutes": 30, "version": 1})[0], 200)
        self.assertEqual(self.request("PATCH", item, {"duration_minutes": 45, "version": 1})[0], 409)
        self.assertEqual(self.request("POST", item + "/events", {"event": "start", "version": 2})[0], 200)
        self.assertEqual(self.request("POST", item + "/events", {"event": "complete", "version": 3})[0], 200)
        self.assertEqual(len(json.loads(self.request("GET", item + "/events")[1])["events"]), 2)
        self.assertEqual(self.request("GET", "/api/tasks/missing")[0], 404)

    def test_rejects_foreign_origin_host_and_traversal(self):
        task = {"title": "禁止跨站写入"}
        for headers in ({"Origin": "https://example.com"}, {"Host": "example.com"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/tasks", task, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/tasks", task, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("GET", "/%2e%2e/store.py")[0], 404)
        self.assertEqual(json.loads(self.request("GET", "/api/tasks")[1])["tasks"], [])


if __name__ == "__main__":
    unittest.main()
