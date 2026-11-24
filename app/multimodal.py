"""多模态消息支持 — 图片/视频/文档/音频统一识别注入。

链路：InboundMessage.resources（飞书 SDK 拆出的并行媒体描述列表）
  → download_media_resources()（下载到内存，按类型分流）
  → build_multimodal_content()（构造 OpenAI 图文混合 content 数组）
  → router / direct_answer / decompose 的 chat.completions.create

实测依据（aigw + qwen3.8-flash）：
- 图片：png/jpg/gif/bmp/webp 全支持，多图混合支持，function calling 兼容
  （image_tokens 计费：64px≈66，4096px≈2502）
- 视频：video_url 形态支持（5s≈322 video_tokens，30s≈2402；<2s 报"too short"）
- 音频：qwen3.8-flash 不吃 audio content，网关 /audio/transcriptions 500
  （not implemented），无可用 omni/ASR 模型 → 诚实降级告知
- 文档（xlsx/docx/pdf/txt 等）：模型不接受非图片 MIME → 本地解析转文本注入

设计约束：
- 下载失败/解析失败不阻断主链路，降级为占位说明（用户可重发）
- 图片/视频大小上限（防 base64 撑爆请求体与 token 计费）
- 图片超尺寸用 PIL 等比缩小（截图常见 1080p+，缩到长边 2048px）
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("multimodal")

# ── 限制 ────────────────────────────────────────────────────────────────
_MAX_IMAGE_BYTES = 10 * 1024 * 1024   # 单图 10MB（base64 后 ~13MB）
_MAX_VIDEO_BYTES = 30 * 1024 * 1024   # 单视频 30MB
_MAX_IMAGE_PIXELS = 2048              # 长边超限等比缩小（省 image_tokens）
_MIN_VIDEO_SECONDS = 2                # 模型要求 >2s（实测 1s 报 too short）
_MAX_VIDEO_SECONDS = 300              # 5 分钟封顶（更长视频截断）

_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/webp"}

# 文档扩展名 → 解析方式（与 channel.download_resources 白名单对齐并扩展）。
# 收录判据：**能抽出可读文本**。二进制但无文本层的（图片/视频/压缩包）不进这里，
# 它们各有自己的分流分支或被降级成说明。
_DOC_EXTS = {
    # 纯文本：代码/配置/数据
    ".md", ".txt", ".csv", ".tsv", ".log", ".json", ".jsonl", ".sql", ".py",
    ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".conf", ".properties",
    ".sh", ".bat", ".ps1", ".java", ".js", ".ts", ".go", ".rs", ".c", ".h",
    ".cpp", ".toml", ".ipynb",
    # Office / 文档
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf",
    ".odt", ".ods", ".odp", ".rtf",
    # 邮件
    ".msg", ".eml",
}

# 单文件解析上限：一份 200 页 PDF 全文塞进上下文会直接顶爆预算，而模型并不需要
# 每一页。截断而不是拒绝——用户至少能问「前面那部分」。总量另有上限（见
# download_media_bundle），因为一条消息可以挂 9 个附件。
_MAX_DOC_CHARS = 30000
_MAX_TOTAL_DOC_CHARS = 60000

# 文本解码的候选编码：中文环境最常见的是 utf-8 / gbk，剩下交给 charset 探测。
# 只试 utf-8 会漏掉 Windows 导出的 GBK CSV（表现为「无法解码」这种无从下手的失败）。
_TEXT_ENCODINGS = ("utf-8", "gbk", "utf-16")


@dataclass
class MediaPart:
    """一个媒体资源的注入形态。

    kind:
      - image / video: data 是 OpenAI content part dict（image_url / video_url）
      - text: data 是解析出的文本（文档类）
      - note: data 是给用户/模型的说明（音频降级、失败占位）
    """

    kind: str  # "image" | "video" | "text" | "note"
    data: Any  # image/video → dict content part；text/note → str
    name: str = ""


@dataclass
class MediaBundle:
    """一条消息的全部媒体资源（下载+解析结果）。"""

    parts: list[MediaPart] = field(default_factory=list)

    @property
    def has_media(self) -> bool:
        return bool(self.parts)

    def notes(self) -> list[str]:
        """note 类文本行（拼进 user 文本帧，让模型知道有什么媒体）。"""
        return [str(p.data) for p in self.parts if p.kind == "note"]

    def texts(self) -> list[str]:
        """文档解析文本（拼进 user 文本帧）。"""
        return [str(p.data) for p in self.parts if p.kind == "text"]

    def content_parts(self) -> list[dict]:
        """image/video 的 OpenAI content parts（进 content 数组）。"""
        return [p.data for p in self.parts if p.kind in ("image", "video")]


def _guess_mime(file_name: str, content_type: str | None) -> str:
    if content_type and "/" in content_type:
        return content_type.split(";")[0].strip().lower()
    guessed = mimetypes.guess_type(file_name)[0]
    return guessed or "application/octet-stream"


def _shrink_image(raw: bytes, mime: str) -> tuple[bytes, str]:
    """长边超限时等比缩小（截图 token 优化）；失败原样返回。"""
    if mime == "image/gif":
        return raw, mime  # 动图不缩（PIL 缩会丢帧）
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        img: Any = PILImage.open(BytesIO(raw))
        w, h = img.size
        if max(w, h) <= _MAX_IMAGE_PIXELS:
            return raw, mime
        scale = _MAX_IMAGE_PIXELS / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)))
        out = BytesIO()
        fmt = "PNG" if mime == "image/png" else "JPEG"
        out_mime = "image/png" if fmt == "PNG" else "image/jpeg"
        img.save(out, fmt)
        shrunk = out.getvalue()
        logger.info("Image shrunk %dx%d → %dx%d (%d → %d bytes)",
                    w, h, img.size[0], img.size[1], len(raw), len(shrunk))
        return shrunk, out_mime
    except Exception:
        return raw, mime


def _video_duration(raw: bytes) -> float:
    """探测视频时长（秒）；失败返回 0（视为未知，放行）。"""
    try:
        import io

        import av

        c: Any = av.open(io.BytesIO(raw))
        dur = 0.0
        if c.duration:
            dur = c.duration / 1_000_000
        c.close()
        return dur
    except Exception:
        return 0.0


def _read_plain_document(path: Path) -> str:
    """无引擎文档解析：txt/md/csv/json/sql 直读、pdf 文本层、.msg/.eml 邮件。

    工单引擎已迁出为独立 A2A 服务（webportal-order-agent，v3.0.0 去 vendor），
    multimodal 作为通道层文档解析器自带实现。pdf 不做视觉回退（本模块调用点
    无 LLM 客户端，扫描件解析为空由调用方降级 note）；图片资源不进本函数
    （download_media_bundle 对 image 走 vision parts 分流）。
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join((page.extract_text() or "")
                         for page in PdfReader(str(path)).pages).strip()

    if suffix == ".eml":
        import email
        from email import policy

        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        return (f"主题: {msg.get('subject', '')}\n发件人: {msg.get('from', '')}\n"
                f"日期: {msg.get('date', '')}\n\n{text}")

    if suffix == ".msg":
        try:
            import extract_msg
        except ImportError:
            raise RuntimeError(
                "解析 .msg 需要 extract-msg（pip install extract-msg）") from None
        m = extract_msg.Message(str(path))
        msg_body = (m.body or "").strip()
        return (f"主题: {m.subject or ''}\n发件人: {m.sender or ''}\n"
                f"日期: {m.date or ''}\n\n{msg_body}")

    if suffix in (".html", ".htm"):
        return _html_to_text(path)

    if suffix in (".doc", ".rtf"):
        return _legacy_word_text(path, suffix)

    if suffix in (".pptx", ".odt", ".ods", ".odp"):
        # 容器格式：直接读字节交给内存解析器，不为「有没有落盘」多写一套。
        # 走到这里说明调用方从文件路径调，而不是 _parse_document 的内存路径。
        return _parse_document(path.read_bytes(), path.name)

    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        raise RuntimeError("图片类型不走文档解析（由 vision 分支处理）")

    # 未知编码兜底：utf-8/gbk/utf-16 全失败时用 charset_normalizer 猜。
    # 猜不出来才报错——识别错编码至少能得到可读文本，直接放弃则整份附件白传。
    for encoding in _TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    try:
        from charset_normalizer import from_path

        best = from_path(str(path), cp_isolation=None).best()
        if best is not None:
            logger.info("文档 %s 编码探测为 %s", path.name, best.encoding)
            return str(best)
    except Exception:
        logger.debug("charset 探测失败: %s", path.name, exc_info=True)
    raise RuntimeError(f"无法解码文件（已尝试 {'/'.join(_TEXT_ENCODINGS)} 与自动探测）：{path.name}")


def _html_to_text(path: Path) -> str:
    """HTML → 纯文本：去脚本/样式，单元格之间补分隔符。

    直接喂标签会让模型把 `<td>` 当正文读（表格结构反而丢失）。有 bs4 用 bs4，
    没有就退回正则粗剥——飞书导出的 HTML 结构化程度低，正则也够用。
    """
    raw = path.read_bytes()
    text = ""
    for encoding in _TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        # 单元格之间补分隔符：bs4 的 get_text 默认每个 text 节点单独成行，
        # 表格会变成「A \n|\n B」这种竖排。这里用空格拼接，再按行收敛。
        for cell in soup.find_all(["td", "th"]):
            cell.append(" | ")
        body = soup.get_text(" ", strip=True)
        return re.sub(r"[ \t]*\|[ \t]*", " | ", body).replace(" | | ", " | ")
    except ImportError:
        stripped = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        stripped = re.sub(r"(?i)</t[dh]>", " | ", stripped)
        stripped = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", stripped)
        stripped = re.sub(r"<[^>]+>", "", stripped)
        return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def _legacy_word_text(path: Path, suffix: str) -> str:
    """老式 Word（.doc / .rtf）→ 文本。

    没有纯 Python 的可靠解析器：.doc 是 OLE 复合文档，.rtf 是控制字格式。
    RTF 先本地解析（格式简单，不依赖外部程序）；解析不出字再交给 antiword，
    因为 antiword 对带大量中文转义的老 RTF 反而更稳。
    `\\'hh` 是**按当前代码页**编码的字节，正确做法是攒起来整体解码（中文 RTF 里
    一个汉字是两三个 `\\'hh`），逐字节 chr() 只会得到乱码。
    """
    import subprocess

    raw_bytes = path.read_bytes()

    if suffix == ".rtf":
        text = _rtf_to_text(raw_bytes)
        if text.strip():
            return text

    antiword = shutil.which("antiword")
    if antiword:
        try:
            out = subprocess.run(
                [antiword, "-w", "0", str(path)],
                capture_output=True, timeout=60, check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return _decode_text(out.stdout)
        except (OSError, subprocess.SubprocessError):
            logger.debug("antiword 调用失败: %s", path.name, exc_info=True)

    if suffix == ".rtf":
        return _rtf_to_text(raw_bytes)
    raise RuntimeError("解析 .doc 需要 antiword（未安装或执行失败），建议另存为 .docx 后重发")


def _rtf_to_text(raw: bytes) -> str:
    """RTF → 纯文本（粗剥：去控制字、还原 \\uN 与 \\'hh 转义）。

    `\\'hh` 是按**当前代码页**编码的字节：中文 RTF 里一个汉字是连续两三个
    `\\'hh`，必须攒成字节串再整体解码。逐字节 chr() 会得到「ä¸\x96ç」这种乱码，
    而且是静默的——模型会当真地去分析乱码。
    """
    text = raw.decode("latin-1", errors="replace")

    def _uni(m: re.Match[str]) -> str:
        code = int(m.group(1))
        return chr(code if code >= 0 else code + 65536)

    def _hex_runs(match: re.Match[str]) -> str:
        runs = re.findall(r"\\'([0-9a-fA-F]{2})", match.group(0))
        return _decode_text(bytes(int(r, 16) for r in runs))

    text = re.sub(r"\\u(-?\d+)\??", _uni, text)
    text = re.sub(r"(?:\\'[0-9a-fA-F]{2})+", _hex_runs, text)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)          # 丢弃 \* 目标组（字体表等）
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)      # 其余控制字
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_document(raw: bytes, file_name: str) -> str:
    """文档 → 文本。

    按容器格式分流，每种都直读到「段落 + 表格」级别：

    | 格式 | 解析器 | 为什么不用别的 |
    |---|---|---|
    | xlsx/xlsm | openpyxl | 只读模式流式读，大表不炸内存 |
    | xls（BIFF） | xlrd 2.x | openpyxl 不认老格式（抛 BadZipFile） |
    | docx | python-docx | 段落 + 表格都要，纯文本会丢表格结构 |
    | doc / rtf | antiword → RTF 粗剥 | 没有纯 Python 的 .doc 可靠解析器 |
    | pptx | python-pptx | 按页抽标题/正文/表格，页序即讲述顺序 |
    | odt / ods / odp | odfpy | ODF 是另一套容器，别指望 Office 库 |
    | pdf | pypdf 文本层 | 扫描件无文本层，由调用方降级说明 |
    | html | bs4 → 正则 | 表格单元格要留分隔符 |
    | 其余 | 编码探测直读 | csv/json/sql/代码… |

    失败一律抛异常，由 `download_media_bundle` 转成 note——**不静默返回空串**，
    空串会让模型以为附件是空的。
    """
    suffix = Path(file_name).suffix.lower()
    budget = _MAX_DOC_CHARS

    if suffix in (".xlsx", ".xlsm"):
        return _xlsx_text(raw)[:budget]

    if suffix == ".xls":
        return _xls_text(raw)[:budget]

    if suffix == ".docx":
        return _docx_text(raw)[:budget]

    if suffix == ".pptx":
        return _pptx_text(raw)[:budget]

    if suffix in (".ppt", ".odt", ".ods", ".odp"):
        # 没有可靠的纯 Python 解析器：明确报错让用户另存为新格式，
        # 比返回一坨二进制乱码诚实（.doc 另有 antiword 兜底）。
        if suffix.startswith(".od"):
            return _odf_text(raw, suffix)[:budget]
        raise RuntimeError(
            f"暂不支持解析 {suffix}（老式二进制格式），请另存为 .pptx 后重发")

    if suffix in _TEXT_LIKE:
        return _decode_text(raw)[:budget]

    # 不在名单里的格式：先按文本试一次（很多人把 .csv 存成 .dat、.log 存成 .out），
    # 二进制就明确报错。绝不静默返回空串——那会让模型以为附件是空的。
    if suffix in (".pdf", ".msg", ".eml") or suffix in _CONTAINER_FALLBACK:
        pass  # 下面落盘走通用解析
    else:
        try:
            return _decode_text(raw)[:budget]
        except RuntimeError:
            raise RuntimeError(
                f"暂不支持解析 {suffix or '（无后缀）'} 格式，"
                "可转为 pdf/docx/txt/csv/xlsx 后重发") from None

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(raw)
        tmp = Path(fh.name)
    try:
        return _read_plain_document(tmp)[:budget]
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# 有容器结构、必须落盘（或走专用路径）解析的后缀。其余未知后缀先按文本试，
# 二进制才拒绝——把 .csv 存成 .dat 的人比想象中多。
_CONTAINER_FALLBACK = {
    ".pdf", ".msg", ".eml", ".doc", ".docx", ".xlsx", ".xls", ".pptx", ".ppt",
    ".odt", ".ods", ".odp", ".html", ".htm", ".rtf",
}

# 没有容器结构、直接解码就能读的后缀（省掉落盘再读一遍）。
_TEXT_LIKE = {
    ".md", ".txt", ".csv", ".tsv", ".log", ".json", ".jsonl", ".sql", ".py",
    ".yaml", ".yml", ".xml", ".ini", ".conf", ".properties",
    ".sh", ".bat", ".ps1", ".java", ".js", ".ts", ".go", ".rs", ".c", ".h",
    ".cpp", ".toml", ".ipynb",
}


def _decode_text(raw: bytes) -> str:
    """按候选编码解码；全失败时用 charset 探测，再不行才报错。

    先判二进制：可执行文件/压缩包也能被 utf-8「成功」解码出大量控制字符，
    那种「解析成功」比报错更糟——模型会把乱码当正文分析。
    """
    if _looks_binary(raw):
        raise RuntimeError("这看起来是二进制文件，不是文本（压缩包/可执行文件/加密文档？）")
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:
        logger.debug("charset 探测失败", exc_info=True)
    raise RuntimeError(f"无法解码文本（已尝试 {'/'.join(_TEXT_ENCODINGS)} 与自动探测）")


# 判断二进制用「控制字符占比」：NUL 出现即判死（文本文件几乎不会有），
# 其余控制字符超过 5% 也算——正常文本里它们只会零星出现在转义序列里。
_BINARY_CONTROL_RATIO = 0.05


def _looks_binary(raw: bytes) -> bool:
    sample = raw[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    control = sum(1 for b in sample
                  if b < 0x20 and b not in (0x09, 0x0A, 0x0D, 0x0C, 0x1B))
    return control / len(sample) > _BINARY_CONTROL_RATIO


def _xlsx_text(raw: bytes) -> str:
    """xlsx → 文本：每个工作表一段，行内单元格用 ` | ` 分隔。"""
    from io import BytesIO

    import openpyxl

    wb = openpyxl.load_workbook(BytesIO(raw), data_only=True, read_only=True)
    lines: list[str] = []
    for ws in wb.worksheets:
        lines.append(f"【工作表 {ws.title}】")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells))
        lines.append("")
    wb.close()
    return "\n".join(lines).strip()


def _xls_text(raw: bytes) -> str:
    """老式 xls（BIFF）→ 文本。xlrd 2.x 只读 xls，正好是这里需要的。

    xlrd 的报错是「Expected BOF record; found b'...'」这类内部术语，用户看不懂
    也改不动。这里包一层，把「该文件其实不是 xls」讲成可行动的话。
    """
    import xlrd

    try:
        wb = xlrd.open_workbook(file_contents=raw)
    except xlrd.XLRDError as e:
        raise RuntimeError(
            f"这个 .xls 文件打不开（{str(e)[:80]}）——多半实际是 xlsx/csv 改了后缀，"
            "或文件已损坏；建议用 Excel 另存为 .xlsx 后重发") from e
    lines: list[str] = []
    for ws in wb.sheets():
        lines.append(f"【工作表 {ws.name}】")
        for r in range(ws.nrows):
            cells = ["" if v in (None, "") else str(v) for v in ws.row_values(r)]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells))
        lines.append("")
    return "\n".join(lines).strip()


def _docx_text(raw: bytes) -> str:
    """docx → 文本：段落按序 + 表格逐行（表格结构是工单/清单类文档的关键信息）。"""
    from io import BytesIO

    import docx

    d = docx.Document(BytesIO(raw))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts).strip()


def _pptx_text(raw: bytes) -> str:
    """pptx → 文本：按页抽标题/正文/表格。

    页序就是讲述顺序，所以保留 `【第 N 页】` 前缀——模型据此能把结论和页码对上，
    用户说「第 5 页那张表」时也定位得到。形状里的文字按文档顺序遍历，
    不改写成「标题/正文」两段，因为很多幻灯片用文本框排版，区分会打乱语序。
    """
    from io import BytesIO

    from pptx import Presentation

    prs = Presentation(BytesIO(raw))
    chunks: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                body = "\n".join(
                    p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if body.strip():
                    texts.append(body.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        texts.append(" | ".join(cells))
        if texts:
            chunks.append(f"【第 {idx} 页】\n" + "\n".join(texts))
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note:
                chunks.append(f"【第 {idx} 页备注】\n{note}")
    return "\n\n".join(chunks).strip()


def _odf_text(raw: bytes, suffix: str) -> str:
    """ODF（odt/ods/odp）→ 文本：用 odfpy 遍历文本节点，表格按行拼接。

    不按 suffix 分开实现：ODF 的段落/表格在三种文档里都是同一套元素，
    统一遍历反而少一份会漂移的代码。
    """
    from io import BytesIO

    from odf import teletype
    from odf.opendocument import load
    from odf.table import Table, TableCell, TableRow

    doc = load(BytesIO(raw))
    lines: list[str] = []
    for p in doc.text.childNodes:
        if getattr(p, "qname", ("", ""))[1] == "p":
            text = teletype.extractText(p).strip()
            if text:
                lines.append(text)
    for table in doc.getElementsByType(Table):
        lines.append(f"【表格 {table.getAttribute('name') or ''}】")
        for row in table.getElementsByType(TableRow):
            cells = [teletype.extractText(c).strip()
                     for c in row.getElementsByType(TableCell)]
            if any(cells):
                lines.append(" | ".join(cells))
    if not lines:  # odp 的正文常在内嵌的 draw 元素里，退回全量文本提取
        return teletype.extractText(doc.text).strip()
    return "\n".join(lines).strip()


def _cap_doc_text(text: str, fname: str, budget: int) -> str:
    """把单份附件文本压进剩余预算，截断时**明说**截了。

    静默截断最坑的一种：模型以为读完了全文，对「后半段说了什么」给出自信但没依据
    的答案。所以截断处留一行告知，并给出后续动作（分段重发 / 只发相关页）。
    """
    if budget <= 0:
        return f"（本条消息的附件文本总量已达上限，本份未注入；如需分析请单独重发 {fname}）"
    limit = min(_MAX_DOC_CHARS, budget)
    if len(text) <= limit:
        return text
    return (text[:limit]
            + f"\n\n...（{fname} 共 {len(text)} 字，已截取前 {limit} 字；"
              "需要后续内容请分段发送或只发相关章节）")


def download_media_resources(channel, msg) -> MediaBundle:
    """已废弃的同步占位——真正入口是 download_media_bundle（async）。"""
    raise NotImplementedError


async def download_media_bundle(channel, msg) -> MediaBundle:
    """下载消息全部媒体资源并分流解析，返回 MediaBundle。

    channel: BotChannel（用其底层 SDK 的 download_resource bytes 接口）。
    失败不抛：单资源失败降级为 note，不阻断消息主链路。
    """
    bundle = MediaBundle()
    resources = getattr(msg, "resources", None) or []
    if not resources:
        return bundle

    message_id = getattr(msg, "id", "") or ""
    # 单条消息的总文本预算：9 个 30KB 附件 = 270KB，直接顶爆上下文。
    # 按附件顺序分配，后面的先被截短——用户通常先发最相关的那份。
    budget = _MAX_TOTAL_DOC_CHARS
    for res in resources:
        rtype = getattr(res, "type", "file") or "file"
        file_key = getattr(res, "file_key", "") or ""
        fname = getattr(res, "file_name", None) or ""
        if not file_key:
            continue
        try:
            # 跨消息下载须用原始消息 id（聚合批次合并的资源带 _source_message_id）
            src_msg_id = getattr(res, "_source_message_id", None) or message_id
            raw, ctype = await channel.download_resource_bytes(
                file_key, resource_type=rtype, message_id=src_msg_id,
            )
            if raw is None:
                bundle.parts.append(MediaPart(
                    "note", f"[媒体 {fname or rtype} 下载失败]", fname))
                continue
            mime = _guess_mime(fname, ctype)

            if rtype == "image" or mime in _IMAGE_MIMES:
                if len(raw) > _MAX_IMAGE_BYTES:
                    bundle.parts.append(MediaPart(
                        "note", f"[图片 {fname} 超过 10MB，未注入]", fname))
                    continue
                raw, mime = _shrink_image(raw, mime)
                b64 = base64.b64encode(raw).decode()
                bundle.parts.append(MediaPart("image", (
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}}
                ), fname))

            elif rtype == "video" or mime.startswith("video/"):
                if len(raw) > _MAX_VIDEO_BYTES:
                    bundle.parts.append(MediaPart(
                        "note", f"[视频 {fname} 超过 30MB，未注入]", fname))
                    continue
                dur = _video_duration(raw)
                if 0 < dur < _MIN_VIDEO_SECONDS:
                    bundle.parts.append(MediaPart(
                        "note", f"[视频 {fname} 短于 2 秒，模型无法识别]", fname))
                    continue
                if dur > _MAX_VIDEO_SECONDS:
                    bundle.parts.append(MediaPart(
                        "note", f"[视频 {fname} 超过 5 分钟，未注入]", fname))
                    continue
                b64 = base64.b64encode(raw).decode()
                bundle.parts.append(MediaPart("video", (
                    {"type": "video_url",
                     "video_url": {"url": f"data:video/mp4;base64,{b64}"}}
                ), fname))

            elif rtype == "audio" or mime.startswith("audio/"):
                # 网关无可用 ASR（/audio/transcriptions 500，各模型不吃 audio）
                bundle.parts.append(MediaPart(
                    "note",
                    f"[语音消息 {fname or ''} 暂不支持转写，请用文字补充说明]",
                    fname))

            elif fname and Path(fname).suffix.lower() in _DOC_EXTS:
                try:
                    text = await _parse_doc_offthread(raw, fname)
                    if text.strip():
                        text = _cap_doc_text(text, fname, budget)
                        bundle.parts.append(MediaPart(
                            "text", f"【附件 {fname}】\n{text}", fname))
                        budget -= len(text)
                    else:
                        # 空串单独一类：扫描版 PDF、纯图 docx 都是这个表现，
                        # 与「解析失败」区分开，用户才知道该重发什么。
                        bundle.parts.append(MediaPart(
                            "note", f"[附件 {fname} 解析为空：可能是扫描件或只有图片，"
                                    "可把关键页截图发我]", fname))
                except Exception as e:
                    logger.warning("Document %s parse failed: %s", fname, e)
                    bundle.parts.append(MediaPart(
                        "note", f"[附件 {fname} 解析失败：{str(e)[:160]}]", fname))

            else:
                bundle.parts.append(MediaPart(
                    "note", f"[文件 {fname or rtype} 类型暂不支持识别"
                            f"{('（后缀 ' + Path(fname).suffix + '）') if fname else ''}]",
                    fname))

        except Exception as e:
            logger.warning("Media resource %s download failed: %s", file_key, e)
            bundle.parts.append(MediaPart(
                "note", f"[媒体 {fname or rtype} 处理失败]", fname))
    return bundle


async def _parse_doc_offthread(raw: bytes, fname: str) -> str:
    """文档解析跑线程池（openpyxl/docx 大文件会阻塞事件循环）。"""
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(
        None, _parse_document, raw, fname)


def build_user_content(text: str, bundle: MediaBundle | None) -> str | list[dict]:
    """构造 OpenAI user 消息 content。

    无媒体 → 纯字符串（原形态，缓存友好）；有媒体 → content 数组
    （文本帧在前带 note/text 注入，图片/视频 parts 随后）。
    """
    if bundle is None or not bundle.has_media:
        return text
    parts: list[dict] = []
    lead = text.strip()
    extras = bundle.notes() + bundle.texts()
    if extras:
        lead = (lead + "\n\n" if lead else "") + "\n\n".join(extras)
    if lead:
        parts.append({"type": "text", "text": lead})
    parts.extend(bundle.content_parts())
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]  # 只有 note/text（无图视频）→ 退回纯文本
    return parts


__all__ = [
    "MediaBundle",
    "MediaPart",
    "build_user_content",
    "download_media_bundle",
]
