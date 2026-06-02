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
from helper.storage import fts, raw_store, session
from helper.storage.models import ConflictLog, EntityCandidate, Memory, RawInput

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """你的任务是从用户的话里识别"对 bot 行为的指令"。

# 抽取判定:必须三闸全过才能抽

只有同时满足下面三个条件的内容,才能抽成 directive。任一不满足 → 不抽。

闸 1:**指令的执行动作由 bot 完成**
  描述第三方人/实体的行为模式、性格、属性、客观事实的内容,即便用对 bot
  说话的语气("你记住"、"告诉你"、"你知道"),仍是关于第三方/世界的客观陈述,
  不抽。
  例外:**用第三方做触发条件,但动作仍由 bot 执行**的情况算 bot 指令,要抽
  (如"对方是客户时要更礼貌" / "他不喜欢长篇,回答他时简短点" — 动作主语都是 bot)。

闸 2:**约束的是 bot 怎么说话/怎么呈现答案,不是内容/知识层判断**
  闸 2 的正面没有封闭枚举:任何形容 bot 表达方式的偏好都算(回复风格、路由
  去向、避谈、复述与否、称呼、语言、长度、强调、次序、修辞、是否附引用 等等)。
  闸 2 的反面是确定的:
    - "哪份资料权威 / 哪个事实最新 / X 取代 Y" 这类**知识层判断**应进 L1
      (entity / relation / supersession),不进 memory
    - 即便包着"以 X 为准"、"回答时用 X"这种祈使外壳,只要把它换成
      "X 是权威 / X 是最新"语义不变,就是知识层,不抽

闸 3:**directive 文本里指代必须能 resolve 成唯一确定的对象**
  历史对话用来把代词映射到具体名字或具体内容片段(如"他"→"刘佳翔"、
  "那篇"→"X 文档")。必须能从历史对话里 resolve 出**唯一确定**的对象才能抽。
  - 多义指向的代词("这种情况 / 这么说 / 上面那段"在不同上下文可指多件事)
    无法 resolve 出唯一对象 → 不抽
  - 唯一指向的代词("那篇"在上文恰好只出现一篇文档)→ 解析后写进 directive,
    可以抽
  - 当前消息里完全没有代词的 → 此闸默认通过

# 关于历史对话

历史对话**只用来理解当前消息的指代和承接**,不要把历史对话里的话也抽成指令——
只从"当前消息"那一段抽。scope_ref 写解析后的具体名字(如"哥"、"小猫老师"),
不是代词。

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
        chat_id = raw.chat_id or ""

        # 长路径默认注入历史对话(对齐 ask/intent),解决"他/她/这事"等代词的指代缺失。
        # 群聊用 chat_id 拉,私聊(chat_id="")用 author 兜底拉同一用户的双向消息。
        chat_context = raw_store.format_context_block(
            s,
            chat_id=chat_id,
            fallback_author=author,
            exclude_raw_id=raw_id,
        )

    user_msg = (
        f"{chat_context}\n\n## 当前消息(只从这条抽指令)\n{text}"
        if chat_context
        else f"## 当前消息\n{text}"
    )

    try:
        reply = run(
            "memory_extract",
            system=SYSTEM_PROMPT,
            user=user_msg,
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

            # directive 文本进 fts 池, 让题面能直接命中"应该路由 / 应该这么答"
            # 这类场景描述, 不再依赖 entity 字面词召回。
            fts.upsert(s, kind="directive", ref=str(new_id), content=directive)

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
