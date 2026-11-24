"""路由规则 → dsh 会话系统提示词 — 规则单源读取与读取失败语义。

为什么不是「首条 user 消息」：规则是**系统侧约束**，塞成 user 发言时模型会把
它当「别人说过的一段话」——既可能被后续轮次冲淡，也会在会话压缩时被当成普通
对话内容裁剪。dsh 的 system prompt 有专门的 persona 段
（`@deepseek-ai/dsh-system-prompt` 的 `personaSuffix`，order 10200，在首方
guidance 之后、工具说明之前），规则放那里才是它该在的位置。

注入点：`deploy/dsh/agentbiz/cordis.patch.yml` 的 `system-prompt` 行用
`!!js` 读本文件全文（yml 里没法写 9KB 正文，也避免出现两份规则）。所以本模块
的职责只剩「读文件 + 失败语义」，装配在 profile 侧完成。

为什么不恢复 `dsh-agent-instructions`：那个插件会把操作者个人的
`~/.agents/AGENTS.md` 一并载入（spec AC-5.1 的同一根因），线上行为不可控。

规则单源：`bot/prompts/router/dsh_routing_rules.md`。改规则只改这个文件。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

RULES_PATH = PROJECT_DIR / "bot" / "prompts" / "router" / "dsh_routing_rules.md"


class RoutingRulesUnavailable(RuntimeError):
    """路由规则文件缺失或为空 — 启动期错误，不允许静默降级成「无规则」。"""


def load_rules(path: Path = RULES_PATH) -> str:
    """读规则正文（含 frontmatter）：缺失或空文件抛错。

    保留 frontmatter 是有意的：version/changelog 会进系统提示词，模型在自述
    「我按第几版规则办事」时能给出版本号，排障时对得上 prompt 门禁的基线。
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RoutingRulesUnavailable(f"读不到路由规则文件 {path}: {e}") from e
    if not text:
        raise RoutingRulesUnavailable(f"路由规则文件为空: {path}")
    return text


def build_injection(path: Path = RULES_PATH) -> str:
    """兼容旧调用方的纯文本形态（不含首尾标记，直接作为系统提示词正文用）。

    历史上这里返回「首条 user 消息」用的带标记文本；v2.0.0 起规则进系统提示词，
    标记已无意义，保留函数名是因为 `router_intent_eval.py` 等回归脚本按它取正文。
    """
    return load_rules(path)
