"""本机任务看板的 HTTP 入口。"""
import argparse
import json
import mimetypes
import os
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from store import Store, StoreError


WEB = Path(__file__).resolve().parent / "web"
BODY_LIMIT = 256 * 1024


class BoardHandler(BaseHTTPRequestHandler):
    def _reply(self, status, content, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(content)

    def _json(self, data, status=200):
        self._reply(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _check_origin(self):
        port = self.server.server_port
        authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            authorities.update({"127.0.0.1", "localhost"})
        host = self.headers.get("Host", "").lower()
        if host not in authorities:
            raise StoreError("请求地址须为当前本机服务", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{host}":
            raise StoreError("只允许同源请求", 403)
        if self.command in ("POST", "PATCH") and self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise StoreError("只允许同源请求", 403)

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise StoreError("请使用 application/json", 415)
        if self.headers.get("Transfer-Encoding"):
            raise StoreError("请提供固定长度的 JSON 请求")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise StoreError("缺少有效的请求长度", 411) from None
        if length < 1 or length > BODY_LIMIT:
            raise StoreError("请求体为空或过大", 413)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise StoreError("JSON 格式有误") from None

    def _static(self, path):
        relative = unquote(path[1:]) if path != "/" else "index.html"
        resource = (WEB / relative).resolve()
        if WEB not in resource.parents or not resource.is_file() or "\x00" in relative:
            raise StoreError("找不到页面", 404)
        mime = mimetypes.guess_type(resource.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime == "application/javascript":
            mime += "; charset=utf-8"
        self._reply(200, resource.read_bytes(), mime)

    def _route(self):
        self._check_origin()
        address = urlsplit(self.path)
        path = address.path
        store = self.server.store
        if address.netloc or address.scheme or not path.startswith("/"):
            raise StoreError("请求地址无效")
        if path == "/api/tasks":
            if self.command == "GET":
                workspace = parse_qs(address.query, keep_blank_values=True).get("workspace", [None])[0]
                return self._json({"tasks": store.list_tasks(workspace)})
            if self.command == "POST":
                return self._json({"task": store.create_task(self._body())}, 201)
        elif path == "/api/workspaces" and self.command == "GET":
            return self._json({"workspaces": store.list_workspaces()})
        elif path.startswith("/api/tasks/"):
            parts = path.strip("/").split("/")
            task_id = parts[2] if len(parts) >= 3 else ""
            if len(parts) == 3:
                if self.command == "GET":
                    return self._json({"task": store.get_task(task_id)})
                if self.command == "PATCH":
                    return self._json({"task": store.update_task(task_id, self._body())})
            if len(parts) == 4 and parts[3] == "events":
                if self.command == "GET":
                    return self._json({"events": store.list_events(task_id)})
                if self.command == "POST":
                    return self._json({"task": store.record_event(task_id, self._body())})
        elif self.command == "GET" and not path.startswith("/api/"):
            if "\x00" in unquote(path):
                raise StoreError("请求地址无效")
            return self._static(path)
        raise StoreError("请求路径或方法不存在", 404)

    def _handle(self):
        try:
            self._route()
        except StoreError as exc:
            self._json({"error": str(exc)}, exc.status)
        except sqlite3.OperationalError:
            self._json({"error": "数据库暂时不可用，请稍后刷新"}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PATCH = _handle


def make_server(port=8765, db=None):
    store = Store(db)
    server = ThreadingHTTPServer(("127.0.0.1", port), BoardHandler)
    server.store = store
    return server


def main():
    parser = argparse.ArgumentParser(description="启动本机工作区任务看板")
    parser.add_argument("--port", type=int, default=os.environ.get("TIMELINE_PORT", "8765"), help="本机端口（默认 8765）")
    parser.add_argument("--db", help="SQLite 文件路径，默认使用 TIMELINE_DB 或用户数据目录")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口须在 0 至 65535 之间")
    with make_server(args.port, args.db) as server:
        print(f"任务看板已启动 http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
