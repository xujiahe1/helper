"""LLM 客户端 — 全部走 Athenai 网关。

- Claude 系列走 anthropic SDK (Anthropic-native /v1/messages)
- Qwen / GPT-mini / embedding / rerank 走 openai SDK (OpenAI-compat /v1/chat/completions 等)
"""

from __future__ import annotations

from functools import lru_cache

from anthropic import Anthropic
from openai import OpenAI

from helper.config import get_settings


@lru_cache
def anthropic_client() -> Anthropic:
    s = get_settings()
    return Anthropic(api_key=s.athenai_api_key, base_url=s.athenai_base_url)


@lru_cache
def openai_client() -> OpenAI:
    s = get_settings()
    return OpenAI(api_key=s.athenai_api_key, base_url=f"{s.athenai_base_url}/v1")
