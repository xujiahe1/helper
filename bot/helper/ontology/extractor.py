"""从 L1 signals 抽 entity 候选。

走 bulk_extract(qwen3.6-flash)— 单 signal 文本短,Qwen 准确够 + 速度 + 成本。
重复抽到的 slug → 增量更新 mention_count + raw_refs。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from helper.llm import run
from helper.storage import session
from helper.storage.models import EntityCandidate, L1Result, RawInput

log = logging.getLogger(__name__)


@dataclass
class EntityHit:
    slug: str
    name: str
    entity_type: str  # decision_concept / system_name / ticket_type / employee / project / fact
    description: str = ""


SYSTEM_PROMPT = """你是知识抽取助手。从给定的"决策场景 + 信号 + 选择"中抽出 entity 候选。

只抽 6 类:
- decision_concept: 决策性概念,如"风险章节位置"、"审批前置"。最重要,门槛低。
- fact: 静态事实,如"产品经理写到末页时已累"。
- system_name: 系统/工具名,如 "Linear"、"Wave"
- ticket_type: 工单/任务类型
- employee: 人(域账号或姓名)
- project: 项目代号

输出 JSON 数组,每项 {slug, name, entity_type, description}。
- slug: 小写下划线,英文优先,中文用拼音(如 "feng_xian_zhang_jie")。最多 64 字符。
- name: 人类可读名(中英文都可)。
- description: 一句话定义,可空。

不要编造 — 文本里没出现的不要抽。最多 8 个。
只输出 JSON 数组,不要 markdown 代码块。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json_array(text: str) -> list[dict] | None:
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, list) else None


def extract_from_text(text: str) -> list[EntityHit]:
    """对一段文本跑 entity 抽取。失败返空列表。"""
    if not text.strip():
        return []
    try:
        reply = run("bulk_extract", system=SYSTEM_PROMPT, user=text, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("entity extract failed: %s", e)
        return []
    arr = _parse_json_array(reply)
    if arr is None:
        log.warning("entity extract bad JSON: %s", reply[:200])
        return []
    out: list[EntityHit] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug", "")).strip().lower()[:64]
        name = str(item.get("name", "")).strip()[:255]
        etype = str(item.get("entity_type", "decision_concept")).strip() or "decision_concept"
        desc = str(item.get("description", "")).strip()
        if not slug or not name:
            continue
        out.append(EntityHit(slug=slug, name=name, entity_type=etype, description=desc))
    return out


def extract_from_l1(raw_id: int) -> list[EntityCandidate]:
    """跑一条 raw 的 entity 抽取并 upsert 到 entity_candidates。"""
    with session() as s:
        raw = s.get(RawInput, raw_id)
        l1 = s.get(L1Result, raw_id)
        if raw is None or l1 is None or l1.error:
            return []
        # 把 L1 五字段拼成抽取语料
        signals = json.loads(l1.signals_json or "[]")
        tradeoffs = json.loads(l1.tradeoffs_json or "[]")
        text = "\n".join(
            [
                f"场景: {l1.scene}",
                f"信号: {'; '.join(signals)}",
                f"权衡: {'; '.join(tradeoffs)}",
                f"选择: {l1.choice}",
                f"原因: {l1.rationale}",
            ]
        )

    hits = extract_from_text(text)
    if not hits:
        return []

    now = datetime.now(timezone.utc)
    out: list[EntityCandidate] = []
    with session() as s:
        for hit in hits:
            existing = s.execute(
                select(EntityCandidate).where(EntityCandidate.slug == hit.slug)
            ).scalar_one_or_none()
            if existing is None:
                refs = [raw_id]
                row = EntityCandidate(
                    slug=hit.slug,
                    name=hit.name,
                    entity_type=hit.entity_type,
                    description=hit.description,
                    raw_refs_json=json.dumps(refs),
                    mention_count=1,
                    first_seen=now,
                    last_seen=now,
                )
                s.add(row)
                out.append(row)
            else:
                refs = json.loads(existing.raw_refs_json or "[]")
                if raw_id not in refs:
                    refs.append(raw_id)
                    existing.mention_count += 1
                existing.raw_refs_json = json.dumps(refs)
                existing.last_seen = now
                if not existing.description and hit.description:
                    existing.description = hit.description
                out.append(existing)
        s.commit()
        # refresh detached state
        return [s.get(EntityCandidate, e.id) for e in out if e.id is not None]
