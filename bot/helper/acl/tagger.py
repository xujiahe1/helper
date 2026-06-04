"""acl_tag — LLM 判内容应贴哪个 topic。

入口:
- tag_text(text): 给一段文本判 topic_id, 失败按 yaml.default_on_uncertain。
- tag_raw(raw_id): 跑 tag_text + 落 raw.acl_topic_id + 同步派生 atom 的 acl_topic_id。
- backfill_all(): 一次性扫所有 raw.acl_topic_id="" 的行。

LLM 1 次失败重试 1 次, 仍失败 → default_on_uncertain (yaml 默认 ""), 写 warning log。
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update

from helper.acl.policy import current_acl
from helper.llm import run
from helper.storage import session
from helper.storage.models import L1Item, RawInput

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_TMPL = """你是内容安全打标器。判定下面这段文本是否涉及任意一个受控 topic。

# 受控 topic 列表
{topics_block}

# 输出规则
- 命中其中某个 topic → 只输出该 topic 的 id (例如: ge), 不要任何前后缀、解释、标点
- 都不命中 → 输出空串(直接什么都不写)
- 不确定 → 输出 UNCERTAIN

注意:
- 工作话题(系统、流程、配置、对外可分享的决策)即便提到 topic 描述里的人名也算公开,不要打标
- 内部花名 / 绰号 / 隐喻称谓 / 私人关系话题, 哪怕没明说 topic 主角名, 一旦语义上属于该 topic 范围也要打标
- 只能用 topic 列表里出现过的 id, 不要自己造新 id"""


def _system_prompt() -> str:
    acl = current_acl()
    blocks = []
    for t in acl.topics:
        blocks.append(f"- id: {t.id}\n  描述:\n    {t.description.strip().replace(chr(10), chr(10) + '    ')}")
    return _SYSTEM_PROMPT_TMPL.format(topics_block="\n".join(blocks))


def _parse_tag(reply: str) -> str:
    """LLM 输出 → topic_id 或空串。'UNCERTAIN' 视为不确定 → 让上层用 default。"""
    body = (reply or "").strip()
    # LLM 可能多嘴, 取第一个非空 token
    if not body:
        return ""
    first = body.splitlines()[0].strip().strip('`"\'').rstrip(",;.").strip()
    if not first or first.lower() == "uncertain":
        return ""
    return first


def tag_text(text: str) -> str:
    """LLM 打标 + 1 次重试。返 topic_id 或空串。

    返空串两种含义:
    - LLM 明确说"不属任何 topic" → 返空(公开内容)
    - LLM 报错或返 UNCERTAIN → 用 yaml 的 default_on_uncertain 兜底
    """
    text = (text or "").strip()
    if not text:
        return ""
    acl = current_acl()
    if not acl.topics:
        return ""
    valid_ids = {t.id for t in acl.topics}

    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            reply = run("acl_tag", system=_system_prompt(), user=text, temperature=0.0)
            tag = _parse_tag(reply)
            if tag == "":
                return ""  # LLM 明确不命中
            if tag in valid_ids:
                return tag
            # LLM 返了不在白名单里的字符串 → 当不确定走 default
            log.warning("acl_tag returned unknown id=%r, fall back to default", tag)
            return acl.default_on_uncertain
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("acl_tag attempt %d failed: %s", attempt, e)
    log.warning("acl_tag both attempts failed (%s); using default_on_uncertain=%r",
                last_err, acl.default_on_uncertain)
    return acl.default_on_uncertain


def tag_raw(raw_id: int) -> str:
    """给 raw + 它派生的 atom 打标。返打上的 topic_id (可能是空串)。

    护栏: 原标非空 + 新判定为空 → 保留旧标不降级。l1-backfill --force-all 会
    走 _run_consumers 末尾重跑 tag_raw, LLM 判定漂移时能保住已有 ge 标不被
    洗回公开。新判定也非空但与旧不同 → 以新为准(确实换 topic 的场景)。
    """
    with session() as s:
        raw = s.get(RawInput, raw_id)
        if raw is None:
            return ""
        text = (raw.content_text or "").strip()
        old_topic_id = raw.acl_topic_id or ""
    if not text:
        return ""

    new_topic_id = tag_text(text)
    if old_topic_id and not new_topic_id:
        log.info(
            "tag_raw: keep existing topic=%s for raw_id=%s (LLM returned empty, no downgrade)",
            old_topic_id, raw_id,
        )
        return old_topic_id
    topic_id = new_topic_id

    with session() as s:
        s.execute(
            update(RawInput).where(RawInput.id == raw_id).values(acl_topic_id=topic_id)
        )
        # 派生 L1Item 同步 — 1 raw : N items 全打同样的标
        s.execute(
            update(L1Item).where(L1Item.raw_id == raw_id).values(acl_topic_id=topic_id)
        )
        s.commit()

    return topic_id


def backfill_all(*, batch_size: int = 50, max_id: int | None = None) -> int:
    """扫所有 raw 行(单次 ingest 路径已自动打标, 这个命令负责存量补)。

    用游标(min_id)往后翻, 不靠"acl_topic_id == ''" 过滤 — 公开内容 LLM 判定后
    标仍是空, 用游标避免反复扫同一批。返跑过 LLM 的行数。

    重跑相同区间是幂等的: tag_raw 会覆盖现有标。
    """
    total = 0
    last_id = 0
    while True:
        with session() as s:
            q = select(RawInput.id).where(RawInput.id > last_id).order_by(RawInput.id).limit(batch_size)
            if max_id is not None:
                q = q.where(RawInput.id <= max_id)
            ids = list(s.execute(q).scalars().all())
        if not ids:
            break
        for rid in ids:
            try:
                tag_raw(rid)
                total += 1
            except Exception:  # noqa: BLE001
                log.exception("backfill tag_raw failed raw_id=%s", rid)
        last_id = ids[-1]
    return total
