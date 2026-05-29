"""memory_extract — LLM 按语义判断 raw 里有没有"约束 bot 行为"的指令。

与 L1 抽取解耦,在 webhook 里和 schedule_l1 平行 fire-and-forget。

抽取边界:**"是描述客观世界,还是约束 bot 行为/口径?"**
- 前者(刘佳翔是 IAM 负责人)→ 进 L1,不进 memory
- 后者(答哥别每次复述身份)→ 进 memory

冲突走现有 ConflictLog target_type='memory',inbox 周报三段式裁决,
不另起机制(commit 0733fbe 已确立的统一修正路径)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlalchemy import select

from helper.llm import run
from helper.storage import session
from helper.storage.models import ConflictLog, EntityCandidate, Memory, RawInput

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """你的任务是从用户的话里识别"对 bot 行为的指令"。

什么算指令(抽出来):
- 谈话对象是 bot 本身(不是描述世界)
- 表达"以后/这种情况/答 X 时... 怎么做 / 别这么做"的行为约束
- 例子:
  - "答哥相关的问题别每次复述身份" → scope=哥(entity), directive="身份已知不复述"
  - "我喜欢简洁回答" → scope=global, directive="回答尽量简洁"
  - "提到 wavelite 别再解释是什么" → scope=wavelite(entity), directive="不解释含义"

什么不算指令(不抽):
- 描述客观事实("刘佳翔是信息化负责人")
- 日常用语("以后再说"、"先这样")
- 提问("怎么处理 X?")
- 已经发生的描述("上次他这么干了")

输出 JSON:
{
  "directives": [
    {"scope_type": "entity|global", "scope_ref": "<entity 名或空>", "directive": "<指令文本>"}
  ]
}

如果没有任何指令,输出 {"directives": []}。
只输出 JSON,不要 markdown 包裹。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        result = json.loads(text[start : end + 1], strict=False)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def _resolve_scope(scope_type: str, scope_ref: str) -> tuple[str, str]:
    """把 LLM 给的 scope_ref(可能是中文名)对到 EntityCandidate slug。

    匹配不上 → 保留原文当 ref(后续 ask 拼接按字面匹配 entity name 也能命中)。
    """
    if scope_type != "entity" or not scope_ref:
        return ("global", "")
    with session() as s:
        ec = s.execute(
            select(EntityCandidate).where(
                EntityCandidate.name == scope_ref,
                EntityCandidate.superseded_at.is_(None),
            )
        ).scalar_one_or_none()
        if ec is not None:
            return ("entity", ec.slug)
    return ("entity", scope_ref)  # 没找到就先记字面值


def _detect_conflict_target(scope_type: str, scope_ref: str) -> int | None:
    """同 scope 已有 alive directive → 冲突。返回旧 memory id;无冲突返 None。"""
    with session() as s:
        existing = s.execute(
            select(Memory)
            .where(Memory.scope_type == scope_type)
            .where(Memory.scope_ref == scope_ref)
            .where(Memory.superseded_at.is_(None))
            .order_by(Memory.id.desc())
        ).scalar_one_or_none()
        return existing.id if existing else None


def extract_for_raw(raw_id: int) -> int:
    """对一条 raw 跑 memory_extract,落库 + 冲突挂 conflict_log。

    返回新落库的 directive 条数。同步 API,调用方决定是否扔后台。
    """
    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None or not (raw.content_text or "").strip():
            return 0
        text = raw.content_text
        author = raw.author_domain or ""

    try:
        reply = run(
            "memory_extract",
            system=SYSTEM_PROMPT,
            user=f"# 用户的话\n{text}",
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("memory_extract LLM failed raw_id=%s: %s", raw_id, e)
        return 0

    data = _parse_json(reply) or {}
    items = data.get("directives") or []
    if not isinstance(items, list):
        return 0

    n_new = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        directive = str(it.get("directive", "")).strip()
        if not directive:
            continue
        scope_type, scope_ref = _resolve_scope(
            str(it.get("scope_type", "global")),
            str(it.get("scope_ref", "")).strip(),
        )

        old_id = _detect_conflict_target(scope_type, scope_ref)

        with session() as s:
            mem = Memory(
                scope_type=scope_type,
                scope_ref=scope_ref,
                directive=directive,
                source_raw_id=raw_id,
                author_domain=author,
            )
            s.add(mem)
            s.flush()
            new_id = mem.id

            if old_id is not None:
                # 冲突走 inbox 周报裁决,不直接覆盖
                s.add(ConflictLog(
                    raw_id=raw_id,
                    target_type="memory",
                    target_slug=str(old_id),  # memory 没有 slug,用 id
                    summary=(
                        f"新指令(memory#{new_id}: {directive!r})与已有 "
                        f"memory#{old_id} 在同 scope({scope_type}:{scope_ref}) 冲突"
                    ),
                    severity="medium",
                ))
            n_new += 1

    if n_new:
        log.info("memory_extract raw_id=%s extracted=%d", raw_id, n_new)
    return n_new


async def _run_in_thread(raw_id: int) -> None:
    """与 L1 同构:用 llm_slot 限并发,扔到线程池跑同步代码。"""
    from helper.im.queue import llm_slot

    try:
        async with llm_slot():
            await asyncio.to_thread(extract_for_raw, raw_id)
    except Exception:  # noqa: BLE001
        log.exception("background memory_extract failed raw_id=%s", raw_id)


def schedule_memory_extract(raw_id: int) -> None:
    """webhook 调用入口 — fire-and-forget。无 running loop(CLI/测试)→ 同步跑。"""
    from helper.im.queue import spawn

    task = spawn(_run_in_thread(raw_id))
    if task is None:
        extract_for_raw(raw_id)
