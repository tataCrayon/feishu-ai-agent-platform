"""dsh 链路媒体转换的契约测试 — MediaBundle → ACP prompt 块。

守的是三条边界：图片必须变成 ACP 认的形状（canonical base64 + 四种 MIME），
不支持的内容（视频/超限/超数）必须以文字说明出现而不是静默消失，
模型不支持图片时要退化成纯文本且如实告知「有几张图没注入」。
"""

from __future__ import annotations

import base64

from app.dsh.media import MAX_IMAGES_PER_PROMPT, build_prompt
from app.multimodal import MediaBundle, MediaPart

# 1×1 PNG，标准库就能造：只要字节正确，ACP 侧不关心内容
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _image_part(name: str = "a.png", mime: str = "image/png", payload: bytes | None = None) -> MediaPart:
    b64 = base64.b64encode(payload if payload is not None else _PNG_1X1).decode()
    return MediaPart(
        "image", {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}, name
    )


def _text_of(blocks: tuple[dict, ...] | list[dict]) -> str:
    return "\n".join(b["text"] for b in blocks if b["type"] == "text")


def test_build_prompt_image_becomes_acp_block() -> None:
    prompt = build_prompt("这是什么", MediaBundle([_image_part()]))
    images = [b for b in prompt.blocks if b["type"] == "image"]
    assert len(images) == 1
    assert images[0]["mimeType"] == "image/png"
    # canonical base64：能解回原字节，且没有 data URL 前缀
    assert base64.b64decode(images[0]["data"]) == _PNG_1X1
    assert images[0]["data"].startswith("iVBOR")
    assert prompt.image_count == 1


def test_build_prompt_text_block_precedes_images() -> None:
    prompt = build_prompt("看图", MediaBundle([_image_part()]))
    assert [b["type"] for b in prompt.blocks] == ["text", "image"]
    assert _text_of(prompt.blocks) == "看图"


def test_build_prompt_document_text_merged_into_lead_text() -> None:
    bundle = MediaBundle([
        MediaPart("text", "【附件 x.md】\n内容", "x.md"),
        MediaPart("note", "[语音消息 暂不支持]", "v.amr"),
    ])
    prompt = build_prompt("总结附件", bundle)
    text = _text_of(prompt.blocks)
    assert "内容" in text and "语音消息" in text
    assert text.startswith("总结附件")


def test_build_prompt_video_degrades_to_note() -> None:
    bundle = MediaBundle([MediaPart(
        "video", {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAA"}}, "a.mp4"
    )])
    prompt = build_prompt("看视频", bundle)
    assert not [b for b in prompt.blocks if b["type"] == "image"]
    assert "不支持视频输入" in _text_of(prompt.blocks)


def test_build_prompt_unknown_image_mime_degrades_to_note() -> None:
    bundle = MediaBundle([_image_part("a.tiff", "image/tiff")])
    prompt = build_prompt("看图", bundle)
    assert not [b for b in prompt.blocks if b["type"] == "image"]
    assert "不受支持" in _text_of(prompt.blocks)


def test_build_prompt_image_over_cap_degrades_to_note() -> None:
    bundle = MediaBundle([_image_part(f"{i}.png") for i in range(MAX_IMAGES_PER_PROMPT + 2)])
    prompt = build_prompt("看图", bundle)
    assert prompt.image_count == MAX_IMAGES_PER_PROMPT
    assert "最多" in _text_of(prompt.blocks)


def test_build_prompt_attachment_only_message_still_has_block() -> None:
    prompt = build_prompt("", MediaBundle([_image_part()]))
    # 纯附件消息没有正文：仍要提交图片块，但首块必须是 text——
    # ACP 的 prompt 以文本开场，图文顺序倒置在部分 route 上会被拒。
    assert [b["type"] for b in prompt.blocks] == ["text", "image"]


def test_text_only_reports_skipped_image_count() -> None:
    prompt = build_prompt("看图", MediaBundle([_image_part()]))
    fallback = prompt.text_only()
    assert [b["type"] for b in fallback] == ["text"]
    assert "1 张图片" in fallback[0]["text"]
    assert "看图" in fallback[0]["text"]


def test_text_only_without_images_is_unchanged() -> None:
    prompt = build_prompt("纯文本", None)
    assert prompt.text_only() == prompt.blocks
