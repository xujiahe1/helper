"""Ask runtime — 拼 prompt → 主路径模型 → 答案 + 引用 + 不确定性。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from helper.ask.retrieve import Hit, retrieve_relevant
from helper.compiler import current_bundle_version
from helper.llm import run
from helper.llm.router import current_routing
from helper.storage import raw_store, session
from helper.storage.models import AskAnswer, RawInput

log = logging.getLogger(__name__)


@dataclass
class Answer:
    answer: str = ""
    confidence: str = "unknown"  # high / medium / low / unknown
    citations: list[dict] = field(default_factory=list)  # [{type, ref}]
    bundle_version: str = ""
    model: str = ""
    answer_id: int | None = None


SYSTEM_PROMPT = """你是企业内部决策规约助手。回答必须基于检索到的"已沉淀规约/entity/历史 raw 判断"。

如果用户问题里有指代(它/这个/上面说的),先看「最近群聊上下文」段把指代解出来,再回答。
群聊上下文只用于解指代和理解情境,不能作为引用依据,citations 仍只从检索结果里挑。

输出 JSON:
{
  "answer": "对用户问题的答复,简短",
  "confidence": "high | medium | low",
  "citations": [{"type": "spec|entity|raw", "ref": "<slug 或 raw_id>"}]
}

判断 confidence:
- high: 至少一条 spec 直接命中
- medium: 多条 entity / raw 间接相关
- low: 检索结果薄弱,基于通用判断回答

不要编造引用 — citations 只能从下面提供的检索结果里挑。
没有可信依据就直说"我不知道",confidence=low,citations=[]。
只输出 JSON,不要 markdown。"""


def _format_hits(hits: list[Hit]) -> str:
    if not hits:
        return "(检索无结果)"
    lines = ["## 检索结果"]
    for h in hits:
        lines.append(f"\n### {h.type}#{h.ref} — {h.title}  (score={h.score:.2f})")
        lines.append(h.body)
    return "\n".join(lines)


def _format_chat_context(
    chat_id: str, *, limit: int = 30, since_days: int = 3, exclude_raw_id: int | None = None
) -> str:
    """拉本地 raw_inputs 拼出近期群聊上下文。空则返空串。

    格式: 每行 "[时间] 域账号: 内容"。
    内容裁到 200 字以内,避免 prompt 爆炸。
    """
    if not chat_id:
        return ""
    with session() as s:
        rows: list[RawInput] = raw_store.list_chat_history(
            s, chat_id, limit=limit, since_days=since_days, exclude_raw_id=exclude_raw_id
        )
        if not rows:
            return ""
        lines = [f"## 最近群聊上下文(近 {since_days} 天 / {len(rows)} 条)"]
        for r in rows:
            ts = r.created_at.strftime("%m-%d %H:%M") if r.created_at else "?"
            who = r.author_domain or "?"
            text = (r.content_text or "").strip().replace("\n", " ")
            if len(text) > 200:
                text = text[:200] + "…"
            lines.append(f"[{ts}] {who}: {text}")
    return "\n".join(lines)


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


def ask(
    question: str,
    *,
    asker_domain: str = "",
    wave_msg_id: str = "",
    chat_id: str = "",
    raw_id: int | None = None,
) -> Answer:
    """Surface 4 主入口。

    chat_id 非空时(群聊),拼入近期群聊上下文。raw_id 排除当前这条问题本身。
    """
    hits = retrieve_relevant(question, top_k=8)
    parts = [f"# 用户问题\n{question}"]
    ctx = _format_chat_context(chat_id, exclude_raw_id=raw_id) if chat_id else ""
    if ctx:
        parts.append(ctx)
    parts.append(_format_hits(hits))
    user_msg = "\n\n".join(parts)

    routing = current_routing()
    model = routing.tasks["ask"].model

    try:
        reply = run("ask", system=SYSTEM_PROMPT, user=user_msg, temperature=0.2)
    except Exception as e:  # noqa: BLE001
        log.warning("ask LLM failed: %s", e)
        return Answer(
            answer=f"(系统暂时无法回答: {type(e).__name__})",
            confidence="unknown",
            bundle_version=current_bundle_version(),
            model=model,
        )

    data = _parse_json(reply) or {}
    answer_text = str(data.get("answer", reply.strip()))
    confidence = str(data.get("confidence", "unknown")).lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "unknown"
    citations_raw = data.get("citations", [])
    citations: list[dict[str, Any]] = []
    if isinstance(citations_raw, list):
        for c in citations_raw:
            if isinstance(c, dict) and "type" in c and "ref" in c:
                citations.append({"type": str(c["type"]), "ref": str(c["ref"])})

    bundle_v = current_bundle_version()
    with session() as s:
        row = AskAnswer(
            asker_domain=asker_domain,
            question=question,
            answer=answer_text,
            confidence=confidence,
            citations_json=json.dumps(citations, ensure_ascii=False),
            spec_bundle_version=bundle_v,
            model=model,
            wave_msg_id=wave_msg_id,
        )
        s.add(row)
        s.commit()
        answer_id = row.id

    return Answer(
        answer=answer_text,
        confidence=confidence,
        citations=citations,
        bundle_version=bundle_v,
        model=model,
        answer_id=answer_id,
    )


def render_for_wave(ans: Answer) -> str:
    """把 Answer 渲染成给 IM 用户看的纯文本。

    - high 置信度:只展示答复正文,不附加引用(引用对用户是噪音)
    - 非 high:展示答复 + 一句"参考了 N 条 spec/事实/历史判断"(只露聚合,不露 ID)
    """
    parts = [ans.answer]
    if ans.confidence != "high":
        type_count: dict[str, int] = {}
        for c in ans.citations:
            t = c.get("type", "")
            type_count[t] = type_count.get(t, 0) + 1
        if type_count:
            label_map = {
                "spec": "规约", "entity": "概念", "fact": "事实",
                "case": "案例", "raw": "历史判断",
            }
            tag = "、".join(
                f"{n} 条{label_map.get(t, t)}" for t, n in type_count.items()
            )
            parts.append(f"\n(置信度: {ans.confidence};参考: {tag})")
        else:
            parts.append(f"\n(置信度: {ans.confidence})")
    return "\n".join(parts)
