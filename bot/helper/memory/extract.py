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
import math
import re
import struct

from sqlalchemy import select

from helper.llm import run
from helper.storage import raw_store, session
from helper.storage.models import ConflictLog, Memory, RawInput

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """你的任务是从用户的话里识别"对 bot 行为的指令"。

# 抽取判定:白名单模式 — 只在落入下列六类形态之一时才抽,其他全部不抽

memory 表只存"约束 bot 自己怎么做"的指令。客观事实、第三方属性、工作流程、
决策依据等不进 memory(去 L1 / 不进库)。**不在白名单 → 一律不抽**, 即便句子
带"记住 / 记录下 / 你要知道"等祈使外壳也不抽。

## 白名单(满足任一即可抽)

1. **bot 说话方式**:回答时要/不要 X、用 X 语气、用 X 长度、答得简短/详细、
   用 X 语言回复、要/不要附引用、要/不要复述身份。
   例:"回答问题时不要和稀泥,直接给答案" ✅
2. **路由/转发/艾特**:涉及 X 类问题(由 bot)路由/转发/艾特给 Y。
   动作主语必须是 bot 自己,不是产品/人。
   例:"涉及 IAM/认证类问题艾特 tachi" ✅
   反例:"IAM 需求要让产品艾特杨潇推进" — 动作主语是产品,不是 bot,**不抽**
3. **称呼**:bot 称呼某人时用 X。
   例:"陈雨晴本人提问时全程称她为牛姐" ✅
4. **避谈/隐去**:bot 回答时不要提及 / 隐去 / 不要透露 X。
   例:"涉及鳕鱼老师的提问应隐去鳕鱼老师" ✅
5. **答题口径**:对 X 类问题统一回答 / 答案统一用 Y(bot 给出的答案)。
   例:"对 C 端用户问门禁同步时效统一回答 2 小时" ✅
   ⚠️ 区分客观事实陈述:"门禁同步实测 2 小时左右" — **不抽**, 这是客观事实, 进 L1
6. **语义映射**:用户说 X 时按 Y 理解(给 bot 的解析约束)。
   例:"我说哥时指刘佳翔" ✅(也可走 aliases)

## 黑名单反面(明确不抽,即便句子带祈使语气)

- **客观事实/数据/技术参数**:"门禁同步 2 小时"、"X 接口的 token 15 天有效"
- **第三方属性/评价/性格**:"X 不懂 Y"、"X 性格内向"、"X 是好人"、
  "鳕鱼老师是开门学者" — 描述世界,不是约束 bot
- **身份纠正/事实声明**:"X 是 Y"、"X 不是 Z"
- **协作流程/责任分工**:"X 类需求由 Y 推进"、"X 由 Y 负责处理" —
  动作主语是人/部门,不是 bot
- **决策依据/技术原理**:"X 不能 Y 因为 Z"、"A 应该 B 原因是 C" — 进 L1
- **历史/记录陈述**:"过去 X 发生过 Y" — 进 L1

## 替换测试(辅助判断)

把句子改写成"X 是 / X 应该 / X 因为"等纯陈述形态:
- 语义不变 → 是知识/事实/属性,**不抽**
- 语义丢失"bot 怎么做" → 是行为指令,**可能抽**(再核对白名单 1-6)

## 指代消解

directive 文本里出现代词("他"、"那篇"、"这种情况")时, 必须能从历史对话
**唯一**解析出对象才抽。多义/无法解析 → 不抽。当前消息无代词 → 直接判断。

# 关于历史对话

历史对话**只用来理解当前消息的指代和承接**,不要把历史对话里的话也抽成指令——
只从"当前消息"那一段抽。scope_ref 写解析后的具体名字(如"哥"、"小猫老师"),
不是代词。

# 关于作者归属

输入会标"## 当前消息(作者: <domain>)" — 这是当前发信人。这条 directive 默认是
**他**给 bot 下的指令, 归属人就是他。
- 转述形态("李四说: 你以后每次都...")的 directive: 仍然是当前发信人在转述这条
  指令给 bot, 归属人不要写成李四
- scope_ref 是指令**作用对象**的名字(如"涉及小猫老师时..."里的 scope_ref="小猫老师"),
  跟"谁说的"是两件事, 不要混
- 不要在 directive 文本里加"xxx 说" — directive 是给 LLM 当指令读的, 主语应是"bot 应该如何"

# 同义实体声明 (优先级高于 directive 抽取)

如果当前消息**显式声明两个名字指同一个人/对象** ("X 就是 Y" / "X 即 Y" / "X 跟 Y 是同一人"
"X 也叫 Y" 等), 输出到 aliases 数组而**不**抽成 directive。 这是结构数据, 用来归并
同一对象在 Memory 表里的多份指令, 跟"约束 bot 行为"语义不同。

格式: {"name": "X", "canonical": "Y"}  其中 canonical 是更"正式 / 明确"的那个名字
(例如真实姓名 vs 昵称, 取真实姓名作 canonical)。 不确定哪个更正式时, 任选一个保持一致即可
(只要 N 个名字都指向同一个 canonical)。

# 路由 app_id 单独抽

如果指令是"<某类问题>路由/转发/咨询/艾特给某个 bot",且文本里出现了 cli_xxx 形式
(cli_ 开头, 后跟 32 位十六进制)的 bot app_id, 必须把 app_id 单独抽到 route_app_id 字段,
并在 directive 文本里**删掉**含 app_id 的那部分(例如"...的 app_id 是 cli_xxx" / "(cli_xxx)" / 单独的"cli_xxx"段)。
原因: app_id 是路由结构数据, 不该写进 directive 文本喂给后续模型(会被复述给用户)。

正确示例:
  原话: "iam 网关 / 查 app_id 类问题艾特 tachi, tachi 的 appid 是 cli_7847a145e02d020b9b7dcec8b6391ab6"
  抽出: {"scope_type": "entity", "scope_ref": "tachi",
         "directive": "iam 网关 / 查 app_id 类问题艾特 tachi",
         "route_app_id": "cli_7847a145e02d020b9b7dcec8b6391ab6"}

非路由场景, 或 directive 文本里没有 cli_xxx, 留空字符串 "".

# 关于已有 alive 指令(用 user_msg 里的 ## 已有 alive 指令 段判断当前消息属于哪类)

如果 user_msg 里附了 "## 已有 alive 指令" 段, 把当前消息和那些指令对齐:
- **修正/收紧/补充已有指令**(信号词如"修正前提"、"收紧"、"以后改成"、"再加一条" /
  或语义上是给已有某条指令加约束的): directive 文本必须**写完整新版后果** —
  把已有指令的核心约束 + 当前消息的新约束**合并复述清楚**, 不要只写增量片段。
  scope_type / scope_ref 必须跟被修正的那条**严格一致**(同一对象), 否则会被
  当作新增指令而不是修正, 后续仲裁会出问题。
- **跟已有无关**: 正常抽即可。
- **说的就是已有那条同语义内容**(只是换种说法): 不抽, 避免重复。

注意: 已有指令的 route_app_id / 路由结构字段由代码层处理, 你**不用**在 directive
文本里复述 cli_xxx; 但 scope_ref 必须跟原条对齐才能让代码识别为同一 scope。

输出 JSON:
{
  "directives": [
    {"scope_type": "entity|global", "scope_ref": "<entity 名或空>",
     "directive": "<指令文本, 不含 cli_xxx>",
     "route_app_id": "<cli_xxx 或空串>"}
  ],
  "aliases": [
    {"name": "<别名>", "canonical": "<主名>"}
  ]
}

两个数组都可以为空。 没有任何指令也没有同义声明 → {"directives": [], "aliases": []}。
只输出 JSON, 不要 markdown 包裹。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
# Wave app_id 形态: cli_ + 32 位十六进制
_APP_ID_RE = re.compile(r"\bcli_[0-9a-f]{32}\b", re.IGNORECASE)


def _scrub_app_id(directive: str, route_app_id: str) -> tuple[str, str]:
    """从 directive 文本里抠掉 cli_xxx, 顺带把抠到的值同步到 route_app_id。

    LLM 已被 prompt 约束剥 hash, 但有时还是会写到 directive 里 — 这是结构纪律,
    不能依赖 LLM 自觉, 在落库前做一道兜底正则。

    返 (清洗后的 directive, route_app_id)。 directive 文本里的 hash 全替换为空,
    顺手清掉残留的"的 app_id 是 " / "(空)" / 多余空格 / 末尾标点。
    """
    found = _APP_ID_RE.findall(directive)
    if not found:
        return directive.strip(), route_app_id
    if not route_app_id:
        route_app_id = found[0]
    cleaned = _APP_ID_RE.sub("", directive)
    # 处理常见残留连接词, 不写复杂 grammar — 列举常见拼接形态即可
    for trail in (
        "的 app_id 是", "的 appid 是", "的 app id 是",
        "app_id 是", "appid 是",
        "()", "( )", "(,)", " ,", " ,", ",,", ",,",
    ):
        cleaned = cleaned.replace(trail, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,,。.;;:、")
    return cleaned, route_app_id


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
    """scope_ref 落库前过同义实体表归一到 canonical 主名,
    避免 "小猫老师" / "周婷" 这种同一人不同叫法在 Memory 里产生独立 alive directive。
    没声明同义时 fallback 原值。"""
    if scope_type != "entity" or not scope_ref:
        return ("global", "")
    from helper.memory.alias import resolve_alias
    return ("entity", resolve_alias(scope_ref))


def _format_alive_memories_block() -> str:
    """把所有 alive memory 渲染成一段 prompt 用的清单。

    数量级:实测 dogfood 后期约 8-10 条,每条 < 100 字 ≈ 总 < 500 token。
    超 50 条再考虑按 entity 命中预筛 — 当前不预筛,简单优先。
    """
    with session() as s:
        rows = list(s.execute(
            select(Memory)
            .where(Memory.superseded_at.is_(None))
            .order_by(Memory.id)
        ).scalars())
    if not rows:
        return ""
    lines = ["## 已有 alive 指令(供你判断当前消息是新增/修正/无关)"]
    for m in rows:
        scope_label = f"entity:{m.scope_ref}" if m.scope_type == "entity" else "global"
        directive_text = (m.directive or "").strip().replace("\n", " ")
        lines.append(f"- memory#{m.id} ({scope_label}): {directive_text}")
    return "\n".join(lines)


def _inherit_route_app_id(
    new_route_app_id: str, scope_type: str, scope_ref: str
) -> tuple[str, int | None]:
    """新 memory route_app_id 为空 + 同 scope 有 alive 旧 memory 带 route_app_id →
    自动从旧条继承(返 inherited_from_id 供 ConflictLog summary 引用)。

    动机:LLM 抽"修正前提"型 raw 时,原文里没出现 cli_xxx,LLM 不会无中生有 →
    新 memory route_app_id 为空 → 后续 ROUTE 路径反查找不到 app_id 落空。
    route_app_id 是确定性绑定(同 scope 的 bot app_id 不会因为 owner 修正措辞
    就变),由代码继承比让 LLM 复述更稳。
    """
    if new_route_app_id:
        return new_route_app_id, None
    with session() as s:
        row = s.execute(
            select(Memory.id, Memory.route_app_id)
            .where(Memory.scope_type == scope_type)
            .where(Memory.scope_ref == scope_ref)
            .where(Memory.superseded_at.is_(None))
            .where(Memory.route_app_id != "")
            .order_by(Memory.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return new_route_app_id, None
    return row.route_app_id, row.id


_MEMORY_SEMANTIC_THRESHOLD = 0.85
_MEMORY_EMBEDDING_DIM = 1024
_MEMORY_EMBEDDING_BYTES = _MEMORY_EMBEDDING_DIM * 2  # fp16


def _compute_embedding(text: str) -> bytes:
    """directive 文本算 1024 维 bge-m3 向量, fp16 编码。 失败返 b"" 不抛 —
    embedding 空的 memory 永远不会被语义 fallback 命中, 但精确路径不受影响。"""
    text = (text or "").strip()
    if not text:
        return b""
    try:
        from helper.llm.embed import embed_one
        vec = embed_one(text)
    except Exception as e:  # noqa: BLE001
        log.warning("memory embedding failed: %s", e)
        return b""
    if not vec or len(vec) != _MEMORY_EMBEDDING_DIM:
        return b""
    return struct.pack(f"{len(vec)}e", *vec)


def _decode_embedding(blob: bytes | None) -> list[float] | None:
    if not blob or len(blob) != _MEMORY_EMBEDDING_BYTES:
        return None
    return list(struct.unpack(f"{_MEMORY_EMBEDDING_DIM}e", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


_CONFLICT_JUDGE_SYSTEM = """你是 procedural memory 冲突 judge。给你两条 memory directive (新 vs 旧),
判断它们是否真的冲突 — 即"在相同对象上不能同时成立"。

输出 JSON: {"verdict": "contradicts | refines | none"}

判断标准:
- contradicts: 同对象, 新旧不可同时成立 (如"用 X 称呼" vs "用 Y 称呼", "要详细回答" vs "要简短")
- refines:    同方向具体化或边界补充, 本质相容 (如"涉及 X 时要谨慎" + "涉及 X 的子场景 Y 时要更谨慎")
- none:       完全不在一个话题上, 无关 (如"用牛姐称呼她" vs "她不懂哥哥的想法" — 一个称呼一个属性, 不冲突)

只输出 JSON, 不要 markdown。"""


def _judge_memory_conflict(new_directive: str, old_directive: str) -> str:
    """判断两条 directive 是否真冲突。 失败/超时 fallback 'contradicts' 保守报冲突, 让人裁。

    返回 verdict: contradicts / refines / none。
    """
    user = (
        f"## 新 directive\n{new_directive}\n\n"
        f"## 旧 directive\n{old_directive}\n\n## 输出\nJSON。"
    )
    try:
        reply = run("conflict_judge", system=_CONFLICT_JUDGE_SYSTEM, user=user, temperature=0.0)
    except Exception as e:  # noqa: BLE001
        log.warning("memory conflict judge LLM failed: %s", e)
        return "contradicts"
    data = _parse_json(reply) or {}
    verdict = str(data.get("verdict", "")).lower().strip()
    if verdict not in ("contradicts", "refines", "none"):
        return "contradicts"
    return verdict


def _detect_conflict_target(
    scope_type: str, scope_ref: str, embedding: bytes,
) -> tuple[int | None, str]:
    """检测冲突目标。返回 (旧 memory id 或 None, alias_hint)。

    alias_hint 非空仅在跨 scope 语义相似 fallback 命中时, 形如 "name_a||name_b",
    其中 name_a 是新 memory 的 scope_ref, name_b 是旧 memory 的。 detector.resolve
    会用它回写 entity_alias 表。 精确撞 scope 时 alias_hint 留空。

    多条 alive 共存时取最新一条作为冲突对象;历史脏数据清理留给 memory_audit。
    """
    with session() as s:
        # 1. 精确撞 scope
        row = s.execute(
            select(Memory.id)
            .where(Memory.scope_type == scope_type)
            .where(Memory.scope_ref == scope_ref)
            .where(Memory.superseded_at.is_(None))
            .order_by(Memory.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is not None:
            return row, ""

        # 2. 语义相似 fallback (跨 scope) — embedding 空则跳过
        new_vec = _decode_embedding(embedding)
        if new_vec is None:
            return None, ""
        candidates = s.execute(
            select(Memory.id, Memory.scope_ref, Memory.embedding)
            .where(Memory.superseded_at.is_(None))
            .where(Memory.scope_type == "entity")  # 同义只对 entity 层有意义
        ).all()
        best_id: int | None = None
        best_ref = ""
        best_score = 0.0
        for cand_id, cand_ref, cand_blob in candidates:
            if cand_ref == scope_ref:
                continue  # 同 scope 已被精确路径处理
            cand_vec = _decode_embedding(cand_blob)
            if cand_vec is None:
                continue
            score = _cosine(new_vec, cand_vec)
            if score >= _MEMORY_SEMANTIC_THRESHOLD and score > best_score:
                best_score = score
                best_id = cand_id
                best_ref = cand_ref
        if best_id is None:
            return None, ""
        # scope_ref 在前 (新), 旧的在后 — detector 解析顺序固定
        return best_id, f"{scope_ref}||{best_ref}"


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

    author_tag = f"(作者: {author})" if author else ""
    current_header = f"## 当前消息{author_tag}(只从这条抽指令)"
    alive_block = _format_alive_memories_block()
    parts = []
    if alive_block:
        parts.append(alive_block)
    if chat_context:
        parts.append(chat_context)
    parts.append(f"{current_header}\n{text}")
    user_msg = "\n\n".join(parts)

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

    # 同义声明 — 不进 Memory, 直接落 entity_alias 表。
    # 优先于 directive 抽取处理:后续 directive 的 scope_ref 解析会读这张表, 同消息内
    # "X 就是 Y" + 关于 X 的 directive 共存时, X 能立刻归一到 Y。
    aliases = data.get("aliases") or []
    if isinstance(aliases, list):
        from helper.memory.alias import add_alias
        for a in aliases:
            if not isinstance(a, dict):
                continue
            nm = str(a.get("name", "")).strip()
            canon = str(a.get("canonical", "")).strip()
            if nm and canon and nm != canon:
                add_alias(nm, canon, source="manual")

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
        # 兜底: LLM 万一漏剥, 用正则把 cli_xxx 从 directive 文本里再抠一遍。
        # cli_ 后面 32 位十六进制是 Wave app_id 的固定形态。
        route_app_id = str(it.get("route_app_id", "")).strip()
        directive, route_app_id = _scrub_app_id(directive, route_app_id)
        # 继承同 scope 旧条的 route_app_id (raw 文本里无 cli_xxx 时不丢路由)
        route_app_id, inherited_from = _inherit_route_app_id(
            route_app_id, scope_type, scope_ref
        )

        embedding = _compute_embedding(directive)
        old_id, alias_hint = _detect_conflict_target(scope_type, scope_ref, embedding)

        # 同 scope / 跨 scope 同义撞之后, 在落库前过一道 LLM judge:
        # 排除"语义无关只是 scope 撞"的假冲突 (如同 global scope 下"门禁同步 2 小时"
        # vs "回答不要和稀泥")。 judge=none/refines → 不开 ConflictLog, 新旧共存。
        # 失败 fallback contradicts (保守报冲突让人裁)。
        suppress_conflict = False
        if old_id is not None:
            old_directive = ""
            with session() as _s:
                _old = _s.get(Memory, old_id)
                if _old is not None:
                    old_directive = _old.directive or ""
            verdict = _judge_memory_conflict(directive, old_directive)
            if verdict != "contradicts":
                log.info(
                    "memory conflict suppressed raw_id=%s old=%d verdict=%s",
                    raw_id, old_id, verdict,
                )
                suppress_conflict = True

        with session() as s:
            mem = Memory(
                scope_type=scope_type,
                scope_ref=scope_ref,
                directive=directive,
                route_app_id=route_app_id,
                source_raw_id=raw_id,
                author_domain=author,
                embedding=embedding,
            )
            s.add(mem)
            s.flush()
            new_id = mem.id

            if old_id is not None and not suppress_conflict:
                # 冲突走 inbox 周报裁决,不直接覆盖
                if alias_hint:
                    # 跨 scope 语义撞 — 提示 owner 三选项, resolve 时回写 alias 表
                    summary = (
                        f"[同义疑似] 新 memory#{new_id} (scope={scope_ref}) 与 "
                        f"memory#{old_id} 语义高度相似 — 是不是同一对象? "
                        f"采纳=确认同义+用新覆盖旧 / 保留=否决同义判断 / 都留=并存"
                    )
                else:
                    summary = (
                        f"新指令(memory#{new_id}: {directive!r})与已有 "
                        f"memory#{old_id} 在同 scope({scope_type}:{scope_ref}) 冲突"
                    )
                if inherited_from is not None:
                    summary += f"; route_app_id 已从 memory#{inherited_from} 继承"
                s.add(ConflictLog(
                    raw_id=raw_id,
                    target_type="memory",
                    target_slug=str(old_id),  # memory 没有 slug,用 id
                    summary=summary,
                    severity="medium",
                    alias_hint=alias_hint,
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
