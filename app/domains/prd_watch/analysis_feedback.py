"""分析意见的人工反馈：逐条打分 + 「采纳即落 PRD 评论」的登记簿。

为什么需要登记簿：卡片按钮回调只带回一个 id，不回带行数据。要让「采纳」真的
写出评论，回调时必须能查到这条意见的**原文块 id + PRD token + 建议表述**，
所以发卡片的那一刻把条目登记下来。

为什么打分独立于采纳：两条不同的信号。
- 采纳 = 这条意见**要落地**（写评论、改 PRD），有副作用；
- 🙂/🙃 = 这条意见**判得准不准**，无副作用，是喂给检查规则的准确率样本。
  采纳但写错了、误报了但看起来合理，都会发生；把两者合成一个按钮就会把
  准确率算成「有多少人愿意动手」。

落盘（`logs/` 已 gitignore）：登记簿按 Story 一个 JSON（重启后老卡片仍可点），
打分是只追加的 JSONL（不会被后写覆盖，能直接算准确率）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("prd_watch.analysis_feedback")

_DEFAULT_DIR = Path("logs/prd_watch")


@dataclass
class AnalysisItem:
    """一条分析意见（卡片按钮要能据此写评论，字段必须自包含）。"""

    item_id: str = ""
    story_id: str = ""
    story_title: str = ""
    kind: str = ""
    title: str = ""
    quote: str = ""
    suggestion: str = ""
    block_id: str = ""
    section: str = ""
    prd_token: str = ""
    prd_url: str = ""
    # 评审卡片专用：分级（阻断/待确认/建议）与「凭什么」（判定维度 + 证据出处）。
    # 需求分析卡的条目留空，评论正文就退回机械自检那套说法。
    level: str = ""
    basis: str = ""

    def comment_body(self) -> str:
        """写进 PRD 的评论正文（纯文本：飞书评论不渲染 markdown）。"""
        head = f"🤖 Bot EasyPRD 分析意见 · {self.item_id}"
        if self.level:
            head = f"🤖 Bot EasyPRD 评审意见（{self.level}）· {self.item_id}"
        lines = [
            head,
            "",
            "【发现的问题】",
            f"  {self.title or '（未命名）'}",
            f"  类型：{self.kind or '自检'}"
            + (f" · 位置：{self.section}" if self.section else ""),
            "",
            "【当前PRD原文】",
            f"  {self.quote or '（未定位到原文，仅给出建议）'}",
            "",
            "【建议调整为】",
            f"  {self.suggestion or '（未给出可替换表述）'}",
            "",
            "【凭什么】",
            f"  {self.basis or 'PRD 机械自检（数量/集合/删除线/术语/状态覆盖/放行词/跨章节一致性）'}",
            "",
            "【谁来定】",
            "  本评论由群卡片「采纳」生成；正文请产品/研发按建议更新，Bot 不改正文。",
        ]
        if self.prd_url:
            lines += ["", f"需求：{self.prd_url}"]
        return "\n".join(lines)


@dataclass
class FeedbackStats:
    """打分汇总：按意见类型算准确率（误报率高说明该条规则要收窄）。"""

    useful: int = 0
    false_positive: int = 0
    adopted: int = 0
    by_kind: dict[str, list[int]] = field(default_factory=dict)   # kind → [有用, 误报]

    @property
    def rated(self) -> int:
        return self.useful + self.false_positive

    @property
    def accuracy(self) -> float:
        return self.useful / self.rated if self.rated else 0.0


class AnalysisFeedback:
    """登记簿 + 打分区。线程/协程安全性：单进程内串行回调，直接读写即可。

    ## 为什么要按文件 mtime 重载

    登记簿是**两个进程共用的文件**：评审卡由 dsh 的 MCP 子进程登记（`render_review_card`），
    按钮回调落在 main 进程（`_on_prd_card_action`）。只在 `__init__` 读一次的话，
    主进程内存里永远只有「启动那一刻」的内容——发卡之后才登记的条目一律查不到，
    用户看到的是「⚠️ 找不到 xxx 的登记记录」，而文件里明明有。

    所以每次查不到就按 mtime 重读一次（文件没变就不读盘）；读到之后再查。
    这也顺带解决了「发卡那一刻登记、点击时进程还没重启」这种最常见的情形。
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        base = Path(directory) if directory else _DEFAULT_DIR
        self._items_path = base / "analysis_items.json"
        self._log_path = base / "analysis_feedback.jsonl"
        self._items: dict[str, AnalysisItem] = {}
        self._mtime: float = 0.0
        self._load()

    # ── 登记 ────────────────────────────────────────────────────────
    def register(self, digest: Any) -> list[AnalysisItem]:
        """把一张分析卡片的意见登记下来（id 与卡片按钮同源，见 `ordered_findings`）。"""
        from app.domains.prd_watch.analysis_card import ordered_findings

        items: list[AnalysisItem] = []
        for index, finding in ordered_findings(digest):
            item_id = item_id_for(digest.story_id, index)
            items.append(AnalysisItem(
                item_id=item_id,
                story_id=str(digest.story_id or ""),
                story_title=str(getattr(digest, "title", "") or ""),
                kind=str(getattr(finding, "kind", "") or ""),
                title=str(getattr(finding, "title", "") or ""),
                quote=str(getattr(finding, "quote", "") or ""),
                suggestion=str(getattr(finding, "suggestion", "") or ""),
                block_id=str(getattr(finding, "block_id", "") or ""),
                section=str(getattr(finding, "section", "") or ""),
                prd_token=str(getattr(digest, "prd_token", "") or ""),
                prd_url=str(getattr(digest, "prd_url", "") or ""),
            ))
        for item in items:
            self._items[item.item_id] = item
        if items:
            self._save()
        return items

    def get(self, item_id: str) -> AnalysisItem | None:
        """查登记条目；内存里没有就按磁盘重载一次再查。

        重载是**跨进程**的唯一通路（见类 docstring）：发卡在 MCP 子进程、点击在 main。
        只补「没过期」的情形：文件没变就不重复读盘，避免每次点击都 IO。
        """
        item = self._items.get(item_id)
        if item is not None:
            return item
        if self._reload_if_changed():
            return self._items.get(item_id)
        return None

    def find_by_code(self, code: str) -> list[AnalysisItem]:
        """按**卡片上显示的短编号**找条目（`B1` / `Q1` / `S1` / `01`）。

        文字指令（卡片被转发后按钮点不动）只能读到人看到的那一行，人不会知道
        登记簿里带文档键的完整 id（`RV-<docKey>-B1`）。所以按后缀匹配；
        多篇文档有同名编号时返回多条，由调用方如实列出来让人补全。
        """
        needle = str(code or "").strip().upper()
        if not needle:
            return []
        self._reload_if_changed()
        return [it for it in self._items.values()
                if it.item_id.upper().endswith(f"-{needle}")]

    def register_items(self, items: list[AnalysisItem]) -> list[AnalysisItem]:
        """登记一批**已构造好**的条目（评审卡片用：编号与渲染同源，见 `review_card`）。

        写盘前先合并别的新增：登记簿是两个进程共写的，直接覆盖会把另一个进程
        刚登记的条目丢掉（MCP 子进程发卡、main 也在登记评分时就会撞上）。
        """
        self._reload_if_changed()
        for item in items:
            if item.item_id:
                self._items[item.item_id] = item
        if items:
            self._save()
        return items

    # ── 打分 / 采纳 ─────────────────────────────────────────────────
    def record(self, item_id: str, act: str, *, operator: str = "") -> bool:
        """追加一条反馈（`useful` / `false` / `adopt`）。失败只记日志，不阻断回执。"""
        item = self.get(item_id)
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            line = {"item_id": item_id, "act": act, "operator": operator,
                    "kind": item.kind if item else "", "title": item.title if item else ""}
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            logger.info("分析反馈写入失败(%s): %s", item_id, str(e)[:120])
            return False

    def stats(self) -> FeedbackStats:
        stat = FeedbackStats()
        try:
            text = self._log_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return stat
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            act = str(row.get("act") or "")
            kind = str(row.get("kind") or "其他")
            bucket = stat.by_kind.setdefault(kind, [0, 0])
            if act == "useful":
                stat.useful += 1
                bucket[0] += 1
            elif act == "false":
                stat.false_positive += 1
                bucket[1] += 1
            elif act == "adopt":
                stat.adopted += 1
        return stat

    def summary_line(self) -> str:
        """一行给状态面板用：别把「没人评过」说成「准确率 0%」。"""
        stat = self.stats()
        if not stat.rated and not stat.adopted:
            return "分析意见反馈：暂无评分"
        worst = sorted(((k, v[1]) for k, v in stat.by_kind.items() if v[1]),
                       key=lambda kv: -kv[1])[:2]
        tail = "｜误报最多：" + "、".join(f"{k} {n} 条" for k, n in worst) if worst else ""
        return (f"分析意见反馈：有用 {stat.useful} / 误报 {stat.false_positive}"
                f"（准确率 {stat.accuracy:.0%}）/ 已采纳写评论 {stat.adopted}{tail}")

    # ── 持久化 ──────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            self._mtime = self._items_path.stat().st_mtime
            raw = self._items_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        for row in (data.get("items") if isinstance(data, dict) else data) or []:
            try:
                item = AnalysisItem(**{k: str(v or "") for k, v in row.items()})
            except TypeError:      # 结构对不上（人工改过文件）→ 丢弃，不影响其余
                continue
            if item.item_id:
                self._items[item.item_id] = item

    def _reload_if_changed(self) -> bool:
        """磁盘比内存新才重读；返回是否真的重读过。

        只**合并**不覆盖：本进程可能刚登记过、还没落盘的条目不能被别人的旧快照冲掉
        （两个进程并发注册时，后读的一方不该把另一方丢掉的条目清空）。
        """
        try:
            mtime = self._items_path.stat().st_mtime
        except OSError:
            return False
        if mtime <= self._mtime:
            return False
        try:
            raw = self._items_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        self._mtime = mtime
        for row in (data.get("items") if isinstance(data, dict) else data) or []:
            try:
                item = AnalysisItem(**{k: str(v or "") for k, v in row.items()})
            except TypeError:
                continue
            if item.item_id and item.item_id not in self._items:
                self._items[item.item_id] = item
        return True

    def _save(self) -> None:
        try:
            self._items_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"items": [asdict(i) for i in self._items.values()]}
            self._items_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            self._mtime = self._items_path.stat().st_mtime
        except OSError as e:
            logger.info("分析意见登记簿写入失败: %s", str(e)[:120])


def item_id_for(story_id: str, index: int) -> str:
    """`AN-<StoryID>-<序号>`：与讨论产生的 `CR-` 前缀区分，统计时能分开算。"""
    return f"AN-{story_id}-{index:02d}"
