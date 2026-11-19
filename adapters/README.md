# 接入 Codex 和 DeepSeek Harness

两个宿主用各自的 MCP 客户端启动同一份 `mcp_server.py`。把网页和两个进程放在同一台电脑上，使用同一个系统用户或相同的 `TIMELINE_DB` 绝对路径。路径含空格时，参数须作为一个完整字符串。所有进程使用 Python 3.9 或更高版本。

在仓库根目录运行 `python3 server.py` 即可打开共享网页。MCP 工具可以在网页关闭时继续读写任务。执行 `python3 -c 'import sys; print(sys.executable)'` 可取得解释器的绝对路径。

## Codex

在仓库根目录运行下面的命令，把本地服务器加入自己的 Codex 配置，然后重新打开会话。

```sh
codex mcp add timeline -- "$(python3 -c 'import sys; print(sys.executable)')" "$PWD/mcp_server.py"
codex mcp get timeline
```

使用自定义数据库时，注册时在 `--` 之前加入 `--env TIMELINE_DB=/数据库的绝对路径`。如需更换网页端口，两端同时设置 `TIMELINE_PORT`。Codex 配置和传输说明见 [官方 MCP 文档](https://developers.openai.com/codex/mcp/)。

## DeepSeek Harness

在 Web 或桌面配置的设置页面添加 MCP 服务。名称填 `timeline`，传输选择 `stdio`，命令填 Python 解释器的绝对路径，参数填 `mcp_server.py` 的绝对路径。在环境变量字段中按需设置共享数据库和端口。点击连接测试，可检查工具能否被发现。

也可在仓库根目录生成一个仅对本次启动生效的 profile 补丁。脚本会填入当前解释器和项目的绝对路径，并传递已设置的 `TIMELINE_DB`、`TIMELINE_PORT`。使用这种方式时，设置页面中应只保留一个同名实例。

```sh
timeline_patch="$(mktemp -d)/timeline.patch.yml"
python3 adapters/deepseek_patch.py > "$timeline_patch"
dsh web --patch "$timeline_patch" --dump-config
dsh web --patch "$timeline_patch"
```

配置示例和服务器管理行为见 [Harness MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md) 与 [Harness MCP 管理器](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-manager/README.md)。

## 验证同一张卡片

1. 在 Codex 对话中说「用 timeline 在演示工作区新建『回归测试』，计划 2026-11-21 15:00 +08:00 开始，预计 60 分钟」。
2. 在 Harness 对话中说「用 timeline 查演示工作区，将『回归测试』移到同日下午四点」，并检查返回的新版本。
3. 在任一端说「给我演示工作区的看板链接」，打开后能看到调整过的时间。页面按浏览器本地时区显示，工具结果使用 UTC。
4. 确实开始工作后，通过 `report_event` 上报 `start`。完成或失败时分别上报 `complete` 或 `fail`，可附结果摘要。改排期和填写执行者可以独立完成，到点运行由后续调度模块负责。

工具包含 `list_workspaces`、`list_tasks`、`get_task`、`create_task`、`update_task`、`report_event` 和 `board_link`。改卡和上报事件都需要当前 `version`。遇到 409 时重新读取，核对另一端已经保存的内容再提交。工具在本地 MCP 传输中交换 UTF-8 JSON，参见 [MCP 生命周期规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)。

## 首版宿主实测

2026 年 11 月 20 日，用临时数据库和隔离配置测试了 Codex 0.169.1 的 app-server 和 DeepSeek Harness 0.2.2-rc.5 的 Cordis 工具运行时、原生执行器及 MCP 客户端。两者均发现全部七个工具。Codex 建立北京时间 15:00 的任务，Harness 将同一卡片调整到 16:00；重启后的 Codex 回读版本 2 和 08:00 UTC。另起完整 Harness Web profile，验证首页响应 200 且宿主实际启动 Python MCP 子进程。此次验收直接调用宿主工具接口，日常桌面会话的自然语言选用工具、模型响应和自动执行不在此次实测范围。
