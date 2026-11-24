# scripts/ — 启动与维护脚本

| 脚本 | 用途 | 备注 |
|---|---|---|
| `start-bot.bat` | Windows 一键启动：清理 MCP 孤儿 → 注入 NO_PROXY → `python -m app.main` | 自动把仓库根定位为脚本目录，换机即用 |
| `probe-tcp.patch` | 历史补丁：cc-connect 的存活探测从 LLM ping 改成 TCP 探测 | **仅供参考**，对照上游源码看 |

启动/维护脚本留仓库根，运行产物只落三类目录：产物（`logs/`）、资源（`bot/`）、代码（`app/`）。
