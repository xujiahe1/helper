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
from helper.storage.models import AskAnswer

log = logging.getLogger(__name__)


@dataclass
class Answer:
    answer: str = ""
    confidence: str = "unknown"  # high / medium / low / unknown
    citations: list[dict] = field(default_factory=list)  # [{type, ref}]
    bundle_version: str = ""
    model: str = ""
    answer_id: int | None = None


@dataclass
class RouteRequest:
    """ask runtime 判定: 这条问题应转发给外部 bot, 不该 helper 自答。

    forwarded_text 不在这里出, 由调用方用用户原始 text 直接转发(避免 LLM 改写画蛇添足)。
    """

    target_app_id: str
    via_label: str
    bundle_version: str = ""
    model: str = ""


SYSTEM_PROMPT = """你是企业内部决策规约助手。回答必须基于检索到的"已沉淀规约/entity/历史 raw 判断"。

如果消息附了「历史对话」段(用户和 bot 之前几轮的对话),用它来理解当前问题里的
指代和承接(如"再读一下"是对上一条 bot 回复的回应,"那个文档"指上文出现过的 KM 链接)。
历史对话只用于解情境,不能作为引用依据,citations 仍只从检索结果里挑。

如果消息附了「用户引用的消息」段, 这是用户在问当前问题时显式引用(quote)的那条 —
它是问题所指的具体对象, 优先以它作为承接对象, 而不是历史对话里的最近一条。

如果消息里有「引用文档」段(用户随消息附的 KM 文档全文),回答时优先用文档内容,
但 citations 仍只能从「检索结果」里挑(文档可能还没沉淀到知识库)。

# 输出格式 — 固定 markdown 分段, 不要 JSON

## 路由分支(优先判断)

如果用户偏好(procedural memory)里写过"<某类问题> 应路由到外部 bot 处理, app_id 是 cli_xxx",
而当前问题命中了那类场景, 不要自己回答, 在第一行单独输出:

ROUTE: cli_xxx | <bot 名字, 如 tachi>

(就这一行, 不要任何前后内容。`|` 后是显示用名字, 没名字就只写 app_id。)

判断从严: 必须 memory 里有明确的"路由给 bot X"指令, 且当前问题命中 memory 里写的场景。
不要凭直觉路由(比如看到"app_id"字面就路由), 除非 memory 里写了"问 app_id 类问题 @ X"。

## 答题分支(默认)

不路由就按下面三段输出, 段标题严格 `## 答复` / `## 置信度` / `## 引用`, 顺序固定:

## 答复
<对用户问题的答复, 自由 markdown, 引号 / 换行 / 列表随便用, 不需要任何转义>

## 置信度
high

(三选一: high / medium / low。判断:
- high: 至少一条 spec 直接命中
- medium: 多条 entity / raw 间接相关
- low: 检索结果薄弱, 基于通用判断回答)

## 引用
- spec: <slug>
- entity: <slug>
- raw: <id>

(每行一条, 形如 `- <type>: <ref>`。type 限 spec / entity / raw / fact / case / relation。
没有可信依据就 `## 引用` 后面留空, 同时把"## 答复"写成"我不知道", 置信度 low。
不要编造引用 — 只能从下面"检索结果"段里挑。)"""


def _format_hits(hits: list[Hit]) -> str:
    if not hits:
        return "(检索无结果)"
    lines = ["## 检索结果"]
    for h in hits:
        lines.append(f"\n### {h.type}#{h.ref} — {h.title}  (score={h.score:.2f})")
        lines.append(h.body)
    return "\n".join(lines)


def _format_chat_context(
    chat_id: str,
    *,
    fallback_author: str = "",
    exclude_raw_id: int | None = None,
    asker_domain: str = "",
) -> str:
    """统一走 raw_store.format_context_block。asker_domain 触发 ACL 过滤,
    防白名单用户的敏感聊天通过群历史穿透给非白名单 asker。"""
    with session() as s:
        return raw_store.format_context_block(
            s,
            chat_id=chat_id,
            fallback_author=fallback_author,
            exclude_raw_id=exclude_raw_id,
            asker_domain=asker_domain,
        )


_FENCE_RE = re.compile(r"```(?:\w+)?\s*(.+?)\s*```", re.DOTALL)
_ROUTE_LINE_RE = re.compile(r"^\s*ROUTE\s*:\s*([^\s|]+)\s*(?:\|\s*(.+?))?\s*$", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(\S.*?)\s*$", re.MULTILINE)
_CITATION_LINE_RE = re.compile(r"^\s*[-*]\s*([A-Za-z]+)\s*:\s*(\S.*?)\s*$")


def _strip_fence(text: str) -> str:
    """LLM 可能把整个回复包在 ``` … ``` 里, 剥掉。"""
    text = text.strip()
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text


def _parse_route(text: str) -> tuple[str, str] | None:
    """识别首行 `ROUTE: <app_id> | <label>` 哨兵。返 (app_id, label) 或 None。

    宽容: 哨兵不必严格在第 0 行, 只要在前 3 行内单独成行即可。
    """
    body = _strip_fence(text)
    m = _ROUTE_LINE_RE.search(body)
    if not m:
        return None
    # 前面只允许空行 — 防止 LLM 把 ROUTE 写在答复正文里
    pre = body[: m.start()].strip()
    if pre:
        return None
    app_id = m.group(1).strip()
    label = (m.group(2) or "").strip()
    return (app_id, label) if app_id else None


def _parse_sections(text: str) -> dict[str, str]:
    """切 `## 标题\\n正文` 段, 返 {title: body}。title 已 lower-strip。"""
    body = _strip_fence(text)
    matches = list(_SECTION_RE.finditer(body))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[title] = body[start:end].strip()
    return out


def _parse_citations(block: str) -> list[dict[str, str]]:
    """从 `## 引用` 段切出 [{type, ref}]。空行/非法行跳过。"""
    out: list[dict[str, str]] = []
    for line in block.splitlines():
        m = _CITATION_LINE_RE.match(line)
        if not m:
            continue
        ctype = m.group(1).strip().lower()
        cref = m.group(2).strip().rstrip(",;.")
        if ctype and cref:
            out.append({"type": ctype, "ref": cref})
    return out


def _format_quoted_message(parent_message_id: str) -> str:
    """按 wave_msg_id 反查被引用消息的原文, 拼成单独一段。抽不到返空串。

    引用对象可能是用户更早的消息, 也可能是 bot 自己之前的回复(im_wave_bot:* 来源)。
    都按时间 + 作者 + 正文一行格式化, 单条上限 800 字, 防爆 prompt。
    """
    if not parent_message_id:
        return ""
    with session() as s:
        row = raw_store.get_by_wave_msg_id(s, parent_message_id)
        if row is None:
            return ""
        ts = row.created_at.strftime("%m-%d %H:%M") if row.created_at else "?"
        if (row.source_type or "").startswith("im_wave_bot"):
            who = "bot"
        else:
            who = f"用户({row.author_domain or '?'})"
        body = (row.content_text or "").strip()
    if len(body) > 800:
        body = body[:800] + "…"
    if not body:
        return ""
    return (
        "## 用户引用的消息(用户在问当前问题时显式引用了这条 — 这就是问题所指的对象)\n"
        f"[{ts}] {who}: {body}"
    )


def _format_inline_docs(docs: list[dict] | None) -> str:
    """把用户随消息附的 KM 文档拼成一段。每个 dict: {title, body, source_url}。"""
    if not docs:
        return ""
    lines = ["## 引用文档(用户随消息附的)"]
    for d in docs:
        title = (d.get("title") or "").strip() or "(无标题)"
        body = (d.get("body") or "").strip()
        url = (d.get("source_url") or "").strip()
        head = f"\n### {title}"
        if url:
            head += f"  ({url})"
        lines.append(head)
        lines.append(body)
    return "\n".join(lines)


def ask(
    question: str,
    *,
    asker_domain: str = "",
    wave_msg_id: str = "",
    chat_id: str = "",
    raw_id: int | None = None,
    inline_context: list[dict] | None = None,
    parent_message_id: str = "",
) -> Answer | RouteRequest:
    """Surface 4 主入口。

    chat_id 非空时(群聊),拼入近期群聊上下文。raw_id 排除当前这条问题本身。
    inline_context: 用户消息里附的 KM 文档(已 fetch 到正文),作为这次问答的额外素材。
        每项 {"title": str, "body": str, "source_url": str}。
    parent_message_id: 用户在 Wave 里 quote 的那条消息 id (webhook event.message.quote_msg_id);
        非空时反查原文拼到 prompt, 让 LLM 看到用户具体引用的内容(可能在历史对话窗口外)。
    """
    # 群聊用 chat_id, 单聊用 asker_domain 兜底, 默认都拼上下文。
    # 透传 asker_domain → format_context_block 按 ACL 跳过敏感历史,
    # 防白名单用户连续聊敏感话题后非白名单 asker 穿插提问时仍能拿到上下文。
    ctx = _format_chat_context(
        chat_id, fallback_author=asker_domain, exclude_raw_id=raw_id,
        asker_domain=asker_domain,
    )

    # ACL 入口短路: 问题 + 历史命中受控 topic 且 asker 非白名单 → 直接拒, 不调主路径 LLM。
    # 防"新内容还没 ingest 时仍泄"或"问题敏感但检索没召回"两种漏点。
    try:
        from helper.acl import deny_for_question
        deny = deny_for_question(asker_domain, question, chat_context=ctx)
    except Exception:  # noqa: BLE001
        log.exception("acl deny_for_question failed; default to not deny")
        deny = None
    if deny is not None:
        log.info("acl denied ask asker=%s question=%r", asker_domain, question[:80])
        return Answer(
            answer=deny, confidence="low",
            bundle_version=current_bundle_version(),
        )

    hits = retrieve_relevant(question, top_k=8, asker_domain=asker_domain)
    parts = [f"# 用户问题\n{question}"]
    if ctx:
        parts.append(ctx)
    quoted = _format_quoted_message(parent_message_id)
    if quoted:
        parts.append(quoted)
    inline = _format_inline_docs(inline_context)
    if inline:
        parts.append(inline)
    parts.append(_format_hits(hits))
    user_msg = "\n\n".join(parts)

    # 用户偏好(procedural memory)— 命中 entity 的 + global 的拼进 system prompt 末尾
    from helper.memory import directives_for_ask
    entity_refs = [h.ref for h in hits if h.type == "entity"]
    prefs = directives_for_ask(entity_refs=entity_refs)
    system_prompt = SYSTEM_PROMPT + ("\n\n" + prefs if prefs else "")

    routing = current_routing()
    model = routing.tasks["ask"].model

    try:
        reply = run("ask", system=system_prompt, user=user_msg, temperature=0.2)
    except Exception as e:  # noqa: BLE001
        log.warning("ask LLM failed: %s", e)
        return Answer(
            answer="抱歉,我这边暂时连不上模型,稍后再问我一次。",
            confidence="unknown",
            bundle_version=current_bundle_version(),
            model=model,
        )

    # 路由分支: 首行 ROUTE: <app_id> | <label> 哨兵
    route = _parse_route(reply)
    if route is not None:
        target_app_id, via_label = route
        return RouteRequest(
            target_app_id=target_app_id,
            via_label=via_label or target_app_id,
            bundle_version=current_bundle_version(),
            model=model,
        )

    sections = _parse_sections(reply)
    answer_text = sections.get("答复", "").strip()
    if not answer_text:
        # 解析失败兜底: LLM 没按格式输出 → 直接吃整段回复但去 fence
        answer_text = _strip_fence(reply)
    confidence = sections.get("置信度", "").strip().lower().splitlines()[0:1]
    confidence = (confidence[0] if confidence else "").strip()
    if confidence not in ("high", "medium", "low"):
        confidence = "unknown"
    citations: list[dict[str, Any]] = _parse_citations(sections.get("引用", ""))

    # ACL 出口硬过滤: LLM 即便没拿到敏感原文 (retrieve 已过滤 + chat_context 已过滤),
    # 仍可能凭参数知识 / 迂回话术自己脑补出敏感名字 → 整段替换为 deny_response。
    try:
        from helper.acl import scrub_output
        scrubbed = scrub_output(asker_domain, answer_text)
    except Exception:  # noqa: BLE001
        log.exception("acl scrub_output failed; default to no scrub")
        scrubbed = None
    if scrubbed is not None:
        log.info("acl scrubbed answer asker=%s", asker_domain)
        answer_text = scrubbed
        confidence = "low"
        citations = []

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
    """把 Answer 渲染成给 IM 用户看的纯文本。"""
    return ans.answer
