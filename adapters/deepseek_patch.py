"""输出 DeepSeek Harness Web profile 的附加配置。输出采用 YAML 支持的 JSON 语法。"""
import json
import os
import sys
from pathlib import Path


def main():
    script = Path(__file__).resolve().parents[1] / "mcp_server.py"
    env = {}
    if os.environ.get("TIMELINE_DB"):
        env["TIMELINE_DB"] = str(Path(os.environ["TIMELINE_DB"]).expanduser().resolve())
    if os.environ.get("TIMELINE_PORT"):
        env["TIMELINE_PORT"] = os.environ["TIMELINE_PORT"]
    config = {"serverName": "timeline", "transport": "stdio", "command": sys.executable,
              "args": [str(script)], "env": env, "failOnStartupError": True}
    entry = {"id": "workspace-timeline-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": config}
    print(json.dumps([{"insert": [entry]}], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
