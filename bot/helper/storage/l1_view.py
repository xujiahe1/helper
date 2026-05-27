"""把 L1Item 行翻译成调用方友好的 dict 列表。

server.py / web/browser.py / 任何要展示 raw 的地方共用 — 避免到处反序列化 payload_json。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from helper.storage.models import L1Item


def list_l1_atoms(sess: Session, raw_id: int) -> list[dict]:
    """[{idx, type, payload}] 按 idx 升序。payload 是已解 JSON 的 dict。

    解析失败的 payload_json 返 {"_raw": "<原文>"};上层渲染按需展示。
    """
    rows = sess.execute(
        select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
    ).scalars().all()
    out: list[dict] = []
    for it in rows:
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {"_raw": it.payload_json}
        out.append({"idx": it.idx, "type": it.type, "payload": payload})
    return out
