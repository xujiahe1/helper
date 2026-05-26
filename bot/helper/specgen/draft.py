"""一簇 L1 → candidate spec(LLM draft)。

走 ask 主路径(claude-opus-4-7)— L2 是产品护城河,不省。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from sqlalchemy import select

from helper.config import get_settings
from helper.llm import run
from helper.storage import session
from helper.storage.models import L1Result, RawInput, SpecCandidate

log = logging.getLogger(__name__)

SPECS_RELDIR = Path("specs")


SYSTEM_PROMPT = """你是决策规约编辑。给你 N 条同类决策的 L1 结构化记录,
你要总结出一条 **可执行的决策规约**(spec)。

输出 JSON:
{
  "slug": "小写下划线英文/拼音 slug, ≤64 字符",
  "title": "一句话标题",
  "statement": "一句话决策规则: 在 X 场景下,应该 Y(因为 Z)",
  "rationale": "为什么这条规则成立 — 多句话,可引用具体 raw 信号"
}

只输出 JSON,不要 markdown。"""


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
        result = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def _format_cluster(raw_ids: list[int]) -> str:
    parts = [f"# 共 {len(raw_ids)} 条同类 L1\n"]
    with session() as s:
        for rid in raw_ids:
            l1 = s.get(L1Result, rid)
            raw = s.get(RawInput, rid)
            if l1 is None or raw is None:
                continue
            parts.append(f"## raw#{rid}")
            parts.append(f"- 原文: {raw.content_text[:300]}")
            parts.append(f"- 场景: {l1.scene}")
            parts.append(f"- 信号: {l1.signals_json}")
            parts.append(f"- 选择: {l1.choice}")
            parts.append(f"- 原因: {l1.rationale}")
            parts.append("")
    return "\n".join(parts)


def draft_spec_from_cluster(raw_ids: list[int]) -> SpecCandidate | None:
    """对一簇 raw_ids 跑 spec draft → 入 spec_candidates 表。"""
    if len(raw_ids) < 2:
        return None
    cluster_text = _format_cluster(raw_ids)
    try:
        reply = run("ask", system=SYSTEM_PROMPT, user=cluster_text, temperature=0.2)
    except Exception as e:  # noqa: BLE001
        log.warning("spec draft LLM failed: %s", e)
        return None

    data = _parse_json(reply)
    if data is None:
        log.warning("spec draft bad JSON: %s", reply[:200])
        return None

    slug = str(data.get("slug", "")).strip().lower()[:128]
    title = str(data.get("title", "")).strip()[:255]
    statement = str(data.get("statement", "")).strip()
    rationale = str(data.get("rationale", "")).strip()
    if not slug or not statement:
        return None

    with session() as s:
        existing = s.execute(
            select(SpecCandidate).where(SpecCandidate.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            existing.cluster_raw_ids_json = json.dumps(sorted(set(raw_ids + json.loads(existing.cluster_raw_ids_json or "[]"))))
            existing.statement = statement
            existing.rationale = rationale
            s.commit()
            return s.get(SpecCandidate, existing.id)
        row = SpecCandidate(
            slug=slug,
            title=title or slug,
            statement=statement,
            rationale=rationale,
            cluster_raw_ids_json=json.dumps(raw_ids),
        )
        s.add(row)
        s.commit()
        return s.get(SpecCandidate, row.id)


def _spec_md(sc: SpecCandidate) -> str:
    refs = json.loads(sc.cluster_raw_ids_json or "[]")
    fm = [
        "---",
        f"slug: {sc.slug}",
        f"title: {sc.title}",
        f"review_status: {sc.review_status}",
        f"created_at: {sc.created_at.isoformat() if sc.created_at else ''}",
        f"promoted_at: {sc.promoted_at.isoformat() if sc.promoted_at else ''}",
        f"raw_refs: {refs}",
        "---",
        "",
        f"# {sc.title}",
        "",
        "## 规则",
        "",
        sc.statement,
        "",
        "## 理由",
        "",
        sc.rationale,
        "",
        "## 支撑 raw",
        "",
    ]
    for r in refs:
        fm.append(f"- raw#{r}")
    return "\n".join(fm) + "\n"


def promote_spec(slug: str, *, reviewer: str = "") -> str | None:
    """把 spec_candidate 标 approved + 落到 git。返回 git 相对路径。"""
    s = get_settings()
    with session() as sess:
        sc = sess.execute(
            select(SpecCandidate).where(SpecCandidate.slug == slug)
        ).scalar_one_or_none()
        if sc is None:
            return None
        sc.review_status = "approved"
        sc.promoted_at = datetime.now(timezone.utc)
        rel = SPECS_RELDIR / f"{sc.slug}.md"
        sc.git_path = str(rel)
        abs_path = s.helper_spec_git_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(_spec_md(sc), encoding="utf-8")
        sess.commit()

    repo = Repo(s.helper_spec_git_dir)
    repo.index.add([str(rel)])
    if repo.is_dirty():
        msg = f"spec: promote {slug}"
        if reviewer:
            msg += f" (review by {reviewer})"
        repo.index.commit(msg)
    return str(rel)
