"""Prompt Manager — 集中管理系统提示词。

提供：
- PromptLoader: 从 bot/prompts/ 目录加载带版本管理的提示词
- load_prompt: 便捷加载函数
"""

from app.prompts.loader import PromptLoader, PromptTemplateError, load_prompt

__all__ = [
    "PromptLoader",
    "PromptTemplateError",
    "load_prompt",
]