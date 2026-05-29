"""vector.index_raw — 拼接内容超过 EMBED_INPUT_CHAR_CAP 时安全截断。"""

from __future__ import annotations

import json

import pytest

from helper.storage import session, vector
from helper.storage.models import L1Item, L1Result, RawInput


def _seed_raw(s, *, content_text: str, atoms: list[dict]) -> int:
    r = RawInput(source_type="km_doc", source_ref="t1", content_text=content_text)
    s.add(r)
    s.flush()
    s.add(L1Result(raw_id=r.id, error="", model="test"))
    for idx, payload in enumerate(atoms):
        s.add(L1Item(raw_id=r.id, idx=idx, type="fact", payload_json=json.dumps(payload, ensure_ascii=False)))
    s.commit()
    return r.id


def test_long_doc_index_call_truncates(db, monkeypatch):
    """416K 字符 raw + 大量 L1 → upsert 接收的内容应该 ≤ EMBED_INPUT_CHAR_CAP。"""
    received: list[str] = []

    def fake_upsert(sess, *, kind, ref, content):
        received.append(content)
        return 1

    monkeypatch.setattr(vector, "upsert", fake_upsert)

    huge = "巨长正文" * 200_000  # ~800K 字符
    atoms = [{"subject": f"事实 {i}", "predicate": "是", "object": "X" * 100} for i in range(500)]
    with session() as s:
        rid = _seed_raw(s, content_text=huge, atoms=atoms)

    with session() as s:
        vector.index_raw(s, rid)

    assert len(received) == 1
    assert len(received[0]) <= vector.EMBED_INPUT_CHAR_CAP


def test_short_doc_no_truncation(db, monkeypatch):
    received: list[str] = []

    def fake_upsert(sess, *, kind, ref, content):
        received.append(content)
        return 1

    monkeypatch.setattr(vector, "upsert", fake_upsert)

    with session() as s:
        rid = _seed_raw(s, content_text="短正文", atoms=[{"subject": "A", "predicate": "is", "object": "B"}])

    with session() as s:
        vector.index_raw(s, rid)

    assert len(received) == 1
    # 短输入应该原样进
    assert "短正文" in received[0]
    assert "A" in received[0]


def test_atoms_preserved_when_raw_huge(db, monkeypatch):
    """raw 巨大但 L1 原子小 — 原子应当尽量保留(它们是高信息密度部分)。"""
    received: list[str] = []

    def fake_upsert(sess, *, kind, ref, content):
        received.append(content)
        return 1

    monkeypatch.setattr(vector, "upsert", fake_upsert)

    huge = "X" * 500_000
    with session() as s:
        rid = _seed_raw(
            s,
            content_text=huge,
            atoms=[{"subject": "lml", "predicate": "回流", "object": "mhy"}],
        )

    with session() as s:
        vector.index_raw(s, rid)

    # 由于截断从前往后,巨型 raw_text 会占满前部 quota,但原子应在 quota 余量里能进去
    assert "lml" in received[0] or "回流" in received[0] or "mhy" in received[0]
