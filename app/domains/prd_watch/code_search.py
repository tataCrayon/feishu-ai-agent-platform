"""代码 / 知识资料检索 — PRD 评审的**取证工具**（本地 grep，不走网络）。

边界（与 Skill 的分工）：本模块只回答「这些词在代码里出现在哪」，不判断
「PRD 与代码是否一致」——那是 `bot/skills/prd-evidence/SKILL.md` 的判断，
由执行面模型读命中片段后给出。工具不做判断，判断不写脚本。

为什么用本地 grep 而不是向量检索：评审要的是「找字段/枚举/契约名」这类精确命中，
`grep -F` 的确定性比相似度更可信，也不会把「像」当成「是」。

检索词从 PRD 正文抽，不是从需求标题猜：
- 反引号包住的标识符（`order_status`）权重最高——那本来就是代码名；
- 中文专有名词（状态名、字段名、接口名）次之；
- 单词长度 ≥4 的英文标识符再次之。

代码根可以**多个**：一次需求往往横跨中台（hub-platform）、RWA 引擎（rwa-engine）、
财富侧（wealth-service）几个仓库，只搜一个根就会出现「检索成功但未命中」——
那不是「代码里没有」，是搜错了地方。

根从哪来（优先级从高到低）：
1. 调用方显式给（`roots=`）；
2. 环境变量 `PRD_REVIEW_CODE_ROOTS`（逗号分隔的绝对/相对路径）；
3. 本仓同级目录里**实际存在**的候选集（`_DEFAULT_CANDIDATES`）。

不做的事：不改仓库、不跑构建、不执行代码。只读。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("prd_watch.code_search")

# 本文件在 app/domains/prd_watch/ 下：向上**四层**才是仓库根。
# 写成三层会落到 app/ 上，默认代码根随之错成 app 目录（实测 grep 0 命中）。
PROJECT_DIR = Path(__file__).resolve().parents[3]

# 代码文本后缀：只搜这些，二进制/图片/依赖目录一律跳过
_CODE_SUFFIXES = (".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".kt",
                  ".go", ".sql", ".xml", ".yml", ".yaml", ".json", ".md")
_SKIP_DIRS = ("node_modules", ".git", "dist", "build", "target", ".venv",
              "venv", "__pycache__", ".idea", ".gradle", "logs", "temp")
# 单次调用最多回多少条：够模型判断，又不至于把上下文撑爆
_MAX_HITS = 40
# 默认候选代码根（本仓同级目录）。只取**实际存在**的那些；
# 顺序即优先级，配额按根轮转分配，避免第一个根把条数吃光。
_DEFAULT_CANDIDATES = (
    "webportal-bff",         # WebPortal BFF + RWA 引擎/代币化平台（3 套系统同仓）
    "hub-platform",          # 中台：hub-order / hub-product / hub-asset
    "rwa-engine",        # RWA 引擎（另一份检出）：flp-rwa-engine / flp-rwa-tokenization
    "wealth-service",    # 财富侧：flp-saas-wealth
    "webportal-admin",  # 权限（webportal 管理端）
    "team-skills",   # 团队 skill / 规范
)
# 每个根最多分到几条：多根时保证每个仓都有机会被看到，而不是被最大的仓垄断
_PER_ROOT_CAP = 12
# grep -rn 的输出行：`<路径>:<行号>:<该行文本>`（路径里可能含盘符冒号）
_HIT_RE = re.compile(r"^(.+?):(\d+):(.*)$")

_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,40})`")
_ENUM_RE = re.compile(r"\b([A-Z][A-Z0-9_]{3,30})\b")
_CAMEL_RE = re.compile(r"\b([a-z][a-zA-Z0-9]{3,30})\b")
_CN_TERM_RE = re.compile(r"[一-龥]{2,8}")

# 中文检索词里的「泛词」：命中它们等于命中全文，排除掉
_STOP_CN = {
    "需求", "系统", "功能", "支持", "页面", "用户", "客户", "数据", "文档", "本章",
    "以下", "如下", "本期", "上述", "相关", "处理", "进行", "需要", "可以", "如果",
    "接口", "字段", "状态", "流程", "规则", "设计", "开发", "测试", "上线",
}


@dataclass
class CodeHit:
    """一条检索命中（给模型的就是这四个字段）。"""

    path: str = ""
    line: int = 0
    text: str = ""
    why: str = ""       # 命中了哪个检索词


def extract_terms(text: str, *, limit: int = 12) -> list[str]:
    """PRD 正文 → 检索词（已去重、按确定性排序）。"""
    terms: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if len(value) < 3 or value in terms:
            return
        if value.lower().replace("_", "") in _STOP_CN:
            return
        terms.append(value)

    # 1) 反引号标识符：PRD 里特意加引号的，基本就是代码名
    for m in _BACKTICK_RE.finditer(text or ""):
        add(m.group(1))
    # 2) 大写枚举（含下划线），如 ORDER_STATUS、UN_SETTLED
    for m in _ENUM_RE.finditer(text or ""):
        add(m.group(1))
    # 3) 驼峰小写标识符（length>=4 才收，避免 `true`/`null`）
    for m in _CAMEL_RE.finditer(text or ""):
        word = m.group(1)
        if "_" in word or (word.islower() and len(word) >= 6):
            add(word)
    # 4) 中文专有名词：取出现在「」或【】里的短语，再回退到长词
    for m in re.finditer(r"[「【]([一-龥]{2,8})[」】]", text or ""):
        if m.group(1) not in _STOP_CN:
            add(m.group(1))
    if len(terms) < limit:
        for m in _CN_TERM_RE.finditer(text or ""):
            word = m.group(0)
            if len(word) >= 4 and word not in _STOP_CN and not any(
                    word[:2] == t[:2] for t in terms):
                add(word)
            if len(terms) >= limit:
                break
    return terms[:limit]


def search_terms(terms: list[str], *, root: Path | str | None = None,
                 roots: list[Path | str] | None = None,
                 per_term: int = 3, timeout_sec: float = 10.0,
                 budget_sec: float = 45.0) -> list[CodeHit]:
    """逐个检索词 grep 一次，合并去重。

    三条判读事实（来自真实评审跑批，不是猜的）：

    1. **每词上限必须小**：`per_term` 默认 2，且整次调用有 `budget_sec`（默认 45s）总预算。
       早先按 3 取、六个词不限总时长，在 hub-platform（650MB）上跑到 2 分多钟被调用方判超时，
       执行面看到的是「检索失败」，评审就丢了代码证据。
    2. **先按词轮转，再加深**：一轮里每个词各取第一条，再取第二条……
       这样热门词（`ExampleCorp` 在本仓几乎每个文件都有）不会把配额吃光、把 `<other term>`
       挤成 0 条。
    3. **总条数上限** `_MAX_HITS`，够模型判断即可。
    """
    bases = _resolve_roots(root, roots)
    if not bases:
        logger.info("没有可用的代码根，跳过检索")
        return []

    deadline = time.monotonic() + max(5.0, budget_sec)
    # 词 × 根 的矩阵。查询顺序：先**所有根跑第一个词**，再第二个词——
    # 预算耗尽时得到的是「每个根都有一份证据」，而不是「第一个词在五个根上都很全」。
    # 前者能回答「这份 PRD 的关键标识符在哪个仓落地」，后者只是证明某个词很常见。
    raw: dict[tuple[str, Path], list[tuple[str, int, str]]] = {}
    for term in terms:
        for base in bases:
            if time.monotonic() >= deadline:
                break
            left = deadline - time.monotonic()
            raw[(term, base)] = _grep(term, base, per_term, min(timeout_sec, left))

    hits: list[CodeHit] = []
    seen: set[tuple[str, int]] = set()
    per_root: dict[Path, int] = {b: 0 for b in bases}
    # 轮转顺序：先按「第几条」加深，再换根、换词 —— 任何一个热门词/大仓都无法垄断
    depth = 0
    while len(hits) < _MAX_HITS:
        progressed = False
        for base in bases:
            if per_root[base] >= _PER_ROOT_CAP and len(bases) > 1:
                continue
            for term in terms:
                rows = raw.get((term, base)) or []
                if depth >= len(rows):
                    continue
                progressed = True
                path, line, text = rows[depth]
                key = (str(Path(path).resolve()), line)
                if key in seen:
                    continue
                seen.add(key)
                rel = _join_rel(path, base)
                hits.append(CodeHit(path=rel, line=line, text=text[:400], why=term))
                per_root[base] = per_root.get(base, 0) + 1
                if len(hits) >= _MAX_HITS:
                    return hits
        depth += 1
        if not progressed:
            break
    return hits


def _resolve_roots(root: Path | str | None,
                   roots: list[Path | str] | None) -> list[Path]:
    """显式 roots > 单个 root > 环境变量/候选集。只保留存在的目录。"""
    chosen: list[Path] = []
    candidates: list[Path | str] = []
    if roots:
        candidates = list(roots)
    elif root:
        candidates = [root]
    else:
        return _default_roots()
    for item in candidates:
        path = Path(item).expanduser()
        if path.exists() and path not in chosen:
            chosen.append(path)
    return chosen


def _grep(term: str, base: Path, per_term: int,
          timeout_sec: float) -> list[tuple[str, int, str]]:
    r"""一个检索词 → [(文件, 行号, 该行文本)]。

    四条实现约束（都是实测踩出来的）：

    1. **不用 `--include` 过滤后缀**：Windows 下 Git 自带的 `grep`（MSYS）把多个
       `--include` 连同后面的 `-F` 一起吞掉，最后把检索词当成文件名，报
       `grep: <term>: No such file or directory` 并返回 2。改成只传 `-F`，
       后缀/目录过滤在 Python 侧做。
    2. **不用 `--exclude-dir`**：同理，在 `_keep` 里按路径片段丢。
    3. **输出不能按第一个冒号切**：`D:/Users/...:12:text` 会切成 `D`。
       用锚定正则 `^(.+?):(\d+):(.*)$`。
    4. **超时保留已读部分**：`TimeoutExpired.stdout` 里已有部分结果，丢掉等于
       白跑一趟（大仓库上很常见）；只有 rc=2（真的读不了）才算这个词没结果。
    """
    grep = shutil.which("grep")
    if not grep:
        logger.info("本机没有 grep，跳过代码检索")
        return []
    args = [grep, "-rn", "--binary-files=without-match", "-I",
            "-m", str(max(1, per_term)), "-F", "--", term, str(base)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout_sec, encoding="utf-8",
                              errors="replace")
    except subprocess.TimeoutExpired as e:
        partial = e.stdout if isinstance(e.stdout, str) else ""
        logger.info("grep 超时(%s)，保留已读 %d 行", term,
                    len(partial.splitlines()))
        return _parse_lines(partial)
    except (subprocess.SubprocessError, OSError) as e:
        logger.info("grep 失败(%s): %s", term, str(e)[:80])
        return []
    return _parse_lines(proc.stdout or "")


def _parse_lines(stdout: str) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for raw in (stdout or "").splitlines():
        # 不能按第一个冒号切：Windows 绝对路径形如 `D:/Users/...:12:text`，
        # 切出来 path 只剩 "D"，后续过滤与相对化全错（实测：命中 19 条、
        # 过滤后 0 条，表现为「代码里一个都没有」）。锚定式正则把盘符冒号吞进路径。
        m = _HIT_RE.match(raw)
        if not m:
            continue
        path, line, text = m.group(1), int(m.group(2)), m.group(3)
        if not _keep(path):
            continue
        out.append((path, line, text.strip()))
    return out


def _keep(path: str) -> bool:
    """路径过滤：跳过依赖/构建目录，只留代码文本后缀。"""
    parts = path.replace("\\", "/").split("/")
    if any(part in _SKIP_DIRS for part in parts[:-1]):
        return False
    return path.lower().endswith(_CODE_SUFFIXES)



def _default_roots() -> list[Path]:
    """默认代码根列表（只含实际存在的目录，去重保序）。

    为什么不写死单一绝对路径：换机器/换人时必须能改，而多一条配置就多一处
    会被忘记同步的地方。环境变量优先，其次按本机既有同级目录探测。
    """
    import os

    env = (os.environ.get("PRD_REVIEW_CODE_ROOTS")
           or os.environ.get("PRD_REVIEW_CODE_ROOT") or "").strip()
    if env:
        out: list[Path] = []
        for raw in env.split(","):
            path = Path(raw.strip()).expanduser()
            if path.exists() and path not in out:
                out.append(path)
        if out:
            return out
    sibling = PROJECT_DIR.parent
    found = [sibling / name for name in _DEFAULT_CANDIDATES
             if (sibling / name).is_dir()]
    return found or [PROJECT_DIR]


def _join_rel(path: str, base: Path) -> str:
    """命中文件 → `<根目录名>/<相对路径>`。

    多根时必须带根名：只给相对路径会出现两个仓各有一个 `docs/xxx.md`，
    执行面看到两条一样的 path，无法判断证据来自哪个仓库。
    """
    try:
        rel = Path(path).resolve().relative_to(base.resolve())
        return f"{base.name}/{rel}".replace("\\", "/")
    except (ValueError, OSError):
        return str(path).replace("\\", "/")
