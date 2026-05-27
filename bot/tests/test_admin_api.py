"""/admin/* 鉴权 + 主要端点。"""

from __future__ import annotations

import json


def _seed_raw_with_l1(make_raw, text: str = "决策原文") -> int:
    from helper.storage import session
    from helper.storage.models import L1Item, L1Result

    rid = make_raw(text)
    with session() as s:
        s.add(L1Result(raw_id=rid, error="", model="claude-test"))
        s.add(L1Item(
            raw_id=rid,
            idx=0,
            type="decision",
            payload_json=json.dumps(
                {"scene": "S", "signals": ["s1"], "tradeoffs": [], "choice": "C", "rationale": "R"},
                ensure_ascii=False,
            ),
        ))
    return rid


def test_admin_unauth_returns_401(app_client):
    app_client.headers.pop("X-Helper-Admin-Key")
    resp = app_client.get("/admin/healthz")
    assert resp.status_code == 401


def test_admin_healthz(app_client):
    resp = app_client.get("/admin/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["admin"] is True


def test_list_raw_inputs(app_client, make_raw):
    rid = _seed_raw_with_l1(make_raw)
    resp = app_client.get("/admin/raw-inputs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    item = next(it for it in body["items"] if it["id"] == rid)
    assert item["l1"]["status"] == "ok"


def test_show_raw_returns_atoms(app_client, make_raw):
    rid = _seed_raw_with_l1(make_raw)
    resp = app_client.get(f"/admin/raw-inputs/{rid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["raw"]["id"] == rid
    assert body["l1"]["atoms"]
    assert body["l1"]["atoms"][0]["type"] == "decision"


def test_show_raw_404(app_client):
    resp = app_client.get("/admin/raw-inputs/9999")
    assert resp.status_code == 404


def test_browse_raw_html(app_client, make_raw):
    rid = _seed_raw_with_l1(make_raw)
    resp = app_client.get(f"/admin/browse/raw/{rid}")
    assert resp.status_code == 200
    assert "decision" in resp.text
    assert f"raw#{rid}" in resp.text


def test_browse_index_renders(app_client):
    resp = app_client.get("/admin/browse")
    assert resp.status_code == 200
    assert "Helper 知识库" in resp.text
