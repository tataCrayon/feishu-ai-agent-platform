"""MediaBundle → ACP prompt 块 — dsh 链路的媒体注入唯一转换点。

为什么需要这一层：飞书侧的多媒体（`app/multimodal.py`）产出的是 OpenAI 形态的
content part（`image_url` 的 data URL、`video_url`），而 ACP 的 `session/prompt`
只认两种内容块：

    {"type": "text",  "text": "..."}
    {"type": "image", "data": "<canonical base64>", "mimeType": "image/png|jpeg|webp|gif"}

两者形状不同，且 ACP **没有** video / audio 块。把转换放在 `app/dsh/` 内是为了
守住 conventions §L2：只有本层允许出现 ACP 协议细节。

无法映射的内容不静默丢弃：视频、超限图片、非四类 MIME 一律转成一行说明文本，
让模型知道自己少看到了什么，而不是假装没收到附件。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from app.multimodal import MediaBundle

logger = logging.getLogger("dsh.media")

# ACP 只接受这四种内联图片 MIME（dsh-acp 侧严格校验，其余直接报 invalid）。
ACP_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

# 同一轮最多注入的图片数：再多会撑爆上下文与图片 token 成本，
# 超出部分降级成说明文本（用户可分批重发）。
MAX_IMAGES_PER_PROMPT = 8

_NORMALIZE_MIME = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}


@dataclass(frozen=True, slots=True)
class Prompt:
    """一轮 prompt 的两种可用形态。

    `blocks` 是完整形态（含图片）；`text_only()` 是模型不支持图片输入时的降级形态，
    仍然告知模型「本轮有 N 张图未注入」，而不是让它对着一张不存在的图作答。
    """

    blocks: tuple[dict[str, Any], ...]
    image_count: int = 0
    _text: str = ""

    def text_only(self) -> tuple[dict[str, Any], ...]:
        """剥掉图片块，保留文本并把「有几张图被略过」写进正文。"""
        if not self.image_count:
            return self.blocks
        note = f"（本轮附带 {self.image_count} 张图片，当前模型未声明图片输入能力，图片内容未注入）"
        body = f"{self._text}\n\n{note}" if self._text else note
        return ({"type": "text", "text": body},)


def _split_data_url(url: str) -> tuple[str, str]:
    """拆 `data:<mime>;base64,<payload>`；非 data URL 返回空串对。"""
    if not url.startswith("data:") or ";base64," not in url:
        return "", ""
    header, payload = url.split(";base64,", 1)
    return header[len("data:"):].strip().lower(), payload.strip()


def _to_acp_image(mime: str, payload: str, name: str) -> tuple[str, str] | None:
    """把图片规范化成 ACP 认的 (mime, canonical base64)；做不到时返回 None。"""
    mime = _NORMALIZE_MIME.get(mime, mime)
    if mime in ACP_IMAGE_MIMES:
        return mime, payload
    if mime != "image/bmp":
        return None
    # bmp 不在 ACP 白名单里，但模型能看：转成 png 再送。
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        img: Any = PILImage.open(BytesIO(base64.b64decode(payload))).convert("RGB")
        out = BytesIO()
        img.save(out, "PNG")
        return "image/png", base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        logger.warning("图片 %s 转码失败（bmp→png）: %s", name, e)
        return None


def build_prompt(text: str, bundle: MediaBundle | None) -> Prompt:
    """构造一轮 ACP prompt 块。

    文本帧在前（正文 + 文档解析文本 + 降级说明），图片块随后——顺序与
    `multimodal.build_user_content` 一致，模型看到的是同一套上下文结构。
    """
    lines: list[str] = []
    images: list[dict[str, Any]] = []
    skipped: list[str] = []

    if bundle is not None:
        lines.extend(bundle.notes())
        lines.extend(bundle.texts())
        for part in bundle.parts:
            if part.kind == "video":
                skipped.append(
                    f"[视频 {part.name or ''} 未注入：dsh 链路不支持视频输入，"
                    "如需分析请截关键帧为图片重发]"
                )
                continue
            if part.kind != "image":
                continue
            url = ""
            if isinstance(part.data, dict):
                url = str((part.data.get("image_url") or {}).get("url") or "")
            mime, payload = _split_data_url(url)
            converted = _to_acp_image(mime, payload, part.name) if payload else None
            if converted is None:
                skipped.append(
                    f"[图片 {part.name or ''} 未注入：格式 {mime or '未知'} 不受支持"
                    "（仅 png/jpeg/webp/gif）]"
                )
                continue
            if len(images) >= MAX_IMAGES_PER_PROMPT:
                skipped.append(f"[图片 {part.name or ''} 未注入：单轮最多 {MAX_IMAGES_PER_PROMPT} 张]")
                continue
            images.append({"type": "image", "data": converted[1], "mimeType": converted[0]})

    body = text.strip()
    extras = lines + skipped
    if extras:
        joined = "\n\n".join(extras)
        body = f"{body}\n\n{joined}" if body else joined

    blocks: list[dict[str, Any]] = []
    if body:
        blocks.append({"type": "text", "text": body})
    elif images:
        # 纯附件消息（用户只甩一张图）：正文补一句中性说明而不是留空文本块，
        # 免得模型把「只有图片」当成上一轮的延续——同会话常驻时尤其明显。
        body = f"（用户只发来了 {len(images)} 张附件图片，没有附带文字）"
        blocks.append({"type": "text", "text": body})
    blocks.extend(images)
    return Prompt(blocks=tuple(blocks), image_count=len(images), _text=body)


__all__ = ["ACP_IMAGE_MIMES", "MAX_IMAGES_PER_PROMPT", "Prompt", "build_prompt"]
