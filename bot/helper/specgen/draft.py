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
from helper.storage.models import ConflictLog, L1Item, RawInput, SpecCandidate

log = logging.getLogger(__name__)

SPECS_RELDIR = Path("specs")

# 静默期阈值 — 簇内最新 decision 距今超过这天数 + 簇没 promote 过 → 强触发 draft,
# 让老线索善终, 不会因为始终凑不齐 ≥ 3 条而被永远遗忘。
_SILENT_DAYS = 90


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


_UNIVERSAL_SYSTEM_PROMPT = """判断下面这一段判断/陈述是不是**普适性表述** —
即"以后所有同类场景都按这个来"的规则, 而不是"这一次的具体决定"。

普适表述信号: "以后"、"统一"、"开始这样"、"一律"、"所有"、"都按 X"、
              "X 类问题统一 Y"、明确说"以后都这么做"

单次决定信号: "这次"、"这一次"、"暂时"、"先这样"、只描述一次具体事件
              没说推广, 或只是当下场景的一次选择

输出 JSON: {"is_universal": true|false, "reason": "<一句话理由>"}
只输出 JSON, 不要 markdown。"""


def _check_universal(cluster_text: str) -> bool:
    """跑 spec_universal_check task, 判断 cluster 是不是普适表述。
    LLM 失败或解析不出 → fallback False (保守, 不让数据噪声变成误沉淀)。"""
    try:
        reply = run(
            "spec_universal_check",
            system=_UNIVERSAL_SYSTEM_PROMPT,
            user=cluster_text,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("spec_universal_check LLM failed: %s", e)
        return False
    data = _parse_json(reply)
    if not data:
        return False
    return bool(data.get("is_universal"))


def _cluster_latest_age_days(cluster_keys: list[tuple[int, int]]) -> int:
    """簇内 decision 对应 raw 的 created_at 最大值 → now 的天数。
    空簇 / 查不到任何 raw → 0 (相当于"刚发生", 不触发静默)。"""
    if not cluster_keys:
        return 0
    raw_ids = list({k[0] for k in cluster_keys})
    with session() as s:
        rows = s.execute(
            select(RawInput.created_at).where(RawInput.id.in_(raw_ids))
        ).all()
    times = [r[0] for r in rows if r[0] is not None]
    if not times:
        return 0
    latest = max(times)
    if latest.tzinfo is None:
        # 老数据 created_at 可能 naive, 当 UTC 处理
        latest = latest.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - latest
    return max(0, delta.days)


def _cluster_already_drafted(cluster_keys: list[tuple[int, int]]) -> bool:
    """簇里任一 (raw_id, idx) 已被任何 SpecCandidate 引用 → 算 promote 过。

    不论 superseded 与否都算: 这簇已被 owner 关注过, 老 key 重燃应走
    conflict 路径而非新草稿, 避免静默期触发反复打扰。
    """
    if not cluster_keys:
        return False
    target = {tuple(k) for k in cluster_keys}
    with session() as s:
        rows = s.execute(select(SpecCandidate.cluster_raw_ids_json)).all()
    for (raw_json,) in rows:
        if not raw_json:
            continue
        try:
            data = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            continue
        for k in data:
            if isinstance(k, list) and len(k) == 2 and tuple(k) in target:
                return True
    return False


def draft_spec_from_cluster(cluster_keys: list[tuple[int, int]]) -> SpecCandidate | None:
    """对一簇 decision 原子跑 spec draft → 入 spec_candidates 表。

    触发判据 (满足任一):
    - 普适: LLM 判 cluster 文本是 "以后都这样" 语义 → 1 条即触发
    - 饱和: ≥ 3 条同类 decision → 数量信号已强
    - 静默: 最新 decision ≥ 90 天 + 簇没 promote 过 → 让老线索善终
    都不满足 → return None
    """
    if not cluster_keys:
        return None
    cluster_text = _format_cluster(cluster_keys)
    n = len(cluster_keys)
    is_universal = _check_universal(cluster_text)

    if is_universal:
        log.info("spec draft triggered: universal (n=%d)", n)
    elif n >= 3:
        log.info("spec draft triggered: saturation (n=%d)", n)
    elif (
        _cluster_latest_age_days(cluster_keys) >= _SILENT_DAYS
        and not _cluster_already_drafted(cluster_keys)
    ):
        log.info("spec draft triggered: silence (n=%d)", n)
    else:
        return None

    return _do_draft(cluster_keys, cluster_text, topic_id=None)


def draft_spec_from_topic(topic_id: int) -> SpecCandidate | None:
    """改动 3: 由 scan_topics_for_draft 决定该触发后, 直接对 topic 的 keys 跑 draft。

    与 draft_spec_from_cluster 区别: scan 阶段已用结构性判据 (饱和/静默) 过滤,
    这里仅再跑一次 _check_universal 用于日志和保留普适触发的能力, 但**不影响**
    是否落库 — scan 让你来你就 draft, 走结构性判据。

    成功 draft 后:
    - SpecCandidate.topic_id = topic_id
    - SpecTopic.last_promoted_at = now (节流冷却 30 天)
    """
    from helper.specgen.cluster import topic_keys
    from helper.storage.models import SpecTopic

    keys = topic_keys(topic_id)
    if not keys:
        log.warning("draft_spec_from_topic: topic=%s 已无 decision keys", topic_id)
        return None
    cluster_text = _format_cluster(keys)
    is_universal = _check_universal(cluster_text)
    log.info(
        "spec draft from topic=%s (n=%d, universal=%s)",
        topic_id, len(keys), is_universal,
    )

    sc = _do_draft(keys, cluster_text, topic_id=topic_id)
    if sc is None:
        return None

    # 不论 sc 是新建/合并/已 approved 防覆写 — 都标 topic 已被处理过, 进入冷却。
    with session() as s:
        topic = s.get(SpecTopic, topic_id)
        if topic is not None:
            topic.last_promoted_at = datetime.now(timezone.utc)
            s.commit()
    return sc


def _do_draft(
    cluster_keys: list[tuple[int, int]],
    cluster_text: str,
    *,
    topic_id: int | None,
) -> SpecCandidate | None:
    """改动 3: 实际跑 LLM 出 spec + 落库的核心。 不做触发判据 — 调用方负责。

    topic_id 非 None → 新建 SpecCandidate 时写入; 老调用方传 None 走老语义。
    """
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
            # 关键保护: 已 approved 的 spec 不被新草稿无声覆写。
            # 改成挂 ConflictLog target_type='spec', summary 描述触动了已批准规约,
            # owner 在周报第 2 段「采纳/保留/都留」裁决后, 再用 pending_payload_json
            # 决定真覆写 / 丢弃新内容 / 起 -v2 旁路。
            if existing.review_status == "approved":
                # 取代表 raw_id (cluster 里第一条) 作为冲突 raw 锚点
                anchor_raw_id = cluster_keys[0][0] if cluster_keys else 0
                # 幂等: 同 (raw, target) 已有 open 冲突 → 不重写, 直接返已批准 spec
                already = s.execute(
                    select(ConflictLog)
                    .where(ConflictLog.raw_id == anchor_raw_id)
                    .where(ConflictLog.target_type == "spec")
                    .where(ConflictLog.target_slug == slug)
                    .where(ConflictLog.resolution == "open")
                ).scalar_one_or_none()
                if already is None:
                    payload = {
                        "slug": slug, "title": title or slug,
                        "statement": statement, "rationale": rationale,
                        "keys": keys_json,
                    }
                    s.add(ConflictLog(
                        raw_id=anchor_raw_id,
                        target_type="spec", target_slug=slug,
                        summary=(
                            f"新 cluster 草稿与已批准规约 [{slug}] 不一致, 等待裁决: "
                            f"采纳=覆盖旧规约 / 保留=丢弃新草稿 / 都留=新草稿改 slug 旁路"
                        ),
                        severity="medium",
                        pending_payload_json=json.dumps(payload, ensure_ascii=False),
                    ))
                    s.commit()
                    log.info(
                        "spec draft hit approved %s, parked as conflict (cluster=%d)",
                        slug, len(cluster_keys),
                    )
                # 不返新 candidate — 当前簇被冻结直到 owner 裁决
                return s.get(SpecCandidate, existing.id)
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
            topic_id=topic_id,
        )
        s.add(row)
        s.commit()
        # spec candidate 也进 FTS — review 前 retrieve 也能召到草稿,不只是 git 落了的
        try:
            from helper.storage import fts
            fts.index_spec(s, row.slug)
        except Exception:  # noqa: BLE001
            log.exception("fts.index_spec failed slug=%s", row.slug)
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
