"""Athenai embeddings 入口 — bge-m3 (1024 dim, OpenAI-compatible /v1/embeddings)。

为啥独立入口而不走 router.run():embedding 输入是 list[str]、输出是 list[list[float]],
跟 chat completions 完全异构,塞 run() 只会让分支变胖。task=embed_index 仍走
meta/policies/llm_routing.yaml,改模型走 PR。

调用方:helper.storage.vector(写入)、helper.ask.retrieve(查询)。
"""

from __future__ import annotations

import logging
from typing import Iterable

from helper.llm.client import openai_client
from helper.llm.router import current_routing

log = logging.getLogger(__name__)

_EMBED_TASK = "embed_index"


def embed_model() -> str:
    """当前生效的 embedding 模型 id。供 VectorIndex 落 model 字段 / dedup 判断。"""
    routing = current_routing()
    return routing.tasks[_EMBED_TASK].model


def embed(texts: list[str] | tuple[str, ...] | Iterable[str]) -> list[list[float]]:
    """批量 embed。空 list 直接返 [],省一次网络。

    错误抛上去给调用方;sink/specgen/retrieve 各自决定降级策略
    (写入侧:跳过该对象的 index,等 reindex 重试;查询侧:退化到 Jaccard 单路)。
    """
    items = [t for t in texts if isinstance(t, str)]
    if not items:
        return []
    routing = current_routing()
    task = routing.tasks[_EMBED_TASK]
    client = openai_client()
    resp = client.embeddings.create(model=task.model, input=items)
    return [d.embedding for d in resp.data]


def embed_one(text: str) -> list[float]:
    """单条文本 embed。常用于查询路径。"""
    out = embed([text])
    if not out:
        raise RuntimeError("embed_one: empty result for non-empty input")
    return out[0]
