"""配置加载器 — 读取 config.toml + .env，暴露各模块强类型配置。

加载优先级：
- 敏感凭据（LLM_API_KEY）以 `.env` 环境变量优先于 config.toml 明文值
- 其余字段：config.toml 对应 section > dataclass 默认值

使用方式：
    from app.config import load_config
    cfg = load_config()          # 默认读项目根目录 config.toml
    cfg.router.model             # 直接访问各 section
"""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent  # app/ -> feishu-agent-platform/
CONFIG_TOML = PROJECT_DIR / "config.toml"

# 加载 .env 文件（优先级高于 config.toml 中的 api_key）
load_dotenv(PROJECT_DIR / ".env")


@dataclass
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str = "deepseek-flash"  # 默认模型（aigw 网关）


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str


@dataclass
class MemoryLifecycleCfg:
    """MemoryCore 进程托管配置 — 随 bot 启动拉起、随 bot 关停停止。

    Attributes:
        enabled: 托管总开关；false = bot 不管 MemoryCore 死活（旧手动模式）。
        core_dir: MemoryCore 仓库目录（含 src/gateway/server.ts 与 tdai-gateway.yaml）。
        manage_process: true = bot 负责拉起与关停；false = 只做健康检查不托管进程。
    """

    enabled: bool = True
    core_dir: str = "~/IdeaProjects/tdai-agent-memory/MemoryCore"
    manage_process: bool = True


@dataclass
class MemoryConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8420"
    api_key: str = ""
    service_id: str = "feishu-bot"
    timeout_sec: int = 10
    # 网关 v1 /recall 只消费 query+session_key，
    # 无条数/策略/阈值参数可下发；此处只留客户端侧真正生效的截断闸。
    recall_context_max_chars: int = 2000
    lifecycle: MemoryLifecycleCfg = field(default_factory=MemoryLifecycleCfg)


@dataclass
class WebPortalConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8000"
    timeout_sec: int = 15


@dataclass
class CCConnectConfig:
    enabled: bool = True
    webhook_url: str = "http://127.0.0.1:9111/hook"
    webhook_token: str = ""
    default_project: str = "dev-bot"
    timeout_sec: int = 120
    # Claude Code 会话并发上限：send_and_wait 全程占一个额度，
    # 超限排队。会话复用由 cc-connect 侧 session_key resume 保证，无需配置。
    max_sessions: int = 2
    agent_map: dict[str, str] = field(default_factory=lambda: {
        "business": "dev-bot",
        "feishu": "dev-bot",
    })



@dataclass
class LLMSiteParams:
    """单个 LLM 调用点的采样参数覆盖 — None 表示继承 RouterConfig 基线。

    站点名与调用点对应：router（意图路由）/ direct（自答）/
    handler（handler agent 主调用）/ decompose（任务分解）。
    """

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


# 温度基线：历史硬编码值收编（与原调用点行为一致）；未列出的站点跟随 RouterConfig.temperature
_SITE_TEMPERATURE_DEFAULTS: dict[str, float] = {
    "decompose": 0.1,   # 任务分解需一点发散
}


@dataclass
class RouterConfig:
    """LangGraph 路由配置。

    Attributes:
        enabled: 是否启用 LangGraph 编排层。
        model: 路由 LLM 模型名。
        base_url: [OI] 兼容 API 地址。
        api_key: API Key。
        temperature: 采样温度基线（router/direct/handler 跟随；低 = 更确定性）。
        max_tokens: 补全上限基线（tokens）；None = 不传该参数，走服务端默认。
        top_p: 核采样基线；None = 不传该参数。
        llm_sites: 站点级采样覆盖（key 见 LLMSiteParams）；未配置项继承基线。
        timeout_sec: LLM 调用超时秒数。
        max_retries: SDK 层请求重试次数（网络/限流错误）。
        fallback_on_error: LLM 失败时是否降级到正则路由。
    """

    enabled: bool = True
    model: str = "deepseek-flash"
    base_url: str = "https://aigw.example.com/v1"
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int | None = None
    top_p: float | None = None
    llm_sites: dict[str, LLMSiteParams] = field(default_factory=dict)
    timeout_sec: int = 10
    max_retries: int = 2
    fallback_on_error: bool = True
    history_enabled: bool = True
    history_max_turns: int = 6
    # 会话历史落盘（logs/history/，重启后回填，对话不丢）
    history_persist: bool = True
    # 每会话 tool-call 上限（防滥用）：同一 session_key 在窗口内最多触发 N 次工具调用
    tool_rate_limit: int = 50
    tool_rate_window_sec: int = 600
    # 意图识别（SPEC-20260912 R3）：自报置信度低于阈值 → 路由降级为 clarify 反问；0 = 永不降级
    clarify_threshold: float = 0.6

    def sampling_params(self, site: str) -> dict[str, Any]:
        """生成指定 LLM 调用点的采样参数（供 chat.completions.create ** 展开）。

        合并优先级：llm_sites[site] > 基线字段 > 温度历史基线 > 基线 temperature。
        None 值不传给 SDK（保持服务端默认，与旧行为一致）。
        """
        override = self.llm_sites.get(site)
        temperature = (
            override.temperature
            if override and override.temperature is not None
            else _SITE_TEMPERATURE_DEFAULTS.get(site, self.temperature)
        )
        max_tokens = (
            override.max_tokens
            if override and override.max_tokens is not None
            else self.max_tokens
        )
        top_p = override.top_p if override and override.top_p is not None else self.top_p

        params: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if top_p is not None:
            params["top_p"] = top_p
        return params


@dataclass
class MessagingConfig:
    """消息聚合与回执配置 — 同会话同用户连发消息 debounce 合并 + 收到即回执。

    Attributes:
        enabled: 是否启用聚合（关闭则每条消息独立处理，旧行为）。
        debounce_sec: 静默窗口秒数。首条消息后等待 N 秒无新消息才触发处理。
        max_wait_sec: 单批最大等待上限（用户持续打字时防无限延长）。
        ack_receipt: 收到即回执开关——debounce 合并后立刻回复
            「✅ 已接收 N 条消息，正在处理/排队中…」，覆盖排队+路由+执行的
            全程静默期（配合运行时护栏，让排队用户知道任务没丢）。
        routed_receipt: 路由后去向回执开关——「🔍 正在交给 XX 处理…」；
            关闭则只保留 ack 回执，减少消息噪音。
        feedback_buttons: 答复后追加 🙂/🙃 反馈卡。开=每轮多一条极简卡片，
            换来「哪轮答得不好」变成可量化信号；关=退回纯文本，行为与旧版一致。
    """

    enabled: bool = True
    debounce_sec: float = 3.0
    max_wait_sec: float = 15.0
    ack_receipt: bool = True
    routed_receipt: bool = True
    feedback_buttons: bool = True


@dataclass
class DashboardConfig:
    """Dashboard HTTP server configuration."""

    enabled: bool = True
    port: int = 9112
    host: str = "0.0.0.0"


@dataclass
class CapabilityConfig:
    """能力热插拔配置 — 管理员审批接入新 Skill/Agent/MCP。

    Attributes:
        enabled: 总开关；false 时文件消息分流完全关闭，其余字段不生效。
        approver_min_level: 提交与审批所需的最低权限等级（L2 管理员）。
        max_pkg_mb: 能力包体积上限（MB）。
    """

    enabled: bool = True
    approver_min_level: int = 2
    max_pkg_mb: int = 2


@dataclass
class RuntimeGuardConfig:
    """运行时护栏配置 — 全局并发上限 + 有界排队 + 任务持久化。

    背景：此前每条消息直接 create_task 起后台编排，无全局并发上限，
    也没有 in-flight 任务持久化——高并发会压垮本地服务器（cc-connect
    每任务 fork 一个 Claude Code 子进程），重启会静默丢失处理中任务。

    Attributes:
        enabled: 总开关；false 时行为与旧版一致（无并发闸、无持久化）。
        max_concurrent: 全局同时执行编排的最大任务数。
            cc-connect 通道每任务 fork 一个子进程（并发预算主要消耗方），
            handler/direct 类任务开销很小；6 可覆盖 4~5 个 cc-connect 重任务
            加若干轻任务，压垮服务器时下调。
        max_queue: 等待队列长度上限；队满时新任务快速失败并回复"系统繁忙"。
        queue_timeout_sec: 任务在队列中的最长等待时间；超时回复"系统繁忙"。
        persist_enabled: 任务生命周期落盘开关（running/finished/interrupted）。
        persist_dir: 任务生命周期档案目录（相对项目根）。
        notify_interrupted: 启动时把上次中断的任务通过飞书主动告知用户。
    """

    enabled: bool = True
    max_concurrent: int = 6
    max_queue: int = 20
    queue_timeout_sec: float = 120.0
    persist_enabled: bool = True
    persist_dir: str = "logs/runtime_tasks"
    notify_interrupted: bool = True


@dataclass
class CaseMirrorConfig:
    """工单案例库飞书镜像 — 案例落盘后自动同步的写目标（user 写权限的收口点）。

    案例沉淀（对话「沉淀案例 …」）在上游 cases/*.md 落盘后，把同一案例镜像到
    飞书案例库：目录节点下建子文档 + 更新目录索引表，回读校验一致才算完成。
    三个 token 是案例库坐标（与 webportal-order-agent 仓 case-ingest SKILL 步骤 8
    记载一致）；换库只改配置不改代码。写入只允许挂 parent_node_token 下、
    改 index_doc_token 这两个目标，调用方不得传任意 token。

    Attributes:
        enabled: 总开关；false = 不同步（ingest 仍落上游本地案例库）。
        space_id: 案例库 wiki 空间 ID。
        parent_node_token: 案例子文档父节点 token。
        index_doc_token: 目录索引文档 token。
        wiki_base_url: wiki 链接前缀（拼目录索引里的跳转链接）。
    """

    enabled: bool = True
    space_id: str = ""
    parent_node_token: str = ""
    index_doc_token: str = ""
    wiki_base_url: str = "https://example.feishu.cn/wiki"


@dataclass
class CmdbWatchConfig:
    """CMDB 工单提醒 — 任何人自助订阅自己的工单，出现任务时私聊推卡片。

    由 `app/domains/cmdb_watch` 的常驻轮询使用（旁路，不在消息路由里）；
    `cmdb-watch-agent` 只负责订阅入口（见 app/agents/handlers/cmdb_watch_handler.py）。
    Attributes:
        enabled: 总开关；false = 不启动轮询循环，零网络开销。
        base_url: CMDB 地址，工单卡片里的「查看工单」链接也由它拼。
        state_path: 订阅者与观察状态的落盘路径（相对路径基于项目根）。
        interval_sec: 轮询间隔，下限 60s（CMDB 内网按 5s 轮询的页面量级估算，60s 足够）。
        scan_limit: 每轮最多拉多少张工单状态（含跟踪中的）；超出截断并告警。
        probe_span: **无 session 时**用 id 顺序探测的跨度；0 = 只用工单列表接口
            （列表接口需要 sessionid，见 client.py 抬头）。
        probe_step: 列表接口可用时的探测宽度（id 已对齐到最新，只需看最近几张）。
        session_id: CMDB sessionid。留空则读环境变量 CMDB_SESSION_ID 或 `~/.cmdb-session`
            （与 cmdb-order-tracker skill 共用同一份，登录一次即可）。
            后台**不会**自动登录：是否允许由环境变量 `CMDB_ALLOW_RELOGIN` 决定
            （默认 confirm = 不登），见 `client.py` 抬头。
        initial_scan: 首次启动时回溯探测多少张历史工单（0 = 只从现在往后看）。
            朴素 id 探测下这是唯一能「找回刚创建但还没被轮询到的工单」的手段。
        refresh_sec: 已跟踪工单的全量校准周期（秒）。平时只轮询轻量状态接口，
            到这个间隔才把整张工单重读一遍，补齐 SQL/NACOS 状态与任务增删。
        refresh_session_on_start: 启动时体检 CMDB 登录态。登录过期不致命（列表入口
            会被拒、自动退回 id 探测），但会让新工单发现晚一轮，所以启动日志要说清楚。
        watch_error_limit: 指定工单关注连续拉取失败多少轮后自动停止（工单被删/
            权限回收时不该无限重试）。
    """

    enabled: bool = False
    base_url: str = "http://cmdb.example.com"
    state_path: str = "logs/cmdb_watch/state.json"
    interval_sec: int = 120
    scan_limit: int = 30
    probe_span: int = 30
    probe_step: int = 3
    session_id: str = ""
    initial_scan: int = 10
    refresh_sec: int = 900
    refresh_session_on_start: bool = True
    watch_error_limit: int = 5

    def resolved_base_url(self) -> str:
        return self.base_url.rstrip("/")


@dataclass
class ScheduledTasksConfig:
    """定时任务调度配置 — 评估通过即生效（无审批门），扫描间隔 ≥10 分钟。

    Attributes:
        enabled: 总开关；false 时 schedule-agent 创建入口与调度循环都关闭。
        scan_interval_sec: 调度扫描间隔（秒）；低于 600（10 分钟）会被
            ScheduledTaskLoop 强制拉回 600（需求红线）。
        approver_min_level: **已废弃**——审批门下线后无人使用，保留字段只为让
            旧 config.toml 里的同名键不至于加载失败（多余键会被静默忽略）。
    """

    enabled: bool = True
    scan_interval_sec: int = 600
    approver_min_level: int = 2


@dataclass
class SkillCatalogConfig:
    """团队 Skill 目录 — 按分支读 description，一天一刷，可手动刷新。

    Attributes:
        enabled: 总开关。false 时不建 store，handler 不会以空目录上线。
        repo_url: team-skills/skills 的 clone 地址。
        bare_dir: bare clone 位置（运行产物，不入库）。
        snapshot_path: 上次成功扫描的 JSON。查询只读这份文件。
        refresh_interval_hours: 快照超过该小时数才在查询时 fetch。手动刷新不受此限制。
        fetch_timeout_sec: git clone / fetch 超时。
        skip_branches: 不进目录的分支（试验分支、已被覆盖的早期包）。
    """

    enabled: bool = True
    repo_url: str = "http://git.example.com/team-skills/skills.git"
    bare_dir: str = "logs/skill_catalog/repo.git"
    snapshot_path: str = "logs/skill_catalog/snapshot.json"
    refresh_interval_hours: int = 24
    fetch_timeout_sec: int = 120
    skip_branches: list[str] = field(default_factory=lambda: [
        "addNewSkill", "addNewSkill_v1", "addNewSkill_v2", "merge_all", "init",
    ])



@dataclass
class PrdWatchConfig:
    """easyprd PRD 变更监听 — 群讨论/妙记 → 变更点 → PRD 评论 → 决策闭环。

    格式契约与不变量：docs/features/easyprd-prd-watch.md

    Attributes:
        enabled: 总开关。false = 旁路不注册，主链路零开销（默认关，配好台账再开）。
        watch_chats: 白名单群 chat_id。**只有这里的群**才会放开「非@消息也收」，
            其余群的准入行为完全不变——放开全量监听是隐私敏感动作，必须逐群显式授权。
        window_sec: 讨论攒窗口（秒）。窗口内多条消息合并成一批送识别，一次讨论
            只出一张卡片，禁止逐条刷屏。
        extract_min_confidence: 变更识别下限。低于此值直接丢弃并计数。
        extract_flag_confidence: 标 ⚠️ 上限。介于 min 与此值之间 → 写评论但标注
            「AI 自动识别，请人工确认」；高于此值按正式变更出。
        poll_replies_sec: 评论回复轮询间隔（决策闭环走轮询，评论事件订阅未接线）。
        decision_ttl_days: pending 超过该工作日数提醒一次，不重复催。
        dry_run: true = 只识别落日志，不真写评论、不发卡片（灰度第一步，看误报率）。
        ledger_base_token / ledger_change_table / ledger_bind_table: 多维表格坐标。
            任一为空 → 台账功能关闭（不猜、不用默认值），变更只进日志。
        comment_identity: 评论读写用 `bot` 还是 `user`。bot 优先——署名是应用，
            追溯比「像同事说的」清楚；但 bot 要能评论，应用必须先是该文档/空间的
            协作者，否则真实调用返回 `1069303 Access denied`（读都读不到，
            返回 `3380004 No permission`），而这一步只能由文档 owner 授予。
            切 `user` 则评价身份本人的登录态需同时具备
            `docs:document.comment:create` / `:write_only` / `:read` 三个 scope。
        meegle_simple_name: 飞书项目空间 simple_name（Story 与 PRD 链接的事实源）。
            空 = 不接 Meegle，绑定全走多维表格（老路径）。凭证只从 .env 的
            MEEGLE_USER_TOKEN / FEISHU_MCP_TOKEN 或 ~/.claude/feishu_token.txt 读，
            **绝不在本配置里写 token**——那是个人在飞书项目的全部读权限。
        story_chat_map: StoryID → 群 chat_id。Meegle 上没有「需求对应哪个群」，
            靠群名模糊匹配 = 写错 PRD 的风险，所以这一项必须管理员显式给。
        prd_field / task_doc_field: Story 上 PRD 链接的字段 key（实测 examplecorp 空间
            「需求文档」= field_c56ee5，「任务文档」= wiki）；换空间时改这里。
        minutes_identity: 读妙记用 user / bot。实测 bot 未被 scope 挡
            （假 token 返回 resource not found 而非 app_scope_not_applied），
            user 侧 minutes:minute 是否已授权取决于登录态 —— 两边都留口子，
            失败时把缺的 scope 原样报出来，不静默跳过会议素材。
        minutes_poll_sec: 妙记轮询间隔（妙记生成事件是 user 身份，bot 长连接收不到）。
        minutes_story_map: minute_token → StoryID 的显式映射（配置里写死）。
            **不做自动发现**：`minutes +search` 需 scope `minutes:minutes.search:read`
            （当前登录态缺失），且「哪场会对应哪个需求」属于人工台账，
            让 bot 靠会议标题猜需求是设计稿明令禁止的猜测行为。
        minutes_base_url / notes_base_url: 妙记与智能纪要 docx 的可点链接前缀，
            卡片「变更依据」靠它拼出能点开的来源。留空 = 没有链接（不猜租户域名）。
            实测：他人拥有的妙记 `+detail` 返回 `No read permission`，
            而会议自动生成的**智能纪要 docx 两种身份都能读** → 会议素材优先用纪要，
            此时卡片来源链接就是 `{notes_base_url}/{obj_token}` 那种形态。
        cr_base_token / cr_table: easyprd **自建**的 CR 表坐标（评论 id、决策状态、
            置信分带）。与需求知识台账的「变更记录」表分开：那是时序归档，这是待办
            状态机。为空时用内存替身，状态面板会明说「决策状态只在内存里」。
        kb_base_token / kb_*_table: 需求知识台账（req-knowledge-ledger 内化）的 base
            与三张表；表 id 留空取 examplecorp 空间默认值。
        figma_token: UI 对账用的 Figma 只读 token（也接受 .env 的 FIGMA_API_KEY）。
        meeting_auto / meeting_scan_sec / meeting_scan_days / meeting_scan_limit:
            是否按需求名核心词自动搜妙记并整理（默认关；关掉时只支持手动贴链接）。
    """

    enabled: bool = False
    watch_chats: list[str] = field(default_factory=list)
    window_sec: float = 300.0
    extract_min_confidence: float = 0.7
    extract_flag_confidence: float = 0.85
    poll_replies_sec: int = 120
    decision_ttl_days: int = 3
    dry_run: bool = True
    ledger_base_token: str = ""
    ledger_change_table: str = ""
    ledger_bind_table: str = ""
    comment_identity: str = "bot"
    meegle_simple_name: str = ""
    story_chat_map: dict[str, str] = field(default_factory=dict)
    prd_field: str = "field_c56ee5"
    task_doc_field: str = "wiki"
    minutes_identity: str = "user"
    minutes_poll_sec: int = 1800
    minutes_story_map: dict[str, str] = field(default_factory=dict)
    # 妙记可点链接前缀（例 `https://xxx.feishu.cn/minutes`），卡片「变更依据」用它拼
    # `{base}/{minute_token}`。**留空 = 卡片只有来源文字、没有链接**——域名是租户私有
    # 信息，绝不拿 token 猜一个出来（链不上的链接在卡片上看不出来，比没链接更糟）。
    minutes_base_url: str = ""
    # 智能纪要（会议自动生成的 docx）链接前缀，同上规则。实测：妙记本体常无读权限，
    # 而纪要 docx 两种身份都能读，所以会议素材优先走纪要 → 这个前缀也留着。
    notes_base_url: str = ""
    # CR 台账（**本能力自建**的「PRD变更建议CR」表）：评论 id / 决策状态 / 分带落在这里。
    # 与需求知识台账的「变更记录」表分开——那是时序归档，这是待办状态机，混一起会让
    # 「已采纳」被写进叙事字段。坐标为空 → 用内存替身，状态面板会明说「只在内存里」。
    cr_base_token: str = ""
    cr_table: str = ""
    # 需求知识台账（req-knowledge-ledger 内化）的 base 与三张表。表 id 留空取默认
    # （examplecorp 空间现行建表形态，与线上一致）。
    kb_base_token: str = ""
    kb_story_table: str = ""
    kb_meeting_table: str = ""
    kb_change_table: str = ""
    # 本地台账（SQLite）。开 = 三张表（绑定 / CR / 知识地图）落 `logs/prd_watch/ledger.db`，
    # 不再需要任何多维表格坐标；base 坐标即使配着也不会被读（避免两个事实源）。
    # 路径留空 = `logs/prd_watch/ledger.db`（相对仓库根，与运行产物同域，已 gitignore）。
    local_ledger: bool = True
    local_ledger_path: str = ""
    # Figma 只读 token（UI 对账用）。也接受 .env 的 FIGMA_API_KEY / FIGMA_TOKEN。
    figma_token: str = ""
    # 会议自动扫描：按需求名核心词搜妙记并整理（不配 = 只支持手动贴链接）
    meeting_auto: bool = False
    meeting_scan_sec: int = 3600
    meeting_scan_days: int = 7
    meeting_scan_limit: int = 5

    @property
    def ledger_ready(self) -> bool:
        """旧台账坐标（绑定表 + 变更记录表）配齐才算可用。

        本地台账开着时这个属性不再是「能不能持久化」的判据——`service` 只看
        `local_ledger`，避免两套存储各判一次、给出互相矛盾的启动摘要。
        """
        return bool(self.ledger_base_token and self.ledger_change_table)

    @property
    def cr_ready(self) -> bool:
        """CR 台账是否可用（配齐 = 决策状态可跨重启）。"""
        return bool(self.cr_base_token and self.cr_table)

    @property
    def kb_ready(self) -> bool:
        """需求知识台账是否可用。"""
        return bool(self.kb_base_token)

    @property
    def minutes_configured(self) -> bool:
        """有可跑的会议素材来源：显式映射、自动扫描、或随群聊一起放开。"""
        return bool(self.minutes_story_map or self.meeting_auto)


@dataclass
class McpServerConfig:
    """一个 stdio MCP server 的启动方式与超时。

    Attributes:
        command: 可执行文件；空 = 当前 Python 解释器（模块启动形式）。
        args: 参数（如 `-m app.mcp.feishu_server`）。
        cwd: 子进程工作目录；空 = 调用方进程当前目录。
        timeout_sec: 单次工具调用上限；客户端不透传时退化为仅提示。
        enabled: false 时不挂载（能力缺席好过启动一个必然失败的子进程）。
    """

    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    timeout_sec: float = 120.0
    enabled: bool = True
    # 子进程环境变量（凭据走这里，值支持 `${VAR}` 由 supervisor 展开）。
    # 为什么需要：Figma 那条 MCP 只认 FIGMA_API_KEY 环境变量，不接受命令行明文
    # token——写进 args 会出现在进程列表与日志里。
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class MCPConfig:
    """MCP 工具面配置 — feishu / domains / capabilities / order_agent。

    Attributes:
        feishu: 飞书收发、读文档、读会话最近消息（三红线在工具层硬拒）。
        domains: 业务域薄封装（日报 / Skill 目录 / 定时任务只读）。
        capabilities: 把进程内 Handler 的工具面（数字先知、CMDB 提醒、工单、
            PRD 监听…）投影成 MCP 工具——dsh 链路下**能力的执行入口**。
            不投影时这些能力只有描述、没有手（会让模型如实说「没有执行入口」）。
        order_agent: 独立工单 Agent 的 MCP 投影（代理转发其 stdio server）。
        feishu_max_text_chars: 单条飞书文本长度上限（超长工具层拒绝，不截断）。
    """

    feishu: McpServerConfig = field(
        default_factory=lambda: McpServerConfig(args=["-m", "app.mcp.feishu_server"]))
    domains: McpServerConfig = field(
        default_factory=lambda: McpServerConfig(args=["-m", "app.mcp.domains_server"]))
    capabilities: McpServerConfig = field(
        default_factory=lambda: McpServerConfig(args=["-m", "app.mcp.capabilities_server"]))
    order_agent: McpServerConfig = field(
        default_factory=lambda: McpServerConfig(args=["-m", "app.mcp.order_agent_server"]))
    # Figma 设计稿（PRD↔UI 对账）：官方 `figma-developer-mcp` 的 stdio 模式。
    # 默认 disabled：它需要 node 入口绝对路径 + token，两者缺一个都会挂上一个
    # 必然失败的子进程（表现为「模型说没有这个工具」，不报错）。
    figma: McpServerConfig = field(
        default_factory=lambda: McpServerConfig(args=["--stdio"], enabled=False))
    feishu_max_text_chars: int = 3000


@dataclass
class DshConfig:
    """dsh 执行面配置 — 进程、profile、默认模型、凭据。

    模型与凭据都可配，源是 config.toml `[projects.dsh]`；`api_key` 留空时从
    `AIGW_API_KEY` 环境变量读（凭据铁律：只放 .env，配置文件里不留明文）。

    Attributes:
        enabled: 总开关；false 时执行面整体不可用（不静默降级到旧 handler）。
        profile: dsh profile 名，部署产物在 deploy/dsh/<profile>/。
        dsh_home: 独立 DSH_HOME，避免与桌面版共享凭据与 session。
        workspace: 会话工作目录（dsh 要求绝对路径）。
        provider: 默认 LLM 路由名，必须在 profile 的 providers 里存在。
        model: 默认模型名，必须在 provider 的 models 列表里存在。
        api_key: 网关凭据；空则读环境变量 AIGW_API_KEY。
        session_ttl_sec: 会话空闲回收阈值；超时关会话，下条消息开新会话。
    """

    enabled: bool = True
    profile: str = "agentbiz"
    dsh_home: str = ""
    workspace: str = ""
    provider: str = "flp-self-oai"
    model: str = "glm-5.3-flash"
    api_key: str = ""
    session_ttl_sec: float = 1800.0

    @property
    def resolved_api_key(self) -> str:
        """生效的网关凭据：配置优先，其次环境变量。"""
        return self.api_key or os.environ.get("AIGW_API_KEY", "")


@dataclass
class PmIdentity:
    """PM 本人身份：open_id 与群里怎么称呼他。

    `names` 是「这条消息冲我来的」判定依据（`pm_watch._my_names`）、也是
    「别给自己起草催办」的过滤条件（`pm_nudge.ME`）。换人只改配置。
    """

    open_id: str = ""
    names: list[str] = field(default_factory=list)


@dataclass
class PmOutputs:
    """PM 的表在哪。空值 = 该表未接，调用方按缺省跳过而不是拼一个字面量。"""

    spreadsheet_token: str = ""
    sheet_id_versions: str = ""
    sheet_id_weekly: str = ""
    base_token: str = ""
    table_id: str = ""


@dataclass
class PmPush:
    """推送目标与时点。`group_id` 为空 = 只发私聊。"""

    group_id: str = ""
    morning: str = "09:00"
    evening: str = "17:30"


@dataclass
class PmRelease:
    """发版节拍。以 SOP 闸门的工作日为坐标：周一=1 … 周四=4。"""

    release_weekday: int = 4
    ask_days_before: list[int] = field(default_factory=lambda: [8])


@dataclass
class PmRules:
    """调用方自用的规则，不传给能力层。"""

    stale_days: int = 3
    snooze_days: int = 3
    scan_mode: str = "all"
    release: PmRelease = field(default_factory=PmRelease)


@dataclass
class PmConfig:
    """PM 工作台绑定（`[projects.pm]`）。

    这一层只是**把身份与表位置从代码里搬出来**，不含业务判断——
    「什么算冲我来」「卡片长什么样」仍在 `app/domains/pm/` 与调用方。
    """

    enabled: bool = False
    identity: PmIdentity = field(default_factory=PmIdentity)
    outputs: PmOutputs = field(default_factory=PmOutputs)
    push: PmPush = field(default_factory=PmPush)
    rules: PmRules = field(default_factory=PmRules)


@dataclass
class DailyReportConfig:
    """日报订阅推送（群里的定时推送），不是路由能力。

    路由侧的资讯问答由 aihot 承接；这里只负责「到点把当日报纸推到订阅群」。
    """

    enabled: bool = True
    cache_dir: str = "logs/daily"
    check_interval_sec: int = 60
    max_push_length: int = 3000


@dataclass
class AppConfig:
    feishu: FeishuConfig
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    webportal: WebPortalConfig = field(default_factory=WebPortalConfig)
    cc_connect: CCConnectConfig = field(default_factory=CCConnectConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    capability: CapabilityConfig = field(default_factory=CapabilityConfig)
    runtime_guard: RuntimeGuardConfig = field(default_factory=RuntimeGuardConfig)
    case_mirror: CaseMirrorConfig = field(default_factory=CaseMirrorConfig)
    daily_report: DailyReportConfig = field(default_factory=DailyReportConfig)
    scheduled_tasks: ScheduledTasksConfig = field(default_factory=ScheduledTasksConfig)
    skill_catalog: SkillCatalogConfig = field(default_factory=SkillCatalogConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    dsh: DshConfig = field(default_factory=DshConfig)
    prd_watch: PrdWatchConfig = field(default_factory=PrdWatchConfig)
    cmdb_watch: CmdbWatchConfig = field(default_factory=CmdbWatchConfig)
    pm: PmConfig = field(default_factory=PmConfig)
    providers: list[ProviderConfig] = field(default_factory=list)
    work_dir: str = ""


def load_config(path: Path | None = None) -> AppConfig:
    """从 config.toml 加载全量配置。

    启动期第一现场：加载成功/失败均打日志（含文件路径与各 section 摘要，
    凭据字段只打「是否配置」不落值）。config.toml 缺失或格式错误直接抛异常，
    由调用方决定是否终止启动。
    """
    path = path or CONFIG_TOML
    logger.info("[config] loading %s", path)
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    project = cfg["projects"][0]
    pf = project["platforms"][0]["options"]

    providers = []
    for p in project.get("agent", {}).get("providers", []):
        # .env 中的 API key 优先于 config.toml
        api_key = os.getenv("LLM_API_KEY") or p["api_key"]
        providers.append(ProviderConfig(
            name=p["name"],
            api_key=api_key,
            base_url=p["base_url"],
            model=p.get("model", "deepseek-flash"),
        ))

    app_cfg = AppConfig(
        feishu=FeishuConfig(
            app_id=pf["app_id"],
            # 凭据不入库：config.toml 留空，从 .env 兜底读取
            app_secret=os.getenv("FEISHU_APP_SECRET") or pf["app_secret"],
        ),
        memory=_load_memory_config(project),
        webportal=_load_webportal_config(project),
        cc_connect=_load_cc_connect_config(project),
        daily_report=_load_daily_report_config(project),
        router=_load_router_config(project),
        messaging=_load_messaging_config(project),
        dashboard=_load_dashboard_config(project),
        capability=_load_capability_config(project),
        runtime_guard=_load_runtime_guard_config(project),
        case_mirror=_load_case_mirror_config(project),
        scheduled_tasks=_load_scheduled_tasks_config(project),
        skill_catalog=_load_skill_catalog_config(project),
        mcp=_load_mcp_config(project),
        dsh=_load_dsh_config(project),
        prd_watch=_load_prd_watch_config(project),
        cmdb_watch=_load_cmdb_watch_config(project),
        pm=_load_pm_config(project),
        providers=providers,
        work_dir=project.get("agent", {}).get("options", {}).get("work_dir", ""),
        # agents_yaml removed — agents loaded from bot/agents/ dir
    )
    _log_summary(app_cfg)
    return app_cfg


def _log_summary(cfg: AppConfig) -> None:
    """打印配置摘要（排障用）— 只打非敏感字段与凭据「有无」，不打值。"""
    logger.info(
        "[config] loaded: feishu(app_id=%s, secret=%s) router(model=%s, enabled=%s) "
        "cc_connect(enabled=%s, webhook=%s) memory(enabled=%s) webportal(enabled=%s) "
        "messaging(debounce=%.1fs, max_wait=%.1fs) dashboard(port=%s) "
        "runtime_guard(enabled=%s, max_concurrent=%s, max_queue=%s)",
        cfg.feishu.app_id[:8] + "…" if cfg.feishu.app_id else "MISSING",
        "set" if cfg.feishu.app_secret else "MISSING",
        cfg.router.model, cfg.router.enabled,
        cfg.cc_connect.enabled, cfg.cc_connect.webhook_url,
        cfg.memory.enabled, cfg.webportal.enabled,
        cfg.messaging.debounce_sec, cfg.messaging.max_wait_sec,
        cfg.dashboard.port,
        cfg.runtime_guard.enabled, cfg.runtime_guard.max_concurrent,
        cfg.runtime_guard.max_queue,
    )
    # PM 绑定单独一行：这一段最容易「写了不生效」（顶层段 vs projects 子段），
    # 启动时把是否读到值打出来，比事后 grep 配置快得多。
    logger.info(
        "[config] pm: enabled=%s identity=%s names=%s outputs=%s push_group=%s",
        cfg.pm.enabled,
        "set" if cfg.pm.identity.open_id else "MISSING",
        cfg.pm.identity.names,
        "set" if cfg.pm.outputs.spreadsheet_token else "MISSING",
        "set" if cfg.pm.push.group_id else "MISSING",
    )


def _load_memory_config(project: dict) -> MemoryConfig:
    """从 project 配置读取记忆服务配置，缺省项回落默认值。"""
    mc = project.get("memory", {})
    lc = mc.get("lifecycle", {})
    return MemoryConfig(
        enabled=mc.get("enabled", True),
        base_url=mc.get("base_url", "http://127.0.0.1:8420"),
        api_key=mc.get("api_key", ""),
        service_id=mc.get("service_id", "feishu-bot"),
        timeout_sec=mc.get("timeout_sec", 10),
        recall_context_max_chars=int(
            mc.get("recall_context_max_chars", 2000)
        ),
        lifecycle=MemoryLifecycleCfg(
            enabled=bool(lc.get("enabled", True)),
            core_dir=str(lc.get(
                "core_dir", "~/IdeaProjects/tdai-agent-memory/MemoryCore"
            )),
            manage_process=bool(lc.get("manage_process", True)),
        ),
    )


def _load_webportal_config(project: dict) -> WebPortalConfig:
    """从 project 配置读取 WebPortal 工单服务配置，缺省项回落默认值。"""
    wc = project.get("webportal", {})
    return WebPortalConfig(
        enabled=wc.get("enabled", True),
        base_url=wc.get("base_url", "http://127.0.0.1:8000"),
        timeout_sec=wc.get("timeout_sec", 15),
    )


def _load_cc_connect_config(project: dict) -> CCConnectConfig:
    """从 project 配置读取 cc-connect 桥接配置，缺省项回落默认值。"""
    cc = project.get("cc_connect", {})
    return CCConnectConfig(
        enabled=cc.get("enabled", True),
        webhook_url=cc.get("webhook_url", "http://127.0.0.1:9111/hook"),
        # 凭据不入库：config.toml 留空，从 .env 兜底读取
        webhook_token=os.getenv("CC_WEBHOOK_TOKEN") or cc.get("webhook_token", ""),
        default_project=cc.get("default_project", "dev-bot"),
        timeout_sec=cc.get("timeout_sec", 120),
        max_sessions=int(cc.get("max_sessions", 2)),
        agent_map=cc.get("agent_map", {
            "business": "dev-bot",
            "feishu": "dev-bot",
        }),
    )


def _load_daily_report_config(project: dict) -> DailyReportConfig:
    """读日报订阅推送配置（[projects.daily_report]）。"""
    d = project.get("daily_report", {})
    default = DailyReportConfig()
    return DailyReportConfig(
        enabled=bool(d.get("enabled", default.enabled)),
        cache_dir=str(d.get("cache_dir", default.cache_dir)),
        check_interval_sec=int(d.get("check_interval_sec", default.check_interval_sec)),
        max_push_length=int(d.get("max_push_length", default.max_push_length)),
    )


def _load_dashboard_config(project: dict) -> DashboardConfig:
    """从 project 配置读取 Dashboard 配置，缺省项回落默认值。"""
    dc = project.get("dashboard", {})
    return DashboardConfig(
        enabled=dc.get("enabled", True),
        port=dc.get("port", 9112),
        host=dc.get("host", "0.0.0.0"),
    )


def _load_capability_config(project: dict) -> CapabilityConfig:
    """从 project 配置读取能力热插拔配置，缺省项回落默认值。"""
    cc = project.get("capability", {})
    return CapabilityConfig(
        enabled=cc.get("enabled", True),
        approver_min_level=int(cc.get("approver_min_level", 2)),
        max_pkg_mb=int(cc.get("max_pkg_mb", 2)),
    )


def _load_runtime_guard_config(project: dict) -> RuntimeGuardConfig:
    """从 project 配置读取运行时护栏配置（并发/排队/任务持久化）。"""
    rg = project.get("runtime_guard", {})
    return RuntimeGuardConfig(
        enabled=bool(rg.get("enabled", True)),
        max_concurrent=int(rg.get("max_concurrent", 6)),
        max_queue=int(rg.get("max_queue", 20)),
        queue_timeout_sec=float(rg.get("queue_timeout_sec", 120.0)),
        persist_enabled=bool(rg.get("persist_enabled", True)),
        persist_dir=str(rg.get("persist_dir", "logs/runtime_tasks")),
        notify_interrupted=bool(rg.get("notify_interrupted", True)),
    )


def _load_case_mirror_config(project: dict) -> CaseMirrorConfig:
    """从 project 配置读取案例库飞书镜像的写目标（三个 token）。"""
    cm = project.get("case_mirror", {})
    default = CaseMirrorConfig()
    return CaseMirrorConfig(
        enabled=bool(cm.get("enabled", True)),
        space_id=str(cm.get("space_id", "")).strip(),
        parent_node_token=str(cm.get("parent_node_token", "")).strip(),
        index_doc_token=str(cm.get("index_doc_token", "")).strip(),
        wiki_base_url=str(cm.get("wiki_base_url", "")).strip() or default.wiki_base_url,
    )


def _load_cmdb_watch_config(project: dict) -> CmdbWatchConfig:
    """从 project 配置读取 CMDB 工单提醒配置，缺省项回落默认值。

    订阅名单不在配置里——每人的 CMDB 用户名由本人在对话里声明并落 state.json，
    配置只留「服务怎么跑」的参数。轮询间隔做下限校验：低于 60s 会把内网 CMDB
    变成自己的压测对象，配置写错时拉回并告警。
    """
    cw = project.get("cmdb_watch", {})
    interval = int(cw.get("interval_sec", 120))
    if interval < 60:
        logger.warning("[cmdb_watch] interval_sec=%s < 60（下限），强制拉回 60", interval)
        interval = 60
    return CmdbWatchConfig(
        enabled=bool(cw.get("enabled", False)),
        base_url=str(cw.get("base_url") or "http://cmdb.example.com").rstrip("/"),
        state_path=str(cw.get("state_path") or "logs/cmdb_watch/state.json"),
        interval_sec=interval,
        scan_limit=max(1, int(cw.get("scan_limit", 30))),
        probe_span=max(0, int(cw.get("probe_span", 30))),
        probe_step=max(1, int(cw.get("probe_step", 3))),
        session_id=str(cw.get("session_id") or ""),
        initial_scan=max(0, int(cw.get("initial_scan", 10))),
        refresh_sec=max(60, int(cw.get("refresh_sec", 900))),
        refresh_session_on_start=bool(cw.get("refresh_session_on_start", True)),
        watch_error_limit=max(1, int(cw.get("watch_error_limit", 5))),
    )


def _load_pm_config(project: dict) -> PmConfig:
    """从 `[projects.pm]` 读 PM 工作台绑定。

    这一段**必须挂在 `[[projects]]` 下面**（与 `[projects.dashboard]` 同级）。
    写成顶层段 `[pm]` 的话 `project.get("pm")` 取不到，只会静默拿到默认值——
    配置写在那儿很好看，运行时永远不生效，不报错也不打日志。
    """
    pm = project.get("pm", {}) or {}
    ident = pm.get("identity", {}) or {}
    out = pm.get("outputs", {}) or {}
    push = pm.get("push", {}) or {}
    rules = pm.get("rules", {}) or {}
    rel = rules.get("release", {}) or {}
    names = [str(n).strip() for n in (ident.get("names") or []) if str(n).strip()]
    return PmConfig(
        enabled=bool(pm.get("enabled", False)),
        identity=PmIdentity(
            open_id=str(ident.get("open_id", "")).strip(),
            names=names,
        ),
        outputs=PmOutputs(
            spreadsheet_token=str(out.get("spreadsheet_token", "")).strip(),
            sheet_id_versions=str(out.get("sheet_id_versions", "")).strip(),
            sheet_id_weekly=str(out.get("sheet_id_weekly", "")).strip(),
            base_token=str(out.get("base_token", "")).strip(),
            table_id=str(out.get("table_id", "")).strip(),
        ),
        push=PmPush(
            group_id=str(push.get("group_id", "")).strip(),
            morning=str(push.get("morning", "09:00")),
            evening=str(push.get("evening", "17:30")),
        ),
        rules=PmRules(
            stale_days=int(rules.get("stale_days", 3)),
            snooze_days=int(rules.get("snooze_days", 3)),
            scan_mode=str(rules.get("scan_mode", "all")),
            release=PmRelease(
                release_weekday=int(rel.get("release_weekday", 4)),
                ask_days_before=[int(d) for d in (rel.get("ask_days_before") or [8])],
            ),
        ),
    )


def _load_scheduled_tasks_config(project: dict) -> ScheduledTasksConfig:
    """从 project 配置读取定时任务调度配置，缺省项回落默认值。"""
    st = project.get("scheduled_tasks", {})
    return ScheduledTasksConfig(
        enabled=bool(st.get("enabled", True)),
        scan_interval_sec=int(st.get("scan_interval_sec", 600)),
        approver_min_level=int(st.get("approver_min_level", 2)),
    )


def _load_skill_catalog_config(project: dict) -> SkillCatalogConfig:
    """从 project 配置读取团队 Skill 目录。缺省即默认仓库与 24 小时刷新。"""
    sc = project.get("skill_catalog", {})
    default = SkillCatalogConfig()
    raw_skip = sc.get("skip_branches", default.skip_branches)
    if isinstance(raw_skip, str):
        skip = [p.strip() for p in raw_skip.split(",") if p.strip()]
    else:
        skip = [str(p).strip() for p in raw_skip if str(p).strip()]
    return SkillCatalogConfig(
        enabled=bool(sc.get("enabled", True)),
        repo_url=str(sc.get("repo_url", default.repo_url)).strip(),
        bare_dir=str(sc.get("bare_dir", default.bare_dir)).strip(),
        snapshot_path=str(sc.get("snapshot_path", default.snapshot_path)).strip(),
        refresh_interval_hours=int(sc.get("refresh_interval_hours", 24)),
        fetch_timeout_sec=int(sc.get("fetch_timeout_sec", 120)),
        skip_branches=skip,
    )


def _load_dsh_config(project: dict) -> DshConfig:
    """从 `[projects.dsh]` 读取执行面配置，缺省项回落 dataclass 默认值。

    凭据不在配置文件里写明文：`api_key` 留空即读 `AIGW_API_KEY`，
    与项目「凭据只放 .env」的口径一致。
    """
    d = project.get("dsh", {})
    default = DshConfig()
    return DshConfig(
        enabled=bool(d.get("enabled", True)),
        profile=str(d.get("profile", default.profile)).strip(),
        dsh_home=str(d.get("dsh_home", "") or "").strip(),
        workspace=str(d.get("workspace", "") or "").strip(),
        provider=str(d.get("provider", default.provider)).strip(),
        model=str(d.get("model", default.model)).strip(),
        api_key=str(d.get("api_key", "") or "").strip(),
        session_ttl_sec=float(d.get("session_ttl_sec", default.session_ttl_sec)),
    )



def _load_mcp_server(raw: dict, default: McpServerConfig) -> McpServerConfig:
    """单个 MCP server 段：args 允许列表或空格分隔字符串。"""
    raw_args = raw.get("args", default.args)
    if isinstance(raw_args, str):
        args = raw_args.split()
    else:
        args = [str(a) for a in raw_args]
    env = raw.get("env", default.env)
    if not isinstance(env, dict):
        raise ValueError(f"env 必须是表（table），收到 {type(env).__name__}")
    return McpServerConfig(
        command=str(raw.get("command", default.command) or ""),
        args=args,
        cwd=str(raw.get("cwd", default.cwd) or ""),
        timeout_sec=float(raw.get("timeout_sec", default.timeout_sec)),
        enabled=bool(raw.get("enabled", default.enabled)),
        env={str(k): str(v) for k, v in env.items()},
    )


def _load_mcp_config(project: dict) -> MCPConfig:
    """从 [projects.mcp] 读取各 stdio server 的启动方式与飞书文本上限。"""
    mc = project.get("mcp", {})
    default = MCPConfig()
    names = ("feishu", "domains", "capabilities", "order_agent", "figma")
    servers = {name: mc.get(name, {}) for name in names}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            raise ValueError(f"[projects.mcp.{name}] 必须是表（table），收到 {type(raw).__name__}")
    return MCPConfig(
        feishu=_load_mcp_server(servers["feishu"], default.feishu),
        domains=_load_mcp_server(servers["domains"], default.domains),
        capabilities=_load_mcp_server(servers["capabilities"], default.capabilities),
        order_agent=_load_mcp_server(servers["order_agent"], default.order_agent),
        figma=_load_mcp_server(servers["figma"], default.figma),
        feishu_max_text_chars=int(mc.get("feishu_max_text_chars", 3000)),
    )


def _load_prd_watch_config(project: dict) -> PrdWatchConfig:
    """从 project 配置读取 easyprd 监听配置，缺省项回落默认值。

    置信度分带做入口校验：min > flag 时两者互换并告警——阈值写反会让「正式变更」
    全部带上 ⚠️（或反之），比配错数字更难发现。
    """
    pw = project.get("prd_watch", {})
    lo = float(pw.get("extract_min_confidence", 0.7))
    hi = float(pw.get("extract_flag_confidence", 0.85))
    if lo > hi:
        logger.warning(
            "[prd_watch] extract_min_confidence(%.2f) > extract_flag_confidence(%.2f)，已互换",
            lo, hi,
        )
        lo, hi = hi, lo
    return PrdWatchConfig(
        enabled=bool(pw.get("enabled", False)),
        # 空串与 None 都不算群 ID：写歪的注释项不能被当成「监听所有群」的空匹配
        watch_chats=[c for c in (str(x).strip() for x in (pw.get("watch_chats") or []))
                     if c and c.lower() != "none"],
        window_sec=float(pw.get("window_sec", 300.0)),
        extract_min_confidence=lo,
        extract_flag_confidence=hi,
        poll_replies_sec=int(pw.get("poll_replies_sec", 120)),
        decision_ttl_days=int(pw.get("decision_ttl_days", 3)),
        dry_run=bool(pw.get("dry_run", True)),
        ledger_base_token=str(pw.get("ledger_base_token", "") or ""),
        ledger_change_table=str(pw.get("ledger_change_table", "") or ""),
        ledger_bind_table=str(pw.get("ledger_bind_table", "") or ""),
        meegle_simple_name=str(pw.get("meegle_simple_name") or "").strip(),
        story_chat_map={str(k): str(v).strip()
                        for k, v in (pw.get("story_chat_map") or {}).items()
                        if str(v).strip()},
        prd_field=str(pw.get("prd_field") or "field_c56ee5").strip(),
        task_doc_field=str(pw.get("task_doc_field") or "wiki").strip(),
        comment_identity=(str(pw.get("comment_identity") or "bot").strip().lower()
                          if str(pw.get("comment_identity") or "bot").strip().lower()
                          in ("bot", "user") else "bot"),
        minutes_identity=(str(pw.get("minutes_identity") or "user").strip().lower()
                          if str(pw.get("minutes_identity") or "user").strip().lower()
                          in ("user", "bot") else "user"),
        minutes_poll_sec=int(pw.get("minutes_poll_sec", 1800)),
        minutes_story_map={str(k): str(v)
                           for k, v in (pw.get("minutes_story_map") or {}).items()},
        minutes_base_url=str(pw.get("minutes_base_url") or "").strip().rstrip("/"),
        notes_base_url=str(pw.get("notes_base_url") or "").strip().rstrip("/"),
        cr_base_token=str(pw.get("cr_base_token") or "").strip(),
        cr_table=str(pw.get("cr_table") or "").strip(),
        kb_base_token=str(pw.get("kb_base_token") or "").strip(),
        kb_story_table=str(pw.get("kb_story_table") or "").strip(),
        kb_meeting_table=str(pw.get("kb_meeting_table") or "").strip(),
        kb_change_table=str(pw.get("kb_change_table") or "").strip(),
        local_ledger=bool(pw.get("local_ledger", True)),
        local_ledger_path=str(pw.get("local_ledger_path") or "").strip(),
        figma_token=str(pw.get("figma_token") or "").strip(),
        meeting_auto=bool(pw.get("meeting_auto", False)),
        meeting_scan_sec=int(pw.get("meeting_scan_sec", 3600)),
        meeting_scan_days=int(pw.get("meeting_scan_days", 7)),
        meeting_scan_limit=int(pw.get("meeting_scan_limit", 5)),
    )


def _load_messaging_config(project: dict) -> MessagingConfig:
    """从 project 配置读取消息聚合（debounce）与回执配置，缺省项回落默认值。"""
    mc = project.get("messaging", {})
    return MessagingConfig(
        enabled=mc.get("enabled", True),
        debounce_sec=float(mc.get("debounce_sec", 3.0)),
        max_wait_sec=float(mc.get("max_wait_sec", 15.0)),
        ack_receipt=bool(mc.get("ack_receipt", True)),
        routed_receipt=bool(mc.get("routed_receipt", True)),
        feedback_buttons=bool(mc.get("feedback_buttons", True)),
    )


def _load_router_config(project: dict) -> RouterConfig:
    """读取 LangGraph 路由配置。

    模型三要素（model/base_url/api_key）单源来自 .env（LLM_MODEL/LLM_BASE_URL/LLM_API_KEY），
    config.toml 不再承载；其余参数仍从 [projects.router] 读取。
    """
    rc = project.get("router", {})
    # 站点级采样覆盖：[projects.router.llm_sites.<site>]，只认 temperature/max_tokens/top_p
    llm_sites = {
        name: LLMSiteParams(
            temperature=s.get("temperature"),
            max_tokens=s.get("max_tokens"),
            top_p=s.get("top_p"),
        )
        for name, s in rc.get("llm_sites", {}).items()
        if isinstance(s, dict)
    }
    return RouterConfig(
        enabled=rc.get("enabled", True),
        model=os.getenv("LLM_MODEL", "deepseek-flash"),
        base_url=os.getenv("LLM_BASE_URL", "https://aigw.example.com/v1"),
        api_key=os.getenv("LLM_API_KEY", ""),
        temperature=rc.get("temperature", 0.0),
        max_tokens=rc.get("max_tokens"),
        top_p=rc.get("top_p"),
        llm_sites=llm_sites,
        timeout_sec=rc.get("timeout_sec", 10),
        max_retries=rc.get("max_retries", 2),
        fallback_on_error=rc.get("fallback_on_error", True),
        history_enabled=rc.get("history_enabled", True),
        history_max_turns=rc.get("history_max_turns", 6),
        history_persist=bool(rc.get("history_persist", True)),
        tool_rate_limit=rc.get("tool_rate_limit", 50),
        tool_rate_window_sec=rc.get("tool_rate_window_sec", 600),
        # 意图识别：clarify 阈值优先 [projects.router_intent]，兼容 [projects.router] 直配
        clarify_threshold=float(
            project.get("router_intent", {}).get(
                "clarify_threshold", rc.get("clarify_threshold", 0.6)
            )
        ),
    )


