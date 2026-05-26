"""Model router — 业务代码声明 task_type,不写死模型名。

模型表外置在 spec git repo 的 meta/policies/llm_routing.yaml,
改路由就改这个 yaml,走 PR + git diff,可审计、可回溯、可 A/B。
设计参见 docs/architecture.md §8.6 / docs/runtime.md §1。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from helper.config import get_settings
from helper.llm.client import anthropic_client, openai_client
from helper.policy import LlmRouting, TaskRouting, load_llm_routing


@lru_cache
def current_routing() -> LlmRouting:
    """从 spec repo 读当前 LLM routing 策略。进程内 cache,要重载需重启。"""
    s = get_settings()
    return load_llm_routing(s.helper_spec_git_dir)


def reset_routing_cache() -> None:
    """测试 / 策略热改后调,清掉 cache。"""
    current_routing.cache_clear()


def _task(routing: LlmRouting, task: str) -> TaskRouting:
    if task not in routing.tasks:
        raise KeyError(
            f"Unknown task type: {task!r}. "
            f"已知: {sorted(routing.tasks)}. "
            f"如需新增,改 meta/policies/llm_routing.yaml。"
        )
    return routing.tasks[task]


def model_for(task: str) -> tuple[str, str]:
    """task → (model_id, provider)。"""
    t = _task(current_routing(), task)
    return t.model, t.provider


def run(
    task: str,
    *,
    system: str = "",
    user: str = "",
    max_tokens: int | None = None,
    temperature: float | None = None,
    **kwargs: Any,
) -> str:
    """单轮文本调用入口:按 task → (model, provider) 分发,返回 assistant text。

    embedding / rerank 走专用入口(后续 M2 加),不走这里。
    """
    routing = current_routing()
    t = _task(routing, task)
    effective_max_tokens = max_tokens or t.max_tokens or routing.defaults.max_tokens

    if t.provider == "anthropic":
        client = anthropic_client()
        params: dict[str, Any] = {
            "model": t.model,
            "max_tokens": effective_max_tokens,
            "messages": [{"role": "user", "content": user}],
            **kwargs,
        }
        if system:
            params["system"] = system
        # claude-opus-4-7 不支持 temperature 参数(Athenai 网关返 400)
        if temperature is not None and not t.model.startswith("claude-opus-4-7"):
            params["temperature"] = temperature
        msg = client.messages.create(**params)
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    if t.provider == "openai":
        client = openai_client()
        msgs: list[dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        params2: dict[str, Any] = {
            "model": t.model,
            "messages": msgs,
            "max_tokens": effective_max_tokens,
            **kwargs,
        }
        if temperature is not None:
            params2["temperature"] = temperature
        resp = client.chat.completions.create(**params2)
        return resp.choices[0].message.content or ""

    raise ValueError(f"Unknown provider: {t.provider!r}")
