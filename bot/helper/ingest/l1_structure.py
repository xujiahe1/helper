"""L1 抽取 — 把毛坯输入(聊天 / 文档 / 任意文本)抽成 0..N 条**知识原子**。

支持两套 prompt(v1 / v2),由 helper.config.get_settings().l1_prompt_version 决定:

v1(legacy)— 5 类细分原子,LLM 自己判类型:
- decision: {scene, signals[], tradeoffs[], choice, rationale}
- fact:     {subject, predicate, object, scope}
- case:     {scene, what_happened, outcome, referenced_spec?}
- concept:  {name, entity_type, description}
- relation: {entity_a, relation, entity_b, description?}

v2(default)— 二分:section 主力 + decision 兜叙事:
- section:  {title, body, topics[], entities[]}  ← 语义独立单元,原文保留
- decision: 同 v1
v2 设计动机:实测发现 fact / concept / relation 边界对 LLM 不稳定,
            把成体系的内容(表格 / 清单 / 映射)拆碎反而丢集合性。

两套 prompt 共存,SECTION 与 5 类合法值并行接受。新 raw 默认走 v2。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from helper.llm import run

log = logging.getLogger(__name__)

ALLOWED_TYPES = {"decision", "fact", "case", "concept", "relation", "section"}


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


SYSTEM_PROMPT_V2 = """你是知识切片器。把输入文本切成「语义独立单元」,每条归到 section 或 decision。

【独立单元怎么判 — 核心原则】
- 单独拿出来能自洽表达一件完整的事
- 删掉它,其他段的理解不受影响 — 互相独立
- 表格、清单、映射、对照 — 作为整体单元保留,**绝不拆**
  (集合性是语义的一部分,把"9 类员工属性"拆成 9 条会彻底丢失"这是一份完整清单"的事实)
- 连贯的论证、推导、举例链 — 作为整体单元保留,不拆

【两类原子】
1. section(主力)— 普通的语义段落
   字段:
     title:    一句话概括这段在讲什么(原文有小标题就用原文标题)
     body:     **原文完整保留**,不改写、不总结、不拆条、不省略
     topics:   3-5 个关键词,用于检索召回(如 ["员工属性", "LML", "回流"])
     entities: 段内出现的关键实体名(术语 / 系统 / 人 / 公司),0-N 个

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
- 直接输出 JSON,不要解释、不要代码块。空文本 → []"""


SYSTEM_PROMPT = """你是知识原子抽取器。给你一段任意文本(可能是聊天片段,也可能是规章文档),
你要从中抽出**所有**值得沉淀的"知识原子",每条原子归到下列 5 类之一。

输入可能是两种形态:
A) 一段独立文本 — 直接抽。
B) 群聊 @bot 触发,带"## 上下文" + "## 主消息" 两块:
   - 主消息是被 @bot 的那条,通常很短(如"@bot prd模板风险章节放前面吧")
   - 上下文是同一对话最近 N 分钟的消息(每行带 [raw#ID @speaker] 前缀)
   - 抽取时把主消息看作"决策时刻",**上下文是支撑这个决策的素材** —
     主消息里没说全的 scene / signals / rationale,**应当从上下文补齐**。
   - 同时记录信息源:见下方 source_raw_ids / decision_speaker / rationale_speaker。

类型与字段:
1. decision — 某场景下做出的判断。
   字段: scene / signals(数组) / tradeoffs(数组) / choice / rationale
   附加(群聊场景填,独立文本可空):
     source_raw_ids: [raw_id, ...]  本条 decision 引用了哪些 raw(主 + 上下文)
     primary_raw_id: int             主消息 raw_id(决策本身这一条)
     decision_speaker: str           主消息说话人(domain / union_id / id 都可)
     rationale_speaker: str          rationale 主要来自谁(可能 ≠ decision_speaker)
2. fact — 静态可验证事实(主谓宾)。
   字段: subject / predicate / object / scope(可空)
   附加: source_raw_ids(可选)
3. case — 发生过的案例 / 反例。
   字段: scene / what_happened / outcome / referenced_spec(可空)
   附加: source_raw_ids(可选)
4. concept — 术语 / 概念 / 实体定义。
   字段: name / entity_type / description
5. relation — 实体间关系。
   字段: entity_a / relation / entity_b / description(可空)

输出 JSON 数组,每个元素 {"type": "<五者之一>", ...该 type 的字段}。

硬性要求:
- 只抽文本里直接出现 / 直接可推导的内容,不编造。
- **宁可多抽不要漏抽** — 文档/对话里出现的每个独立概念、关系、事实、案例、决策,
  都单独成一条原子,不要为了简洁合并相似条目。一篇 8K 字文档抽 30+ 条原子是常态,
  不是异常。如果你犹豫"这条是不是不重要",倾向于抽出来。
- 抽多少条由文本含量决定:0 / 1 / 几十甚至上百都可,不要用类型预设条数。
- 群聊场景下,**主消息是决策核心**,如果上下文里也有独立的判断/事实/反例(不是为
  主消息服务的素材),也分别抽出来 — 一次抽完所有原子。
- 同一原子重复提及只抽一次,但**字面相似但语义不同**(如同一术语在不同章节有不同
  侧重)要分别抽。
- 群聊 decision 的 source_raw_ids 要把"被引用作 signal/rationale 的上下文 raw_id"
  也列出来,不能只填主消息。
- 直接输出 JSON 数组,不要解释、不要代码块。空 → []。"""


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


def _resolve_system_prompt(version: str | None) -> str:
    """version=None 时读 settings.l1_prompt_version。未知值回落 v1 + warning。"""
    if version is None:
        try:
            from helper.config import get_settings
            version = get_settings().l1_prompt_version
        except Exception:  # noqa: BLE001
            version = "v1"
    if version == "v2":
        return SYSTEM_PROMPT_V2
    if version != "v1":
        log.warning("unknown l1_prompt_version=%r, falling back to v1", version)
    return SYSTEM_PROMPT


def _structure_one_chunk(
    user_prompt: str,
    *,
    prompt_version: str | None = None,
) -> tuple[list[L1Item], str]:
    """单次 LLM 调用 — 返回 (items, error)。"""
    system = _resolve_system_prompt(prompt_version)
    try:
        reply = run("l1_structure", system=system, user=user_prompt, temperature=0)
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
    prompt_version: str | None = None,
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
        items, err = _structure_one_chunk(user_prompt, prompt_version=prompt_version)
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
        items, err = _structure_one_chunk(chunk_prompt, prompt_version=prompt_version)
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
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                items.append(obj)
            else:
                log.info("l1: object at offset %d not a dict (%s), skipped", i, type(obj).__name__)
        except json.JSONDecodeError as e:
            log.warning(
                "l1: bad object at offset %d (%s), skipped: %.120s",
                i, e.msg, chunk.replace("\n", " "),
            )
        i = j + 1
    return items
