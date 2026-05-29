"""M5 procedural memory 层 — 抽取 / 拼接 / 冲突路径覆盖。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select


def _seed_raw(content_text: str, *, author: str = "alice") -> int:
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        r = RawInput(
            source_type="im_wave",
            source_ref=f"test-{content_text[:10]}",
            content_text=content_text,
            author_domain=author,
        )
        s.add(r)
        s.flush()
        return r.id


# ---------- extract ----------


def test_extract_directive_with_entity_scope(db, settings, llm_stub):
    """LLM 抽到 entity scope 的 directive,落库 + 解析 scope_ref。"""
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import EntityCandidate, Memory

    with session() as s:
        s.add(EntityCandidate(slug="哥", name="哥", description="刘佳翔"))

    raw_id = _seed_raw("回答哥相关的问题时,不用每次都把哥的身份说一遍。")
    llm_stub.set(
        "memory_extract",
        json.dumps({"directives": [{
            "scope_type": "entity", "scope_ref": "哥",
            "directive": "身份已知不复述",
        }]}),
    )

    n = extract_for_raw(raw_id)
    assert n == 1

    with session() as s:
        rows = s.execute(select(Memory)).scalars().all()
        assert len(rows) == 1
        assert rows[0].scope_type == "entity"
        assert rows[0].scope_ref == "哥"
        assert "身份" in rows[0].directive
        assert rows[0].source_raw_id == raw_id
        assert rows[0].author_domain == "alice"


def test_extract_global_directive(db, settings, llm_stub):
    """global scope directive 也能落库。"""
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw("我喜欢简洁回答")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "global", "scope_ref": "",
        "directive": "回答尽量简洁",
    }]}))
    extract_for_raw(raw_id)

    with session() as s:
        m = s.execute(select(Memory)).scalar_one()
        assert m.scope_type == "global"
        assert m.scope_ref == ""


def test_extract_no_directives_when_pure_fact(db, settings, llm_stub):
    """LLM 判断没有指令(纯事实) → 0 条落库。"""
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw("刘佳翔是米哈游信息化负责人")
    llm_stub.set("memory_extract", json.dumps({"directives": []}))
    n = extract_for_raw(raw_id)
    assert n == 0
    with session() as s:
        assert s.execute(select(Memory)).scalars().all() == []


def test_extract_handles_bad_json(db, settings, llm_stub):
    """LLM 返非 JSON → 静默 0 条,不抛错。"""
    from helper.memory import extract_for_raw

    raw_id = _seed_raw("随便一句")
    llm_stub.set("memory_extract", "not a json at all")
    assert extract_for_raw(raw_id) == 0


def test_extract_injects_chat_context_for_pronoun_resolution(db, settings, llm_stub):
    """memory_extract 在群聊场景下注入历史对话, LLM 能看到当前消息里"他"指代谁。

    回归 bug: 之前 prompt 里只有当前消息, "他"无法解析成历史里出现过的实体。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory, RawInput

    chat_id = "oc_g1"
    # 历史: 用户先问"哥是谁", bot 答介绍了"哥"
    with session() as s:
        s.add(RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            source_ref="hist-1", content_text="哥是谁",
            author_domain="alice", chat_id=chat_id, is_at_bot=True,
        ))
        s.add(RawInput(
            source_type="im_wave_bot",
            source_ref="hist-2", content_text="哥特指刘佳翔, 米哈游信息化负责人。",
            author_domain="alice", chat_id=chat_id,
        ))

    # 当前消息: 第三方在群里教 bot 一条规则, 里面用了代词"他"
    raw_id = _seed_raw_in_chat(
        "你继续学习: 一般他很快回复就是支持, 不回复就是不支持。",
        author="ting.zhou02", chat_id=chat_id, is_at_bot=True,
    )

    captured: dict = {}

    def _handler(*, system: str, user: str, **_: Any) -> str:
        captured["user"] = user
        return json.dumps({"directives": [{
            "scope_type": "entity", "scope_ref": "哥",  # LLM 应当解出"他"=哥
            "directive": "他快速回复=支持, 不回复=不支持; 不持续追问",
        }]})

    llm_stub.set("memory_extract", _handler)
    n = extract_for_raw(raw_id)
    assert n == 1

    # 1) LLM 拿到的 user prompt 里能看到历史对话块和具体实体名"哥"
    user_prompt = captured["user"]
    assert "## 历史对话" in user_prompt
    assert "哥是谁" in user_prompt
    assert "刘佳翔" in user_prompt
    assert "## 当前消息" in user_prompt

    # 2) 落库后 scope_ref = 具体实体名, 不是代词
    with session() as s:
        rows = s.execute(select(Memory)).scalars().all()
        assert len(rows) == 1
        assert rows[0].scope_ref == "哥"


def _seed_raw_in_chat(content_text: str, *, author: str, chat_id: str, is_at_bot: bool) -> int:
    """带群信息的种子 — chat_context 拼接需要 chat_id + im_wave 来源。"""
    from helper.storage import session
    from helper.storage.models import RawInput

    with session() as s:
        r = RawInput(
            source_type="im_wave:im.msg.group.sent_v2",
            source_ref=f"test-{content_text[:10]}",
            content_text=content_text,
            author_domain=author,
            chat_id=chat_id,
            is_at_bot=is_at_bot,
        )
        s.add(r)
        s.flush()
        return r.id


# ---------- conflict ----------


def test_conflict_logged_when_same_scope_already_has_alive(db, settings, llm_stub):
    """同 scope 已有 alive directive 写新条目 → 挂 ConflictLog,旧条目不直接覆盖。"""
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog, Memory

    # 旧条目
    r1 = _seed_raw("回答哥相关的问题时不用复述身份", author="alice")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "哥",
        "directive": "身份已知不复述",
    }]}))
    extract_for_raw(r1)

    # 新条目(冲突)
    r2 = _seed_raw("以后答哥的问题要把身份说清楚", author="bob")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "哥",
        "directive": "每次完整复述身份",
    }]}))
    extract_for_raw(r2)

    with session() as s:
        mems = s.execute(select(Memory).order_by(Memory.id)).scalars().all()
        assert len(mems) == 2
        # 新旧都 alive,等裁决
        assert all(m.superseded_at is None for m in mems)
        # 冲突挂上
        conflicts = s.execute(
            select(ConflictLog).where(ConflictLog.target_type == "memory")
        ).scalars().all()
        assert len(conflicts) == 1
        assert conflicts[0].target_slug == str(mems[0].id)


def test_conflict_resolve_supersede_marks_old_memory(db, settings, llm_stub):
    """裁决"采纳新"(superseded) → 旧 memory 打 superseded_at,新 memory 仍 alive。"""
    from helper.conflict.detector import resolve
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog, Memory

    r1 = _seed_raw("第一条")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "global", "scope_ref": "",
        "directive": "回答简短",
    }]}))
    extract_for_raw(r1)

    r2 = _seed_raw("第二条")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "global", "scope_ref": "",
        "directive": "回答详细",
    }]}))
    extract_for_raw(r2)

    with session() as s:
        conflict_id = s.execute(
            select(ConflictLog.id).where(ConflictLog.target_type == "memory")
        ).scalar_one()

    assert resolve(conflict_id, resolution="superseded", resolver_domain="owner")

    with session() as s:
        mems = s.execute(select(Memory).order_by(Memory.id)).scalars().all()
        # 旧的(简短)被 superseded;新的(详细)alive
        assert mems[0].superseded_at is not None
        assert mems[1].superseded_at is None


# ---------- lookup ----------


def test_lookup_global_directive_always_returned(db, settings):
    """global scope 不需要 entity_refs 也能被召回。"""
    from helper.memory.lookup import directives_for_ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(scope_type="global", scope_ref="", directive="回答简洁"))

    block = directives_for_ask(entity_refs=[])
    assert "## 用户偏好" in block
    assert "回答简洁" in block


def test_lookup_entity_directive_only_when_entity_in_refs(db, settings):
    """entity scope directive 仅当 entity 在 refs 列表里才召回(避免无关偏好打扰)。"""
    from helper.memory.lookup import directives_for_ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(scope_type="entity", scope_ref="哥", directive="身份已知不复述"))

    # 没命中 entity → 不出现
    assert directives_for_ask(entity_refs=["其它"]) == ""
    # 命中 → 出现
    block = directives_for_ask(entity_refs=["哥", "李四"])
    assert "哥" in block
    assert "身份已知不复述" in block


def test_lookup_skips_superseded(db, settings):
    """superseded 的 directive 不进 lookup。"""
    from datetime import datetime, timezone

    from helper.memory.lookup import directives_for_ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="global", scope_ref="", directive="老指令",
            superseded_at=datetime.now(timezone.utc),
        ))
        s.add(Memory(scope_type="global", scope_ref="", directive="新指令"))

    block = directives_for_ask(entity_refs=[])
    assert "新指令" in block
    assert "老指令" not in block


def test_lookup_empty_when_no_memory(db, settings):
    from helper.memory.lookup import directives_for_ask

    assert directives_for_ask(entity_refs=["哥"]) == ""


# ---------- e2e: ask 拼接 ----------


def test_ask_system_prompt_includes_directives(db, settings, llm_stub, stub_bundle, monkeypatch):
    """ask runtime 会把命中的 directive 拼进 system prompt。"""
    from helper.ask.runtime import ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(scope_type="global", scope_ref="", directive="回答尽量简洁"))

    # stub retrieve 返空 + ask 模型返简单 JSON
    monkeypatch.setattr("helper.ask.runtime.retrieve_relevant", lambda q, top_k=8: [])
    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")
    captured = {}

    def fake_ask(system: str, user: str, **kw):
        captured["system"] = system
        return json.dumps({"answer": "OK", "confidence": "low", "citations": []})

    llm_stub.set("ask", fake_ask)
    ask("无所谓问什么", asker_domain="alice")

    assert "## 用户偏好" in captured["system"]
    assert "回答尽量简洁" in captured["system"]
