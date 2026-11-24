# scripts/ — 运维与启动脚本

| 脚本 | 用途 | 备注 |
|---|---|---|
| `start-bot.bat` | Windows 一键启动（清 MCP 孤儿 → 注入 NO_PROXY → `python -m app.main`） | 自动把仓库根定位为脚本目录的上一级 |
| `start-external.bat` | 拉起外部能力服务（本机 :8000） | 路径写的是另一个仓库，按需改 |
| `probe-tcp.patch` | 历史补丁留档：cc-connect 健康探测由 LLM ping 改 TCP 探活 | **已落地**，仅作事故溯源 |

启动/运维脚本不放仓库根：根目录只留「配置 + 单源资产（bot/）+ 代码（app/）」三类。
