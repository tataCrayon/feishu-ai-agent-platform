"""业务范围边界 —— 李四负责公募 / 私募 / ESOP / 票据四条产品线。

这个模块是「该不该推给他」的唯一判定来源。pm_watch（群消息感知）和
pm_daily_brief（日计划渲染）都从这里取规则，避免同一套边界散在多个模块里
各写一遍，然后慢慢漂移成三套互相矛盾的版本。

规则调整只改这里。他换职责、加业务线时，同步更新 MY_DOMAINS 和
FOCUS_CHAT_KEYWORDS 两处即可。
"""

from typing import Optional, Tuple

# 我负责的产品线 / 客户 / 项目关键词
# 票据：归他管（曾明确不归他，已从排除表里拿掉并转正）。
MY_DOMAINS: Tuple[str, ...] = (
    "公募", "私募", "ESOP", "esop", "员工持股",
    "票据", "商票", "银票", "电票",
    "Webportal", "webportal", "WebPortal",
    "华夏", "哈富", "建银", "私募白标",
)

# 明确不归我管的项目或业务线。出现即跳过，优先级高于 MY_DOMAINS。
# 星企通、7709：他明确说过不属于他。加之前先确认，不要凭消息内容推断归属。
OUT_OF_SCOPE: Tuple[str, ...] = (
    "星企通",
    "7709",
)
# 整体静音的群（群名包含即跳过，只放行 @我 和私聊）。
# 全员群、闲聊群、职场群信息密度太低，逐条判相关性纯属浪费。
MUTED_CHAT_KEYWORDS: Tuple[str, ...] = (
    "吐槽", "大晨会", "职场群", "本地群",
)

# 高关注群：群名命中这些词的，消息只要带交付动作就值得看一眼。
# 别用太泛的词（「产研」这类），会把战略机构部、流程通知全带进来。
FOCUS_CHAT_KEYWORDS: Tuple[str, ...] = (
    "项目组", "私募", "华夏", "ESOP", "版本规划", "需求版本",
    "哈富", "建银", "测试环境", "票据",
)


def out_of_scope_hit(text: str) -> Optional[str]:
    """命中明确不归我管的业务线时返回那个词，否则 None。

    日计划这类「本来就是我的表」的场景只用这一个函数做减法，
    不做白名单 —— 表里有些需求标题没有业务线字样，白名单会误删。
    """
    if not text:
        return None
    return next((k for k in OUT_OF_SCOPE if k in text), None)


def domain_of(text: str) -> Optional[str]:
    """返回文本落在我哪个业务域，判不出来返回 None。"""
    if not text:
        return None
    return next((d for d in MY_DOMAINS if d in text), None)


def in_scope(text: str) -> bool:
    """文本是否属于我的职责范围。排除项优先。"""
    if out_of_scope_hit(text):
        return False
    return domain_of(text) is not None


def is_muted_chat(chat_name: str) -> bool:
    return any(k in (chat_name or "") for k in MUTED_CHAT_KEYWORDS)


def is_focus_chat(chat_name: str) -> bool:
    return any(k in (chat_name or "") for k in FOCUS_CHAT_KEYWORDS)