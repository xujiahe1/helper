"""批量文档 ingest 实现。

切片策略:
  - 按 markdown heading(##/###) 优先切;否则按段落(双换行)切
  - 单片上限 ≤ 1500 字符;超长强制按句号拆
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from helper.ingest import process_raw
from helper.llm import run
from helper.storage import raw_store, session

log = logging.getLogger(__name__)


MAX_UNIT = 1500
MIN_UNIT = 30


@dataclass
class IngestResult:
    file: str
    units_total: int = 0
    units_with_decision: int = 0
    raw_ids: list[int] = None  # type: ignore[assignment]


_HEADING_RE = re.compile(r"^#{2,3}\s+", re.MULTILINE)


def split_into_units(text: str) -> list[str]:
    """切片。返回每片的纯文本。"""
    text = text.strip()
    if not text:
        return []

    # 按 heading 切
    parts: list[str]
    if _HEADING_RE.search(text):
        parts = re.split(r"(?=^#{2,3}\s+)", text, flags=re.MULTILINE)
    else:
        parts = re.split(r"\n\s*\n", text)

    out: list[str] = []
    for p in parts:
        p = p.strip()
        if len(p) < MIN_UNIT:
            continue
        if len(p) <= MAX_UNIT:
            out.append(p)
            continue
        # 超长 → 按句拆
        sents = re.split(r"(?<=[。!?\.\!\?])\s+", p)
        buf = ""
        for s in sents:
            if len(buf) + len(s) > MAX_UNIT and buf:
                out.append(buf.strip())
                buf = s
            else:
                buf += s
        if buf.strip():
            out.append(buf.strip())
    return out


DECISION_DETECT_PROMPT = """这段文本里是否包含**决策性判断**(某人选了 X 不选 Y,或给出了一条经验规则)?

输出 JSON: {"is_decision": true/false, "summary": "若 true 给一句话决策摘要,否则空串"}

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
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _detect_decision(unit: str) -> tuple[bool, str]:
    """走 bulk_extract(qwen flash)— 简单分类够用。"""
    try:
        reply = run("bulk_extract", system=DECISION_DETECT_PROMPT, user=unit, temperature=0)
    except Exception as e:  # noqa: BLE001
        log.warning("decision detect failed: %s", e)
        return False, ""
    data = _parse_json(reply) or {}
    return bool(data.get("is_decision", False)), str(data.get("summary", ""))


def ingest_text_units(
    units: list[str],
    *,
    source_type: str = "doc_batch",
    source_ref: str = "",
    author: str = "",
    run_l1: bool = True,
) -> IngestResult:
    """批量入库。每片 LLM 检测是否含决策 → 入 raw_inputs → 跑 L1。

    严格串行 — 服务器内存不够并发。
    """
    res = IngestResult(file=source_ref or source_type, raw_ids=[])
    res.units_total = len(units)
    for unit in units:
        is_dec, _summary = _detect_decision(unit)
        if not is_dec:
            continue
        res.units_with_decision += 1
        with session() as s:
            row = raw_store.append(
                s,
                source_type=source_type,
                source_ref=source_ref,
                content_text=unit,
                author_domain=author,
            )
            raw_id = row.id
        if run_l1:
            try:
                process_raw(raw_id)
            except Exception:  # noqa: BLE001
                log.exception("batch L1 raw#%d failed", raw_id)
        res.raw_ids.append(raw_id)
    return res


def ingest_file(path: str | Path, *, author: str = "", run_l1: bool = True) -> IngestResult:
    """读单个 .md / .txt / .json,切片 → ingest。"""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    if p.suffix.lower() == ".json":
        # 假设 dump 是 list[str] 或 list[{text: str}]
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            texts = [
                d if isinstance(d, str) else str(d.get("text", "")) for d in data if d
            ]
        else:
            texts = [str(data)]
        units: list[str] = []
        for t in texts:
            units.extend(split_into_units(t))
    else:
        units = split_into_units(p.read_text(encoding="utf-8"))
    return ingest_text_units(
        units,
        source_type="doc_batch",
        source_ref=str(p),
        author=author,
        run_l1=run_l1,
    )
