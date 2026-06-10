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


SYSTEM_PROMPT = """你是企业内部决策规约助手,也是可以自然对话的大模型助手。

检索到的"已沉淀规约/entity/历史 raw 判断"是增强材料,不是你开口回答的前提。
有检索结果时,优先使用它们回答;没有检索结果时,也可以基于你的通用知识、
当前对话和助手角色继续帮助用户。只有当用户明确询问企业内部事实、已有规约、
历史判断、人物/项目等需要库内依据的问题,而检索结果不足时,才说明当前知识库里没有查到依据。

如果消息附了「历史对话」段(用户和 bot 之前几轮的对话),用它来理解当前问题里的
指代和承接(如"再读一下"是对上一条 bot 回复的回应,"那个文档"指上文出现过的 KM 链接)。
历史对话只用于解情境。

如果消息附了「用户引用的消息」段, 这是用户在问当前问题时显式引用(quote)的那条 —
它是问题所指的具体对象, 优先以它作为承接对象, 而不是历史对话里的最近一条。

如果消息里有「引用文档」段(用户随消息附的 KM 文档全文),回答时优先用文档内容。

# 路由哨兵(优先判断, 命中则只输出哨兵这一行)

如果用户偏好(procedural memory)里写过"涉及 X / Y / Z 类问题, 全部去艾特 bot N 询问"
之类的路由指令, 当前问题命中或接近 X / Y / Z 描述的范围 → **不要自己回答, 也不要建议
用户去艾特**, 在第一行单独输出:

ROUTE: <bot 名字>

(就这一行, 不要任何前后内容。系统会按 bot 名字反查路由目标自动转发, 你不需要也不
应该知道 bot 的 app_id 或任何 cli_xxx hash。)

判断宽松: directive 描述的关键词、主题域、相邻概念都算命中, 让目标 bot 自己决定能不能答。
反过来, 题面跟所有 directive 描述的范围**都不沾边**时(纯天气、纯通用知识), 才自答。

# 自答分支

不路由就直接给答案, 自由 markdown, 不要分段标题, 不要"置信度", 不要"引用"列表,
不要"根据知识库..."这种引述话术 — 该说什么直接说。"""


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
# 新格式: ROUTE: <bot 名字>。 兼容旧 ROUTE: cli_xxx | <name> 形态(过渡期),
# 但旧形态下我们丢弃 cli_ 部分, 仍按 name 反查 — 防 LLM 误抄过期 hash。
_ROUTE_LINE_RE = re.compile(r"^\s*ROUTE\s*:\s*(.+?)\s*$", re.MULTILINE)


def _strip_fence(text: str) -> str:
    """LLM 可能把整个回复包在 ``` … ``` 里, 剥掉。"""
    text = text.strip()
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text


def _parse_route(text: str) -> str | None:
    """识别首行 `ROUTE: <bot 名字>` 哨兵, 返 bot 名字 (= entity scope_ref) 或 None。

    宽容:
      - 哨兵不必严格在第 0 行, 只要在前 3 行内单独成行即可
      - 兼容旧 `ROUTE: cli_xxx | <name>` 形态: LLM 万一抄了缓存的旧格式, 我们丢弃
        cli_ 部分(可能是过期/编造的 hash), 取 `|` 后的 name; 如果没有 `|`, 那这条
        ROUTE 是裸 cli_, 我们当解析失败 → None, 强制走答题分支兜底。

    返回 None 时调用方走答题分支。返回 "" 也视作失败。
    """
    body = _strip_fence(text)
    m = _ROUTE_LINE_RE.search(body)
    if not m:
        return None
    pre = body[: m.start()].strip()
    if pre:
        return None
    raw = m.group(1).strip()
    if "|" in raw:
        # 旧格式 `cli_xxx | name`: 取 `|` 后的 name
        _, _, name = raw.partition("|")
        name = name.strip()
        return name or None
    if raw.startswith("cli_"):
        # 裸 cli_xxx 没 `|` — 没法反查 entity 名, 当解析失败
        return None
    return raw or None


def _format_quoted_message(parent_message_id: str, *, asker_domain: str = "") -> str:
    """按 wave_msg_id 反查被引用消息的原文, 拼成单独一段。抽不到返空串。

    引用对象可能是用户更早的消息, 也可能是 bot 自己之前的回复(im_wave_bot:* 来源)。
    都按时间 + 作者 + 正文一行格式化, 单条上限 800 字, 防爆 prompt。

    asker_domain 非空时按 ACL 过滤: 非白名单 asker quote 了带 acl_topic_id 标的
    raw → 整段不注入(像没引用过)。防群历史已过滤但 quote 仍把敏感原文塞进 prompt。
    """
    if not parent_message_id:
        return ""
    ts = "?"
    who = "用户"
    body = ""
    with session() as s:
        row = raw_store.get_by_wave_msg_id(s, parent_message_id)
        if row is not None:
            if asker_domain:
                try:
                    from helper.acl import is_allowed
                    if not is_allowed(asker_domain, getattr(row, "acl_topic_id", "") or ""):
                        log.info(
                            "acl blocked quoted msg asker=%s topic=%s",
                            asker_domain, getattr(row, "acl_topic_id", "") or "",
                        )
                        return ""
                except Exception:  # noqa: BLE001
                    log.exception("acl quoted-msg check failed; default to not inject")
                    return ""
            ts = row.created_at.strftime("%m-%d %H:%M") if row.created_at else "?"
            if (row.source_type or "").startswith("im_wave_bot"):
                who = "bot"
            else:
                who = f"用户({row.author_domain or '?'})"
            body = (row.content_text or "").strip()
            # 兜底 1: row 拿到但 content_text 是 envelope JSON (历史 raw 在 merge_forward
            # 抽取器上线前入库的, 或非 text/rich_text 类型抽不到文本退化存的原文) →
            # 走 wave OpenAPI 拉一次远端, 用新抽取器再解一遍。
            if body and (body.startswith("{\"schema\"") or body.startswith("{\"message_list\"")):
                body = ""

    # 兜底 2: 本地完全反查不到 → 走 wave OpenAPI message/get 拉远端。
    # 适用场景: bot 没在源群 / 历史早于 bot 入群 / webhook 错过投递。
    if not body:
        try:
            from helper.im import wave_client
            from helper.im.wave_webhook import extract_text_from_content
            remote = wave_client.get_message(parent_message_id)
            if remote:
                remote_text = extract_text_from_content(
                    remote.get("msg_type", "") or "",
                    remote.get("content", "") or "",
                )
                if remote_text:
                    body = remote_text.strip()
                    log.info(
                        "quoted parent fallback via OpenAPI parent=%s len=%d",
                        parent_message_id, len(body),
                    )
            else:
                log.warning(
                    "quoted parent not in raw_inputs and OpenAPI fetch returned empty parent=%s asker=%s",
                    parent_message_id, asker_domain,
                )
        except Exception:  # noqa: BLE001
            log.exception("quoted parent OpenAPI fallback failed parent=%s", parent_message_id)

    if not body:
        return ""
    if len(body) > 1500:
        body = body[:1500] + "…"
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

    # quote 段要在 deny 检查前算: 让入口闸能基于 quote 内容判定话题
    # (题面不敏感但 quote 指向敏感原文的场景)。 _format_quoted_message
    # 自身已按 asker_domain 过滤 — 非白名单 quote 了敏感 raw 直接返空。
    quoted = _format_quoted_message(parent_message_id, asker_domain=asker_domain)

    # ACL 入口短路: 问题 + 历史 + quote 命中受控 topic 且 asker 非白名单 → 直接拒,
    # 不调主路径 LLM。防"新内容还没 ingest 时仍泄"或"问题敏感但检索没召回"两种漏点。
    try:
        from helper.acl import deny_for_question
        deny_ctx = ctx
        if quoted:
            deny_ctx = f"{deny_ctx}\n\n{quoted}" if deny_ctx else quoted
        deny = deny_for_question(asker_domain, question, chat_context=deny_ctx)
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
    if quoted:
        parts.append(quoted)
    inline = _format_inline_docs(inline_context)
    if inline:
        parts.append(inline)
    # directive 是"行为指令"不是"事实", 只走 system prompt 用户偏好段, 不进检索结果
    fact_hits = [h for h in hits if h.type != "directive"]
    parts.append(_format_hits(fact_hits))
    user_msg = "\n\n".join(parts)

    # 用户偏好(procedural memory)— 三路命中合并:
    #   - entity 命中(题面里有该 entity 字面词 → 拼对应 entity scope directive)
    #   - directive 命中(directive 文本本身被 fts/vector 检索召回 → 拼)
    #   - global scope 一律拼
    from helper.memory import directives_for_ask
    entity_refs = [h.ref for h in hits if h.type == "entity"]
    # asker 自己的 canonical 中文名也注入 — 让 scope=entity:<asker 中文名> 的
    # directive 在他 @bot 提问时生效 (题面里没有他名字也能命中)。 alias 表没记录
    # 时走 wave OpenAPI lazy 拉一次落 source='auto' 缓存。
    if asker_domain:
        try:
            from helper.memory.alias import resolve_alias
            asker_canon = resolve_alias(asker_domain)
            if asker_canon == asker_domain:
                from helper.im.wave_user import ensure_alias_for_domain
                asker_canon = ensure_alias_for_domain(asker_domain)
            if asker_canon and asker_canon != asker_domain:
                entity_refs.append(asker_canon)
        except Exception:  # noqa: BLE001
            log.exception("asker→canonical resolve failed asker=%s", asker_domain)
    directive_ids = [int(h.ref) for h in hits if h.type == "directive" and h.ref.isdigit()]
    prefs = directives_for_ask(entity_refs=entity_refs, directive_ids=directive_ids)
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

    # 路由分支: 首行 ROUTE: <bot 名字> 哨兵 → 按名字反查 alive memory.route_app_id
    # 拿真 app_id (LLM 视野里没 hash, 解决 LLM 编造 cli_xxx 的根因)。
    via_label = _parse_route(reply)
    if via_label:
        from helper.memory import resolve_route_app_id
        target_app_id = resolve_route_app_id(via_label)
        if target_app_id:
            return RouteRequest(
                target_app_id=target_app_id,
                via_label=via_label,
                bundle_version=current_bundle_version(),
                model=model,
            )
        # 反查不到 — LLM 给了一个 memory 里没登记 route_app_id 的名字 → 兜底文案
        # (不去解析 reply 后续答题段, 因为 LLM 走的就是 ROUTE 分支, 没生成答题段)
        log.warning("route hint=%r but no alive memory route_app_id; fallback msg", via_label)
        return Answer(
            answer=f"这个问题可能更适合 @{via_label} 来答, 你可以直接 @ 它问问。",
            confidence="low",
            bundle_version=current_bundle_version(),
            model=model,
        )

    # prompt 已不要求 LLM 输出分段标题/置信度/引用 — 整段回复(去 fence)就是答案
    answer_text = _strip_fence(reply).strip()
    confidence = "unknown"
    citations: list[dict[str, Any]] = []

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
