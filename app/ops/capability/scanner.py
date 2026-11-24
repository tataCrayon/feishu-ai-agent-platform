"""能力包静态安全扫描 — 审批人看到内容前的第一道闸。

扫描维度（命中任一 high 即自动拒绝，见设计稿 3.3）：
- 凭据泄漏：明文 password/secret/token/api_key 赋值 + pre-commit 同源的已知凭据模式
- 危险指令：破坏性 SQL/DDL、危险 shell 命令、脚本注入点
- 路径安全：zip-slip（压缩包条目越界路径）
- 结构合规：SKILL.md / agent.yaml 是否存在且 frontmatter 可解析

规则与 .git/hooks/pre-commit、order-sql validator 黑名单语义对齐，
后续新增已知凭据需同步三处（pre-commit / scanner / 文档）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = __import__("logging").getLogger("capability.scanner")

# ── 风险等级 ──
RISK_LOW = 0
RISK_MEDIUM = 1
RISK_HIGH = 2

RISK_LABEL_FALLBACK = "unknown"

_RISK_LABEL = {RISK_LOW: "low", RISK_MEDIUM: "medium", RISK_HIGH: "high"}

# 已知凭据片段（与 .git/hooks/pre-commit 同源维护）
_KNOWN_CREDENTIAL_FRAGMENTS = (
    "3NDxNtOrk6X",
    "Mk2ePO0nm7N",
    "mZ1CP6FEJl",
    "whk-feishu-bot-secret",
    "gxfds72jq",
    "sk-kePKf8",
    "ak-6c364d",  # 他人 key：仅供拦截特征匹配，真实凭据只许在 .env
    "n4ITVPpqy7R",
    "maup2FuMn0W",
    "0KTY25Ksa1J",
    "6Rw4bfIae6S",
    "6JsPt9OCf1U",
)

# 通用明文凭据赋值：password = "..." / secret: xxx / token=yyy（跳过 ${ENV} 占位符）
_CRED_ASSIGN_RE = re.compile(
    r"""(?i)\b(password|passwd|secret|token|api_key|apikey|access_key)\b\s*[:=]\s*
        (?![$\{])["']?([^\s"'{}]{6,})["']?""",
    re.VERBOSE,
)

# 危险 SQL / DDL（与 order-sql validator 黑名单语义对齐）
_DANGEROUS_SQL_RE = re.compile(
    r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE|ALTER\s+TABLE|"
    r"DELETE\s+FROM\s+\w+\s*;?\s*$|UPDATE\s+\w+\s+SET\s+.*;?\s*$|GRANT\s+|REVOKE\s+)",
    re.IGNORECASE | re.MULTILINE,
)

# 危险 shell / 脚本指令
_DANGEROUS_SHELL_RE = re.compile(
    r"\b(rm\s+-rf\s+[~/]|mkfs|dd\s+if=|:\(\)\{.*\};:|curl\s+[^|]*\|\s*(ba)?sh|"
    r"wget\s+[^|]*\|\s*(ba)?sh|chmod\s+777\s+/|reg\s+delete|format\s+c:)",
    re.IGNORECASE,
)

# 出网端点黑名单语义：内网地址/元数据端点不允许出现在能力包里
_SUSPICIOUS_ENDPOINT_RE = re.compile(
    r"(169\.254\.169\.254|metadata\.google\.internal|"
    r"localhost:\d+|127\.0\.0\.1:\d+)",
)


@dataclass
class ScanFinding:
    """单条扫描发现。"""

    severity: int  # RISK_LOW / RISK_MEDIUM / RISK_HIGH
    rule: str  # 规则名，如 "credential-assign"
    detail: str  # 人类可读说明（含文件与行号定位）


@dataclass
class ScanReport:
    """一次扫描的完整报告。"""

    risk_level: int = RISK_LOW
    findings: list[ScanFinding] = field(default_factory=list)
    capability_type: str = ""  # skill / agent / mcp
    capability_name: str = ""
    capability_description: str = ""  # 包内 description 原文（路由注入面，审批卡片展示用）
    parse_ok: bool = True
    parse_error: str = ""

    @property
    def blocked(self) -> bool:
        """high 风险或解析失败 → 自动拒绝。"""
        return self.risk_level >= RISK_HIGH or not self.parse_ok

    def summary_lines(self) -> list[str]:
        """卡片展示用的摘要行。"""
        lines = [f"类型：{self.capability_type or '未知'} / 名称：{self.capability_name or '-'}"]
        if not self.parse_ok:
            lines.append(f"❌ 解析失败：{self.parse_error}")
        if self.findings:
            for f in self.findings:
                icon = {RISK_LOW: "ℹ️", RISK_MEDIUM: "⚠️", RISK_HIGH: "🚨"}.get(f.severity, "•")
                lines.append(f"{icon} [{f.rule}] {f.detail}")
        else:
            lines.append("✅ 静态扫描未发现风险项")
        return lines


class CapabilityScanner:
    """能力包静态扫描器。scan() 返回 ScanReport，不做任何写入。"""

    def scan(self, pkg_dir: Path) -> ScanReport:
        """扫描解包后的能力目录。

        目录约定（与现有注册发现对齐）：
        - Skill 包：pkg_dir/SKILL.md
        - Agent 包：pkg_dir/agent.yaml（MCP 能力即带 mcp_servers 段的 agent.yaml）
        """
        report = ScanReport()
        pkg_dir = pkg_dir.resolve()

        # ── 结构识别 ──
        skill_md = pkg_dir / "SKILL.md"
        agent_yaml = pkg_dir / "agent.yaml"
        if skill_md.exists():
            report.capability_type = "skill"
        elif agent_yaml.exists():
            report.capability_type = "agent"
        else:
            report.parse_ok = False
            report.parse_error = "目录中未找到 SKILL.md 或 agent.yaml"
            report.risk_level = RISK_HIGH
            return report

        # ── 逐文件扫描（文本文件按行检查）──
        text_suffixes = {".md", ".yaml", ".yml", ".json", ".toml", ".py", ".txt", ".sql", ".sh"}
        for path in sorted(pkg_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = path.relative_to(pkg_dir)
            self._scan_text(content, rel, report)

        # ── frontmatter 结构校验 ──
        self._validate_structure(pkg_dir, report)

        report.risk_level = max(
            (f.severity for f in report.findings), default=RISK_LOW
        )
        return report

    # ── 内部：逐条规则 ──

    def _scan_text(self, content: str, rel: Path, report: ScanReport) -> None:
        for i, line in enumerate(content.splitlines(), start=1):
            loc = f"{rel}:{i}"

            for frag in _KNOWN_CREDENTIAL_FRAGMENTS:
                if frag in line:
                    report.findings.append(ScanFinding(
                        RISK_HIGH, "known-credential", f"{loc} 命中已知凭据模式",
                    ))
                    break

            m = _CRED_ASSIGN_RE.search(line)
            if m:
                report.findings.append(ScanFinding(
                    RISK_HIGH, "credential-assign",
                    f"{loc} 疑似明文凭据赋值（字段 {m.group(1)}）；"
                    f"凭据请写入 .env，包内用 ${{{m.group(1).upper()}}} 占位符",
                ))

            if _DANGEROUS_SQL_RE.search(line):
                report.findings.append(ScanFinding(
                    RISK_MEDIUM, "dangerous-sql", f"{loc} 含破坏性 SQL 语句",
                ))

            if _DANGEROUS_SHELL_RE.search(line):
                report.findings.append(ScanFinding(
                    RISK_HIGH, "dangerous-shell", f"{loc} 含危险 shell 指令",
                ))

            if _SUSPICIOUS_ENDPOINT_RE.search(line):
                report.findings.append(ScanFinding(
                    RISK_MEDIUM, "suspicious-endpoint",
                    f"{loc} 引用本机/元数据端点，请确认必要性",
                ))

    def _validate_structure(self, pkg_dir: Path, report: ScanReport) -> None:
        try:
            if report.capability_type == "skill":
                meta = self._read_frontmatter(pkg_dir / "SKILL.md")
                if meta is None:
                    raise ValueError("SKILL.md 缺少合法 frontmatter")
                name = str(meta.get("name") or pkg_dir.name).strip()
                if not str(meta.get("description", "")).strip():
                    raise ValueError("SKILL.md 缺少 description（路由工具描述不能为空）")
                report.capability_description = str(meta.get("description", "")).strip()
            else:
                raw = yaml.safe_load((pkg_dir / "agent.yaml").read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not raw.get("name"):
                    raise ValueError("agent.yaml 缺少 name 字段")
                name = str(raw["name"]).strip()
                if not str(raw.get("description", "")).strip():
                    raise ValueError("agent.yaml 缺少 description（路由工具描述不能为空）")
                report.capability_description = str(raw.get("description", "")).strip()
                # D2：MCP 只允许通过新建 Agent 包承载——天然满足；额外校验
                # mcp_servers 凭据必须是 ${ENV} 占位符
                for srv_name, cfg in (raw.get("mcp_servers") or {}).items():
                    if not isinstance(cfg, dict):
                        continue
                    for k, v in (cfg.get("env") or {}).items():
                        if isinstance(v, str) and v and not v.startswith("${"):
                            raise ValueError(
                                f"mcp_servers.{srv_name}.env.{k} 疑似明文凭据，"
                                f"必须使用 ${{ENV_NAME}} 占位符"
                            )
            report.capability_name = name
        except Exception as e:
            report.parse_ok = False
            report.parse_error = str(e)
            report.findings.append(ScanFinding(RISK_HIGH, "invalid-structure", str(e)))

    @staticmethod
    def _read_frontmatter(path: Path) -> dict | None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return None
        return meta if isinstance(meta, dict) else None


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> list[str]:
    """zip-slip 安全解压：拒绝绝对路径与 .. 越界条目。

    Returns:
        解压出的文件相对路径列表。

    Raises:
        ValueError: 条目路径越界时（调用方应视为 high 风险拒绝）。
    """
    import zipfile

    extracted: list[str] = []
    dest = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise ValueError(f"zip 条目路径越界：{name!r}（zip-slip）")
            if info.is_dir():
                (dest / name).mkdir(parents=True, exist_ok=True)
                continue
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest)):
                raise ValueError(f"zip 条目路径越界：{name!r}（zip-slip）")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            extracted.append(name)
    return extracted
