# 开发规范 — feishu-agent-platform × dsh（重构期适用）

> 适用范围：`app/` 下所有新代码，以及迁移期被改动的一侧。
> **本文只写「代码里读不出来的约定」**——能从 `ruff.toml` / `mypy.ini` 读到的规则不在这里重复。
> 违反条款时：能在 CI/门禁里机器拦的，一律写进门禁；拦不住的才靠 review。

## 一、分层与依赖方向

```
app/gateway/       飞书接入、aggregator、HTTP API           ← 只依赖 core + protocols
app/core/          新增：跨面共享的纯逻辑（无 IO 实现细节）
app/dsh/           新增：全仓唯一可触碰 ACP 协议的模块
app/mcp/           新增：MCP server 实现（feishu / memory / domain engines）
app/agents/        能力元数据（agent.yaml 加载），不含执行循环
app/guardrails/    并发闸、审批、扫描、审计               ← 任何人可依赖
app/observability/ 任务档案、控制台服务                   ← 只读 logs/ 与 session 产物
app/domains/       业务域（daily / webportal / prd_watch / cmdb_watch）← 不允许反向依赖 gateway
```

**铁律**

| 编号 | 规则 | 违反后果 |
|---|---|---|
| L1 | 依赖只能**向下**（gateway → core → domains），`domains/` 不得 import `gateway/` 或 `dsh/` | 业务域变成入口耦合，无法单测 |
| L2 | **只有 `app/dsh/` 可 import ACP 协议细节**（JSON-RPC 报文、session id、update 类型） | 协议升级时改动点散落全仓 |
| L3 | **只有 `app/mcp/` 可实现 MCP 工具**，工具的业务逻辑来自 `app/domains/` | 工具与业务逻辑双写 |
| L4 | `app/orchestrator/` 与 `app/agents/handlers/` 处于**冻结**状态（只修致命 bug，不加功能） | 迁移期双写漂移 |

## 二、模块契约

每个新模块（一个目录）必须同时具备：

1. **模块 docstring**：这个模块负责什么 / 不负责什么 / 调用方是谁。三行以内。
2. **入口收窄**：对外的函数/类不超过 5 个，其余以 `_` 前缀私有。
3. **失败语义声明**：抛异常还是返回错误值，二选一并写在 docstring。同一模块内不得混用。

禁止：模块级可变全局状态（`process` 级单例除外，且必须提供 `reset()` 供测试）。

## 三、错误处理

| 场景 | 做法 |
|---|---|
| 外部 IO（HTTP / 子进程 / 文件） | 必须 catch，转为模块内定义的错误类型或返回空值 + 记日志 |
| 后台循环（watcher / poller / supervisor） | **必须 catch 全部异常**——循环因异常退出就再也不会回来，用户只看到「从此不响了」 |
| 协议层（ACP / MCP） | 不允许吞：要么向上抛，要么显式转为用户可见的失败回执 |
| 用户可见失败 | 必须带「发生了什么 + 下一步做什么」两段，不允许只回「失败」 |

## 四、异步

- 所有 IO 用 `async`；**禁止在事件循环里做同步阻塞调用**（`subprocess.run`、`requests`、`time.sleep`）。需要时用 `asyncio.to_thread`。
- 后台任务必须可取消：接受 `asyncio.Event` 或 `CancelledError` 支持，`finally` 里清理。
- 超时显式化：所有跨进程调用必须带超时，不得依赖上游默认值。

## 五、数据模型

- 跨模块传递的结构用 `@dataclass(frozen=True)`；需要校验的外部输入用 pydantic。
- **禁止裸 dict 跨模块传参**（现状 `state: dict[str, Any]` 是反面样本，迁移期不新增）。
- 枚举用 `enum.StrEnum`，不用裸字符串常量。

## 六、日志

- `logging.getLogger("<面>.<模块>")`，如 `dsh.adapter`、`mcp.feishu`、`domains.prd_watch`。
- **INFO 写状态变化，DEBUG 写过程细节**。循环里的每轮采样用 DEBUG。
- 禁止在日志里打印凭据、完整报文、用户原文（原文超 200 字截断）。

## 七、测试

| 层 | 要求 | 怎么验 |
|---|---|---|
| 纯逻辑（`core/`、`domains/` 的判定） | 必须有单测，**不依赖网络与文件系统** | `pytest` |
| 协议适配（`dsh/`、`mcp/`） | 必须有契约测试，用录制报文或本地 stub | `pytest` + 录制夹具 |
| 端到端 | 由 `verify_spec.py` 按 spec AC 断言 | `python app/eval/verify_spec.py` |
| golden 回归 | 路由与安全用例迁移后必须重跑 | `router_intent_eval.py`、`prompt_eval.py` |

**测试命名**：`test_<被测行为>_<场景>_<期望>`，例 `test_recall_gateway_500_returns_empty`。

## 八、门禁（改动收尾必走）

```bash
python -m ruff check <改动文件> --select F821,F811,F401,F541,F841 --fix
python -X utf8 -m mypy <改动文件>
python -m py_compile <改动文件>
python -X utf8 -m app.eval.comment_lint <改动文件>
```

**F821/F811/E4/E7/E9 必须为零**；`mypy` 新增错误必须为零（存量豁免见 `mypy.ini`）。
新增模块**不得**新增 mypy 豁免段——豁免是给存量还债的，不是给新代码开后门。

## 九、注释纪律

注释只解释「**为什么代码必须这样**」。禁止写：

- 日期、事件编号、「用户要求」、「原来是 X 现改为 Y」→ 属 git log
- 「待优化」「TODO 后续」→ 要么现在做，要么开 issue，不写在代码里
- 复述代码在做什么（`# 遍历列表`）

必须写：非显然的边界条件、绕开某个 bug 的原因、性能取舍。

## 十、设计模式约束

| 该用 | 场景 |
|---|---|
| 协议适配器（Adapter） | `app/dsh/adapter.py`：把 ACP 报文转换为内部事件类型，全仓唯一转换点 |
| 端口与适配器（Hexagonal） | `app/domains/` 定义能力接口，`app/mcp/` 实现，`app/gateway/` 消费 |
| 注册表（Registry） | 技能、插件、MCP 工具的自动发现；**不新写**，用 dsh 原生的 `dsh-skill` / `dsh-mcp-client` |
| 状态机 | 任务生命周期、审批流；状态转移必须集中在一处，禁止散落 `if` |

**不该用**（迁移期明确禁止）：

- 为「以后可能扩展」预留抽象基类——只有一个实现时不要建 ABC
- 事件总线替代直接调用——除非确有 ≥3 个消费者
- 自研重试/限流/缓存——dsh 已有 `dsh-llm-retry` 等，先查再写

## 十一、迁移期专项

| 规则 | 说明 |
|---|---|
| 新旧代码不得同时定义同一能力 | 一个能力迁移完再删旧的，中间态用开关切，不用两套并存 |
| 每阶段必须可回滚 | 阶段结束前验证上一阶段仍能跑 |
| 删除前先冻结 | 冻结 = 只修致命 bug；冻结满一个阶段再删 |
| 动手前先读 spec 对应 R 编号 | 无对应 R 的实现不写 |
