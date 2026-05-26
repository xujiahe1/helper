"""Ontology 周期体检 — 每周一次跑。

任务:
  1) merge_near_duplicates: 名字 / slug 相似度 ≥ 阈值的 entity 合并(name normalize + LLM judge)
  2) flag_orphans: mention_count 长期未增长(N 周)+ 未晋升 → 标 archived
  3) decay_promoted: 已晋升但近 N 月零引用 → frontmatter archived: true(不删 git 文件)

CLI: helper ontology maintain
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from helper.config import get_settings
from helper.llm import run
from helper.policy import load_knowledge_policy
from helper.policy.knowledge import should_decay, should_merge
from helper.storage import session
from helper.storage.models import EntityCandidate

log = logging.getLogger(__name__)


@dataclass
class MaintenanceReport:
    merged: list[tuple[str, str]] = field(default_factory=list)  # (loser_slug, winner_slug)
    archived_orphans: list[str] = field(default_factory=list)
    decayed_promoted: list[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"[\w一-鿿]+")


def _toks(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 1}


def _name_similarity(a: str, b: str) -> float:
    """简易 Jaccard。两边名字短就直接相等判断,否则 token 重叠。"""
    if not a or not b:
        return 0.0
    if a.strip() == b.strip():
        return 1.0
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


JUDGE_PROMPT = """两个 entity 是否指同一个概念? a vs b。

a: {a_name} — {a_desc}
b: {b_name} — {b_desc}

只输出 yes 或 no。"""


def _llm_judge_same(a: EntityCandidate, b: EntityCandidate) -> bool:
    try:
        reply = run(
            "synonym_judge",
            user=JUDGE_PROMPT.format(
                a_name=a.name, a_desc=a.description[:200],
                b_name=b.name, b_desc=b.description[:200],
            ),
            temperature=0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("synonym_judge LLM failed %s vs %s: %s", a.slug, b.slug, e)
        return False
    return "yes" in reply.lower().split()


def _merge_into(loser: EntityCandidate, winner: EntityCandidate, sess) -> None:
    loser_refs = json.loads(loser.raw_refs_json or "[]")
    winner_refs = json.loads(winner.raw_refs_json or "[]")
    merged = sorted(set(loser_refs + winner_refs))
    winner.raw_refs_json = json.dumps(merged)
    winner.mention_count = len(merged)
    if loser.first_seen and (winner.first_seen is None or loser.first_seen < winner.first_seen):
        winner.first_seen = loser.first_seen
    if loser.last_seen and (winner.last_seen is None or loser.last_seen > winner.last_seen):
        winner.last_seen = loser.last_seen
    if not winner.description and loser.description:
        winner.description = loser.description
    sess.delete(loser)


def merge_near_duplicates(*, dry_run: bool = False) -> list[tuple[str, str]]:
    s = get_settings()
    policy = load_knowledge_policy(s.helper_spec_git_dir)
    threshold = policy.merge.semantic_similarity_threshold
    merged_pairs: list[tuple[str, str]] = []
    with session() as sess:
        all_ec = sess.execute(
            select(EntityCandidate).order_by(EntityCandidate.mention_count.desc())
        ).scalars().all()

        seen_losers: set[int] = set()
        for i, a in enumerate(all_ec):
            if a.id in seen_losers:
                continue
            for b in all_ec[i + 1 :]:
                if b.id in seen_losers:
                    continue
                if a.entity_type != b.entity_type:
                    continue
                sim = _name_similarity(a.name, b.name)
                if not should_merge(policy, sim) and sim < threshold:
                    continue
                if not _llm_judge_same(a, b):
                    continue
                # winner = mention_count 高那个
                if a.mention_count >= b.mention_count:
                    winner, loser = a, b
                else:
                    winner, loser = b, a
                merged_pairs.append((loser.slug, winner.slug))
                seen_losers.add(loser.id)
                if not dry_run:
                    _merge_into(loser, winner, sess)
        if not dry_run:
            sess.commit()
    return merged_pairs


def flag_orphans(*, weeks_idle: int = 8) -> list[str]:
    """N 周未被引用 + 未晋升 → 标 archived(用 entity_type 加 :archived 后缀,简易)。"""
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks_idle)
    flagged: list[str] = []
    with session() as sess:
        cands = sess.execute(
            select(EntityCandidate)
            .where(EntityCandidate.promoted_at.is_(None))
            .where(EntityCandidate.last_seen < cutoff)
        ).scalars().all()
        for c in cands:
            if c.entity_type.endswith(":archived"):
                continue
            c.entity_type = f"{c.entity_type}:archived"
            flagged.append(c.slug)
        sess.commit()
    return flagged


def decay_promoted() -> list[str]:
    """已晋升 entity 按 knowledge_policy.decay 衰减(写 entity_type 后缀)。"""
    s = get_settings()
    policy = load_knowledge_policy(s.helper_spec_git_dir)
    now = datetime.now(timezone.utc)
    decayed: list[str] = []
    with session() as sess:
        cands = sess.execute(
            select(EntityCandidate).where(EntityCandidate.promoted_at.is_not(None))
        ).scalars().all()
        for c in cands:
            base_type = c.entity_type.split(":")[0]
            months = max(0, (now - (c.last_seen or c.first_seen or now)).days // 30)
            should, action = should_decay(policy, base_type, months)
            if not should:
                continue
            if action == "deprioritize" and not c.entity_type.endswith(":archived"):
                c.entity_type = f"{base_type}:archived"
                decayed.append(c.slug)
        sess.commit()
    return decayed


def run_maintenance(*, dry_run: bool = False) -> MaintenanceReport:
    rep = MaintenanceReport()
    rep.merged = merge_near_duplicates(dry_run=dry_run)
    if not dry_run:
        rep.archived_orphans = flag_orphans()
        rep.decayed_promoted = decay_promoted()
    return rep
