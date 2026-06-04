"""L1 抽取 — 把毛坯输入(聊天 / 文档 / 任意文本)抽成 0..N 条**知识原子**。

二分 schema:
- section:  {title, body, topics[], entities[]}  ← 语义独立单元,原文保留
- decision: {scene, signals[], tradeoffs[], choice, rationale,
             source_raw_ids?, primary_raw_id?, decision_speaker?, rationale_speaker?}
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from helper.llm import run

log = logging.getLogger(__name__)

ALLOWED_TYPES = {"section", "decision"}


@dataclass
class L1Item:
    """单条知识原子(L1Result 表的一行)。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class L1Output:
    """一次 L1 抽取的整体结果。"""

    items: list[L1Item] = field(default_factory=list)
    raw_text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


SYSTEM_PROMPT = """你是知识切片器。把输入文本切成「语义独立单元」,每条归到 section 或 decision。

【独立单元怎么判 — 核心原则】
- 单独拿出来能自洽表达一件完整的事
- 删掉它,其他段的理解不受影响 — 互相独立
- 表格、清单、映射、对照 — 作为整体单元保留,**绝不拆**
  (集合性是语义的一部分,把"9 类员工属性"拆成 9 条会彻底丢失"这是一份完整清单"的事实)
- 连贯的论证、推导、举例链 — 作为整体单元保留,不拆

【两类原子】
1. section(主力)— 任何独立的语义单元,长短不限
   字段:
     title:    一句话概括这段在讲什么(原文有小标题就用原文标题)
     body:     **原文完整保留**,不改写、不总结、不拆条、不省略
     topics:   3-5 个关键词,用于检索召回(如 ["员工属性", "LML", "回流"])
     entities: 段内出现的关键实体名(术语 / 系统 / 人 / 公司),0-N 个
   长度无下限 — 一句话事实 / 身份映射 / 黑话定义 / 单条规则也是 section:
     - "周婷就是小猫老师" → section(身份映射, body 就是原文这一句)
     - "记住徐叶佳不吃鱼" → section(个人偏好/口径, body 原文)
     - "Helper 生产端口是 8009" → section(系统配置事实)
     - "小猫老师被哥评价为'螃蟹'" → section(人际评价)
   只要是独立可召回的知识点就 section,**绝不因为太短而丢弃**。

2. decision — 仅当一段明确是「为什么这么做」(场景→选项→选择→理由)时
   字段(同传统 decision):
     scene:      决策场景
     signals:    支撑判断的信号(数组)
     tradeoffs:  考虑过的取舍(数组)
     choice:     最终选择
     rationale:  理由
     source_raw_ids / primary_raw_id / decision_speaker / rationale_speaker(群聊场景填,否则可空)
   注意:decision 不替代 section — 同一段如果既包含决策又包含其他陈述内容,
   decision 抽出来的同时,**原段落仍以 section 形式保留**(让"是什么"和"为什么"都在)。

【参考值,不是硬约束】
- 一段 ~1000 字是常态。超过时回头看一下是不是合并了两件事
- 但语义上确属一件事(大表 / 长清单 / 完整论证)就保留整体,不强切
- 文档的章节结构是「线索」(作者认为这里是边界),通常跟语义边界重合 —
  但不要为了「切在章节边界」而把一件事拆成两段,
  也不要为了「不切章节」而把两件事合一段

【输入形态】
A) 一段独立文本(文档 / 长消息) — 直接切。
B) 群聊 @bot 触发,带 ## 上下文 + ## 主消息 两块 —
   主消息很短,需要从上下文补齐 scene/signals/rationale。
   群聊场景下 decision 必填 source_raw_ids(主 + 上下文)+ primary_raw_id + decision_speaker。

【用户附加说明】
如果输入末尾有「## 用户附加说明」段,这是用户随文档发来的取舍指令(例如
"只读 xxx 部分,其他章节过时了不抽"、"忽略 yyy 段")。按指令执行:
- 用户说"只读 X" → 只对 X 相关段落产出 section / decision,其他段落整段不抽
- 用户说"忽略 Y" → Y 段不抽,其他正常抽
- 指令模糊或与文档结构冲突 → 按文档结构正常抽,不为难
用户附加说明本身**不抽成原子**,只影响切片范围。

【硬性要求】
- 只切文本里直接出现的内容,不编造
- body 必须从原文逐字抄录(可省略明显的格式噪音如多余空行,但不允许改写或概括)
- 输出 JSON 数组。每元素 {"type": "section" | "decision", ...该 type 字段}
- 直接输出 JSON,不要解释、不要代码块。空文本 → []

【JSON 转义 — 极重要】
body 字段是 JSON 字符串值,原文里出现的所有特殊字符必须按 JSON 规则转义:
- 双引号 " → \\"
- 反斜杠 \\ → \\\\
- 换行 → \\n,制表符 → \\t,回车 → \\r
原文如 "模拟器"、"加白" 这种带双引号的内容,在 body 里必须写成 \\"模拟器\\"、\\"加白\\"。
不转义会直接让整段 JSON 解析失败,这条原子整条丢失。
检查输出前自己默念一遍:每个 body 字段是否所有 " 都已经写成 \\"。"""


def _format_context_block(context: list[dict] | None) -> str:
    """把上下文行渲染成 prompt 用的 [raw#ID @speaker] text 列表。

    context 期望是按时间正序的 [{raw_id, speaker, text, ts}]。
    """
    if not context:
        return ""
    lines = []
    for c in context:
        raw_id = c.get("raw_id", "?")
        speaker = c.get("speaker") or "user"
        ts = c.get("ts", "")
        text = (c.get("text") or "").strip().replace("\n", " ")
        prefix = f"[raw#{raw_id} @{speaker}"
        if ts:
            prefix += f" {ts}"
        prefix += "]"
        lines.append(f"{prefix} {text}")
    return "\n".join(lines)


def _build_user_prompt(
    raw_text: str,
    *,
    context: list[dict] | None,
    primary_raw_id: int | None,
    primary_speaker: str,
    user_instruction: str = "",
) -> str:
    """有 context 时拼 ## 上下文 + ## 主消息 两块;无 context 时直接传原文。
    user_instruction 非空时,作为 ## 用户附加说明 段拼到末尾。
    """
    if context:
        ctx_block = _format_context_block(context)
        primary_prefix = "[主消息"
        if primary_raw_id is not None:
            primary_prefix += f" raw#{primary_raw_id}"
        if primary_speaker:
            primary_prefix += f" @{primary_speaker}"
        primary_prefix += "]"
        body = (
            "## 上下文(同对话最近窗口,按时间正序)\n"
            f"{ctx_block}\n\n"
            "## 主消息(本次 @bot 触发抽取)\n"
            f"{primary_prefix} {raw_text.strip()}"
        )
    else:
        body = raw_text

    instr = (user_instruction or "").strip()
    if instr:
        body = f"{body}\n\n## 用户附加说明\n{instr}"
    return body


# 长文档按 H2 章节切片的字符阈值。Sonnet 4.6 单次 output 上限 64K tokens(中文
# ~1.5 字符/token),广抽取 ~5x 包装系数,12K 字符可能产 ~5K tokens 输出 — 留足
# 16K max_tokens 余量。超过这个阈值就切,避免单次输出被截。
LONG_DOC_THRESHOLD = 12000


def _chunk_by_h2(text: str) -> list[str]:
    """按 ^## 行边界切 — 每块自带一个 H2 标题。

    切片前缀:H1(如果有)+ 当前 H2 段落。这样 LLM 看每个 chunk 时仍知道这是哪
    篇文档的哪一章。
    没 H2 → 按字符数硬切(尽量在段落边界),保证每块 ≤ LONG_DOC_THRESHOLD。
    """
    lines = text.split("\n")
    h1 = ""
    for ln in lines:
        if ln.startswith("# ") and not ln.startswith("## "):
            h1 = ln.strip()
            break

    chunks: list[list[str]] = []
    current: list[str] = []
    has_h2 = False
    for ln in lines:
        if ln.startswith("## "):
            has_h2 = True
            if current:
                chunks.append(current)
            current = [ln]
        else:
            current.append(ln)
    if current:
        chunks.append(current)

    if not has_h2:
        return _chunk_by_size(text, LONG_DOC_THRESHOLD)

    out: list[str] = []
    for ch in chunks:
        body = "\n".join(ch).strip()
        if not body:
            continue
        # 第一个 chunk 经常只是 H1 + 空行(没有实际段落),跳过
        if body == h1 or (h1 and body.replace(h1, "").strip() == ""):
            continue
        prefix = (h1 + "\n\n") if (h1 and not body.startswith(h1)) else ""
        full = prefix + body
        # 单个 H2 段也可能超阈值 — 再按字符切
        if len(full) > LONG_DOC_THRESHOLD:
            for sub in _chunk_by_size(full, LONG_DOC_THRESHOLD):
                out.append(sub)
        else:
            out.append(full)
    return out


def _chunk_by_size(text: str, max_chars: int) -> list[str]:
    """段落边界切 — 优先 \\n\\n,然后 \\n,最后强切。"""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut <= max_chars // 2:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut <= max_chars // 2:
            cut = max_chars
        out.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        out.append(remaining.strip())
    return out


def _structure_one_chunk(user_prompt: str) -> tuple[list[L1Item], str]:
    """单次 LLM 调用 — 返回 (items, error)。"""
    try:
        reply = run("l1_structure", system=SYSTEM_PROMPT, user=user_prompt, temperature=0)
    except Exception as e:  # noqa: BLE001
        return [], f"LLM call failed: {type(e).__name__}: {e}"

    arr = _parse_json_array(reply)
    if arr is None:
        return [], f"bad JSON from LLM, first 200 chars: {reply[:200]!r}"

    items: list[L1Item] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", "")).strip().lower()
        if t not in ALLOWED_TYPES:
            log.debug("l1: unknown type %r dropped", t)
            continue
        payload = {k: v for k, v in item.items() if k != "type"}
        items.append(L1Item(type=t, payload=payload))
    return items, ""


def structure(
    raw_text: str,
    *,
    context: list[dict] | None = None,
    primary_raw_id: int | None = None,
    primary_speaker: str = "",
    user_instruction: str = "",
) -> L1Output:
    """L1 入口。LLM/解析失败时返回 error 字段非空,不抛。

    群聊 @bot 路径传 context = [{raw_id, speaker, text, ts}, ...] 把上下文窗口
    一并喂给 LLM,让它从上下文补齐主消息缺失的 scene/signals/rationale。

    长文档(纯文本 > LONG_DOC_THRESHOLD 字符)按 H2 章节切片,每片独立抽取后合并。
    群聊路径(有 context)不切 — 主消息肯定短,且切片会破坏上下文承接。

    user_instruction: 用户随文档发来的取舍指令(如"只读 xxx 部分")。
    长文档分片时,每片都拼上同一份 instruction(否则后续片不知道用户要什么)。
    """
    if not (raw_text or "").strip():
        return L1Output(raw_text=raw_text)

    # 群聊路径或短文本 — 单次抽取
    if context or len(raw_text) <= LONG_DOC_THRESHOLD:
        user_prompt = _build_user_prompt(
            raw_text,
            context=context,
            primary_raw_id=primary_raw_id,
            primary_speaker=primary_speaker,
            user_instruction=user_instruction,
        )
        items, err = _structure_one_chunk(user_prompt)
        if err and not items:
            return L1Output(raw_text=raw_text, error=err)
        return L1Output(items=items, raw_text=raw_text)

    # 长文档 — 切片后逐片抽取(每片都带同一份用户指令)
    chunks = _chunk_by_h2(raw_text)
    log.info(
        "l1: long doc %d chars → %d chunks (avg %d chars)",
        len(raw_text), len(chunks), len(raw_text) // max(1, len(chunks)),
    )
    all_items: list[L1Item] = []
    errors: list[str] = []
    for i, chunk in enumerate(chunks):
        chunk_prompt = _build_user_prompt(
            chunk, context=None, primary_raw_id=None, primary_speaker="",
            user_instruction=user_instruction,
        )
        items, err = _structure_one_chunk(chunk_prompt)
        log.info("l1 chunk %d/%d: %d items, err=%s", i + 1, len(chunks), len(items), err or "ok")
        all_items.extend(items)
        if err:
            errors.append(f"chunk{i}: {err}")
    # 至少 1 个 chunk 成功就当整体成功;全失败才返 error
    if not all_items and errors:
        return L1Output(raw_text=raw_text, error="; ".join(errors))
    return L1Output(items=all_items, raw_text=raw_text)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _parse_json_array(text: str) -> list | None:
    """容忍 ```json``` 包裹 + 数组未闭合(尾部截断)+ 单个 obj 损坏。

    流程:
    1. 剥 ```json``` 围栏 / 前后多余文字
    2. 先尝试整段 json.loads — 成功最好(99% 情况)
    3. 失败则降级到逐对象切:从首个 `[` 后开始,按大括号深度扫,把每个顶层
       `{...}` 切出来独立 json.loads。坏的丢 warning 跳过,好的累积返回。
       这样 LLM 输出尾部被 max_tokens 截断 / 中间一条 obj 字符串没转义,都能尽量
       挽救;最差返回 [],而不是整批 None 丢光。

    返回 None 仅表示"完全识别不出数组形态"(连首 `[` 都没找到)。
    空数组 [] 合法,语义是"LLM 看完没东西可抽"。
    """
    text = (text or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("[")
    if start == -1:
        return None

    # 尝试 1: 整段 parse
    end = text.rfind("]")
    if end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 尝试 2: 逐对象切 — 容错截断 / 局部坏数据
    return _salvage_objects(text, start)


def _salvage_objects(text: str, array_start: int) -> list:
    """从 `[` 之后扫,按大括号深度提取每个顶层 `{...}`,逐个 json.loads。

    跳过字符串字面量内的大括号(包括转义);未闭合的尾部丢弃。
    """
    items: list = []
    i = array_start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace() or ch == ",":
            i += 1
            continue
        if ch == "]":
            break
        if ch != "{":
            i += 1
            continue
        # 找匹配的 `}` — 跟踪字符串/转义
        depth = 0
        j = i
        in_str = False
        esc = False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if depth != 0 or j >= n:
            # 尾部对象未闭合(被 max_tokens 截了),丢弃
            log.info("l1: trailing object unclosed at offset %d, dropped", i)
            break
        chunk = text[i : j + 1]
        obj = _try_load_object(chunk)
        if obj is not None:
            items.append(obj)
        else:
            # 尝试 1 失败 → 修复字符串值里的裸双引号再试一次
            repaired = _fix_unescaped_quotes(chunk)
            obj = _try_load_object(repaired)
            if obj is not None:
                log.info("l1: object at offset %d salvaged after quote fix", i)
                items.append(obj)
            else:
                log.warning(
                    "l1: bad object at offset %d, even after quote fix, skipped: %.120s",
                    i, chunk.replace("\n", " "),
                )
        i = j + 1
    return items


def _try_load_object(chunk: str) -> dict | None:
    """parse 一个 {...} chunk,成功返 dict,失败返 None。"""
    try:
        obj = json.loads(chunk)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    return None


def _fix_unescaped_quotes(chunk: str) -> str:
    """修复 LLM 在字符串值内忘记转义的裸双引号。

    JSON 字符串值的边界规则: 进入字符串后(在某个 `"` 之后),下一个未转义的 `"`
    才是结束符 — 但 LLM 经常把 body 里原文的 `"加白"` 直接写出来。
    本函数用启发式: 在字符串内时, 如果遇到一个 `"`, 看它后面跳过空白后是否是
    `,` / `}` / `]` 或 `:` — 如果不是,认为它是字符串内的字面量,转义成 `\\"`。

    精度足够覆盖 v2 prompt 产出的对象(扁平结构, body 是字符串值, 没有嵌套对象在
    string 之外); 复杂嵌套场景这个启发式可能误伤, 容忍度内可接受。
    """
    out: list[str] = []
    n = len(chunk)
    i = 0
    in_str = False
    esc = False
    while i < n:
        c = chunk[i]
        if not in_str:
            out.append(c)
            if c == '"':
                in_str = True
            i += 1
            continue
        # 在字符串内
        if esc:
            out.append(c)
            esc = False
            i += 1
            continue
        if c == "\\":
            out.append(c)
            esc = True
            i += 1
            continue
        if c != '"':
            out.append(c)
            i += 1
            continue
        # c == '"' 且非转义 — 是不是真终止符?
        k = i + 1
        while k < n and chunk[k] in " \t\r\n":
            k += 1
        if k < n and chunk[k] in ",}]:":
            # 真终止
            out.append('"')
            in_str = False
            i += 1
        else:
            # 假终止(字符串内的字面引号)— 转义
            out.append('\\"')
            i += 1
    return "".join(out)
