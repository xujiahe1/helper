"""一簇 decision 原子 → candidate spec(LLM draft)。

cluster keys = [(raw_id, idx), ...] — 每条 decision 是 raw 里的一个 L1Item。
spec_candidates.cluster_raw_ids_json 重用为 [[raw_id, idx], ...](老数据 [raw_id]
形式仍可读,会被当成 idx=0)。

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
from helper.storage.models import L1Item, RawInput, SpecCandidate

log = logging.getLogger(__name__)

SPECS_RELDIR = Path("specs")


SYSTEM_PROMPT = """你是决策规约编辑。给你 N 条同类 decision 原子(每条已结构化:
场景 / 信号 / 权衡 / 选择 / 原因),你要总结出一条**可执行的决策规约**(spec)。

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


def _format_cluster(keys: list[tuple[int, int]]) -> str:
    parts = [f"# 共 {len(keys)} 条同类 decision 原子\n"]
    with session() as s:
        for raw_id, idx in keys:
            it = s.execute(
                select(L1Item).where(L1Item.raw_id == raw_id, L1Item.idx == idx)
            ).scalar_one_or_none()
            raw = s.get(RawInput, raw_id)
            if it is None or raw is None:
                continue
            payload = json.loads(it.payload_json or "{}")
            parts.append(f"## raw#{raw_id}#{idx}")
            parts.append(f"- 原文: {raw.content_text[:300]}")
            parts.append(f"- 场景: {payload.get('scene', '')}")
            parts.append(f"- 信号: {payload.get('signals', [])}")
            parts.append(f"- 权衡: {payload.get('tradeoffs', [])}")
            parts.append(f"- 选择: {payload.get('choice', '')}")
            parts.append(f"- 原因: {payload.get('rationale', '')}")
            parts.append("")
    return "\n".join(parts)


def draft_spec_from_cluster(cluster_keys: list[tuple[int, int]]) -> SpecCandidate | None:
    """对一簇 decision 原子跑 spec draft → 入 spec_candidates 表。"""
    if len(cluster_keys) < 2:
        return None
    cluster_text = _format_cluster(cluster_keys)
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

    keys_json = [list(k) for k in cluster_keys]
    with session() as s:
        existing = s.execute(
            select(SpecCandidate).where(SpecCandidate.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            old = json.loads(existing.cluster_raw_ids_json or "[]")
            old_t = {tuple(k) if isinstance(k, list) and len(k) == 2 else (k, 0) for k in old}
            new_t = {tuple(k) for k in keys_json}
            merged = sorted(old_t | new_t)
            existing.cluster_raw_ids_json = json.dumps([list(k) for k in merged])
            existing.statement = statement
            existing.rationale = rationale
            s.commit()
            return s.get(SpecCandidate, existing.id)
        row = SpecCandidate(
            slug=slug,
            title=title or slug,
            statement=statement,
            rationale=rationale,
            cluster_raw_ids_json=json.dumps(keys_json),
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
        if isinstance(r, list) and len(r) == 2:
            fm.append(f"- raw#{r[0]}#{r[1]}")
        else:
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
        try:
            from helper.storage import vector as vec
            vec.index_spec(sess, sc.slug)
        except Exception:  # noqa: BLE001
            log.exception("index_spec failed slug=%s", sc.slug)
        sess.commit()

    repo = Repo(s.helper_spec_git_dir)
    repo.index.add([str(rel)])
    if repo.is_dirty():
        msg = f"spec: promote {slug}"
        if reviewer:
            msg += f" (review by {reviewer})"
        repo.index.commit(msg)
    return str(rel)
