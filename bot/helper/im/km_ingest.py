"""KM 文档接入 — @bot 消息里检出 KM 链接 → 整篇拉成 raw_input → 走 sink.process_raw。

设计要点:
- 整篇文档不分块,让 L1 LLM 自己从全文里抽 0..N 条多 type 知识原子
- 仅支持 document(协同)/ markdown / spreadsheet 三类,其它类型回告"不支持"
- 幂等: (source_type='km_doc', source_ref=enc_id[#sheet_id]) 已存在就跳过(不重新拉)
- 拉文档失败 / 不支持类型 — 不入 raw,但返回结果让上层回告用户

不在这里调 schedule_l1: 调度由 caller 决定(webhook 路径要排队走 llm_slot)。
本模块只负责"识别 + 拉取 + 写 raw_input",返回 raw_id 给上层串后续。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_

from helper.im import km_client
from helper.im.km_client import KMAPIError
from helper.storage import session
from helper.storage.models import RawInput
from helper.storage.raw_store import append as raw_append

log = logging.getLogger(__name__)


# 在群聊 @bot 消息里识别 KM 链接(完整 URL,带 https 前缀)。
# 文本里粘贴的 KM 链接通常以空白 / 标点结尾,容忍 query / fragment。
_KM_URL_RE = re.compile(
    r"https?://km\.mihoyo\.com/(?:m/)?doc/[A-Za-z0-9?=&#_\-/]+",
)


@dataclass
class KMIngestResult:
    """单个 KM 链接的处理结果。

    status:
      - ok           : 成功拉取并写入 raw_input(可能是新行,也可能是已存在的旧行)
      - skipped      : 已经在 raw_inputs 里,跳过重新拉
      - no_permission: 应用对该文档没有可见性(retcode=10401305) — 让用户授权后重发
      - unsupported  : 文档类型不支持(spreadsheet 没传 sheetId / smart_spreadsheet / 文件 / 视频 等)
      - error        : 其它拉取失败(限流 / 网络 / KM 内部错),message 里有原因
    """

    url: str
    enc_id: str
    sheet_id: str | None
    status: str
    raw_id: int = 0
    title: str = ""
    doc_type: str = ""
    message: str = ""


# ---------- URL 检测 ----------

def find_km_urls(text: str) -> list[str]:
    """从消息正文里抠出所有 KM 链接(去重保序)。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _KM_URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:)】])'\"")
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


# ---------- 单篇拉取 ----------

def _source_ref(enc_id: str, sheet_id: str | None) -> str:
    return f"{enc_id}#{sheet_id}" if sheet_id else enc_id


def _existing_raw(enc_id: str, sheet_id: str | None) -> RawInput | None:
    ref = _source_ref(enc_id, sheet_id)
    with session() as s:
        row = (
            s.query(RawInput)
            .filter(and_(RawInput.source_type == "km_doc", RawInput.source_ref == ref))
            .order_by(RawInput.id.desc())
            .first()
        )
        if row is not None:
            s.expunge(row)
        return row


def _spreadsheet_to_text(values: list[list[Any]], *, max_rows: int = 500) -> str:
    """list[list] → 简单 markdown 表格(空行截断 / 每行 cells join 制表符)。

    超过 max_rows 截断,末尾标注。绝大多数业务表格几十到几百行。
    """
    if not values:
        return ""
    truncated = False
    rows = values
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
    lines = []
    for row in rows:
        cells = ["" if c is None else str(c) for c in row]
        if any(c.strip() for c in cells):
            lines.append("\t".join(cells))
    if truncated:
        lines.append(f"\n[…后续 {len(values) - max_rows} 行已截断]")
    return "\n".join(lines)


def _fetch_doc_text(enc_id: str, sheet_id: str | None) -> tuple[str, str, str, str]:
    """拉文档 → (status, doc_type, title, content_text)。

    status:
      ok            : 拉到正文
      no_permission : 应用对文档无权限(retcode=10401305)
      unsupported   : 类型不支持(retcode=10401307)
      error         : 其它 KM 错误(限流 / 网络 / 内部)
    """
    try:
        info = km_client.get_doc_detail(enc_id)
    except KMAPIError as e:
        # 应用对文档无可见性 → 让用户去授权
        if e.retcode == 10401305:
            return "no_permission", "", "", ""
        # 不支持的文档类型 → 上层用专门 status 回告
        if e.retcode == 10401307:
            return "unsupported", "", "", str(e.message or "")
        return "error", "", "", f"retcode={e.retcode} {e.message}"

    title = (info.get("title") or "").strip()
    doc_type = (info.get("doc_type") or "").lower()

    if doc_type in {"document", "markdown"}:
        body = (info.get("content") or "").strip()
        text = f"# {title}\n\n{body}" if title else body
        return "ok", doc_type, title, text

    if doc_type == "spreadsheet":
        if not sheet_id:
            # 表格类必须带 sheetId(链接里的 ?sheetId=xxx);分享时通常在,但用户也可能漏
            return "unsupported", doc_type, title, "spreadsheet 链接缺少 sheetId,无法定位 sheet"
        try:
            values = km_client.get_spreadsheet_range(enc_id, sheet_id)
        except KMAPIError as e:
            return "error", doc_type, title, f"retcode={e.retcode} {e.message}"
        body = _spreadsheet_to_text(values)
        if not body:
            return "error", doc_type, title, "spreadsheet 空数据 / 取值失败"
        text = f"# {title}\n\n{body}" if title else body
        return "ok", doc_type, title, text

    # smart_spreadsheet / file / video / whiteboard / mind_map / unravel ...
    return "unsupported", doc_type, title, ""


def ingest_one(
    url: str,
    *,
    sender_domain: str = "",
    chat_id: str = "",
    parent_message_id: str = "",
) -> KMIngestResult:
    """处理单个 KM 链接 → KMIngestResult。"""
    enc_id, sheet_id = km_client.parse_km_url(url)
    if not enc_id:
        return KMIngestResult(
            url=url, enc_id="", sheet_id=None,
            status="error", message="无法解析 enc_id",
        )

    # 幂等检查:已经入过库就直接复用旧行
    existing = _existing_raw(enc_id, sheet_id)
    if existing is not None:
        # 解析旧行 attachments 里的 doc_type / title 给上层回显
        title = ""
        doc_type = ""
        try:
            atts = json.loads(existing.attachments_json or "[]")
            if atts and isinstance(atts, list):
                meta = atts[0]
                if isinstance(meta, dict):
                    title = meta.get("title") or ""
                    doc_type = meta.get("doc_type") or ""
        except (ValueError, TypeError):
            pass
        return KMIngestResult(
            url=url, enc_id=enc_id, sheet_id=sheet_id,
            status="skipped", raw_id=existing.id,
            title=title, doc_type=doc_type,
        )

    status, doc_type, title, text = _fetch_doc_text(enc_id, sheet_id)
    if status == "no_permission":
        return KMIngestResult(
            url=url, enc_id=enc_id, sheet_id=sheet_id,
            status="no_permission",
        )
    if status == "unsupported":
        return KMIngestResult(
            url=url, enc_id=enc_id, sheet_id=sheet_id,
            status="unsupported", title=title, doc_type=doc_type,
            message=text or f"不支持的 KM 文档类型: {doc_type or 'unknown'}",
        )
    if status == "error":
        return KMIngestResult(
            url=url, enc_id=enc_id, sheet_id=sheet_id,
            status="error", title=title, doc_type=doc_type,
            message=text,
        )

    # ok → 写 raw_input。attachments_json 存元信息(title / doc_type / source_url / sheet_id)
    meta = {
        "source": "km",
        "source_url": url,
        "enc_id": enc_id,
        "sheet_id": sheet_id or "",
        "title": title,
        "doc_type": doc_type,
    }
    with session() as s:
        row = raw_append(
            s,
            source_type="km_doc",
            source_ref=_source_ref(enc_id, sheet_id),
            author_domain=sender_domain,
            content_text=text,
            attachments_json=json.dumps([meta], ensure_ascii=False),
            chat_id=chat_id,
            parent_message_id=parent_message_id,
            media_type=doc_type,
        )
        s.commit()
        raw_id = row.id

    log.info(
        "km_ingest ok raw#%d enc=%s type=%s title=%r len=%d",
        raw_id, enc_id, doc_type, title[:40], len(text),
    )
    return KMIngestResult(
        url=url, enc_id=enc_id, sheet_id=sheet_id,
        status="ok", raw_id=raw_id, title=title, doc_type=doc_type,
    )


def ingest_text(
    text: str,
    *,
    sender_domain: str = "",
    chat_id: str = "",
    parent_message_id: str = "",
) -> list[KMIngestResult]:
    """扫文本里所有 KM 链接 → 逐个 ingest_one。"""
    urls = find_km_urls(text)
    return [
        ingest_one(
            url,
            sender_domain=sender_domain,
            chat_id=chat_id,
            parent_message_id=parent_message_id,
        )
        for url in urls
    ]


# ---------- 用户友好回告 ----------

def format_results(results: list[KMIngestResult]) -> str:
    """给 wave reply 用的简短中文回执。

    单条:`📄 已学习《标题》(document, raw#42)`
    多条:逐行汇总 ✓ / ⏭️ / ⚠️
    """
    if not results:
        return ""
    if len(results) == 1:
        r = results[0]
        return _format_one(r)
    lines = [_format_one(r) for r in results]
    return "\n".join(lines)


def _format_one(r: KMIngestResult) -> str:
    label = (r.title or r.enc_id or "未知文档")[:60]
    if r.status == "ok":
        return f"📄 已学习《{label}》({r.doc_type}, raw#{r.raw_id})"
    if r.status == "skipped":
        return f"⏭️ 《{label}》已学过 (raw#{r.raw_id}),跳过"
    if r.status == "no_permission":
        # 没拉到正文,title 也是空的 — 文案不带《》
        return "❌ 我没权限读这篇文档,你需要先进行授权"
    if r.status == "unsupported":
        return f"⚠️ 文档类型不支持({r.doc_type or 'unknown'}),无法读取"
    # error: 限流 / 网络 / KM 内部错 — 不暴露 retcode 给用户
    return "⚠️ 文档拉取失败,请稍后重试"
