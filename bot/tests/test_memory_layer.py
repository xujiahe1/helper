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
    from helper.storage.models import Memory

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


def test_extract_no_directives_when_doc_version_authority(db, settings, llm_stub):
    """闸 2: 知识源版本/权威性陈述应进 L1, 不进 memory。

    回归 raw#231 → memory#7 误抽: 用户原话
        "https://km.mihoyo.com/doc/mhhujaiuuz18 这里面的员工属性,
         是当前阶段最新的。你现在里面的那个是老的"
    被抽成 entity=lml员工属性 directive, 后续在 ask system_prompt 里
    强迫 bot 区分新/旧文档, 污染回复风格。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw(
        "https://km.mihoyo.com/doc/mhhujaiuuz18 这里面的员工属性,"
        "是当前阶段最新的。你现在里面的那个是老的"
    )
    llm_stub.set("memory_extract", json.dumps({"directives": []}))
    n = extract_for_raw(raw_id)
    assert n == 0
    with session() as s:
        assert s.execute(select(Memory)).scalars().all() == []


def test_extract_skip_when_subject_is_third_party_behavior(db, settings, llm_stub):
    """闸 1: 描述第三方行为模式不抽。

    回归 raw#84 → memory#3: 用户原话
        "你继续学习: 一般他很快回复,就是支持; 不回复,就是不支持。
         这个时候你就不要持续追问,要等待他心情好的时候,说下你的想法
         (不能很正确), 然后请教他的意思, 然后赞同。"
    句首"你继续学习"是祈使外壳, 实际描述的是"他"的行为模式 + 客观规律,
    不该抽成 bot 行为指令。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw(
        "你继续学习:一般他很快回复,就是支持这个事情;不回复,就是不支持。"
    )
    llm_stub.set("memory_extract", json.dumps({"directives": []}))
    n = extract_for_raw(raw_id)
    assert n == 0
    with session() as s:
        assert s.execute(select(Memory)).scalars().all() == []


def test_extract_skip_when_objective_fact_with_imperative_shell(db, settings, llm_stub):
    """闸 1: 关于第三方的客观事实, 即便包"你记住/告诉你"祈使外壳, 也不抽。

    回归 raw#195 → memory#5: 用户原话 "小猫老师是好人,你记住"
    是关于"小猫老师"的事实陈述,"你记住"只是语气词。被抽成
    entity=小猫老师 directive 后污染 ask system_prompt。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw("小猫老师是好人,你记住")
    llm_stub.set("memory_extract", json.dumps({"directives": []}))
    n = extract_for_raw(raw_id)
    assert n == 0
    with session() as s:
        assert s.execute(select(Memory)).scalars().all() == []


def test_extract_skip_when_unresolved_pronoun(db, settings, llm_stub):
    """闸 3: 多义代词在历史对话里 resolve 不出唯一对象 → 不抽。

    回归 raw#103 → memory#4: 用户原话 "如果是小猫老师本人问你, 你不能这么说"
    "这么说"指上文某段, 没历史时无法 resolve 成唯一具体内容,
    抽出来的 directive 是空心的, 不该抽。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw("如果是小猫老师本人问你,你不能这么说")
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


def test_extract_inherits_route_app_id_when_new_raw_has_none(db, settings, llm_stub):
    """同 scope 旧 memory 有 route_app_id, 新 raw 是"修正前提"型(原文不含 cli_xxx)
    → LLM 抽出来的 route_app_id 是空 → 代码层应自动从旧条继承。

    动机: raw#533 实测踩到的真坑 — owner 写"修正一下找 tachi 的前提...", LLM
    抽出新指令但丢了 route_app_id, 后续 ROUTE 路径反查 tachi 落空。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    # seed: 旧 memory 带 route_app_id
    r1 = _seed_raw("路由 X 类问题给 tachi, app_id 是 cli_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "tachi",
        "directive": "X 类问题路由给 tachi",
        "route_app_id": "cli_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }]}))
    extract_for_raw(r1)

    # 新 raw: 修正前提, 文本里没 cli_xxx → LLM 也只能给 route_app_id="" 的 directive
    r2 = _seed_raw("修正一下: 只有 X 子集才路由给 tachi")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "tachi",
        "directive": "只有 X 子集才路由给 tachi",
        "route_app_id": "",
    }]}))
    extract_for_raw(r2)

    with session() as s:
        new_mem = s.execute(
            select(Memory).where(Memory.scope_ref == "tachi").order_by(Memory.id.desc())
        ).scalars().first()
        # 新条目继承了旧的 route_app_id, 不再是空
        assert new_mem.route_app_id == "cli_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


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
    monkeypatch.setattr("helper.ask.runtime.retrieve_relevant", lambda q, top_k=8, asker_domain="": [])
    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")
    captured = {}

    def fake_ask(system: str, user: str, **kw):
        captured["system"] = system
        return "OK"

    llm_stub.set("ask", fake_ask)
    ask("无所谓问什么", asker_domain="alice")

    assert "## 用户偏好" in captured["system"]
    assert "回答尽量简洁" in captured["system"]


def test_ask_pulls_directive_via_fts_hit(db, settings, llm_stub, stub_bundle, monkeypatch):
    """题面里没有 entity 字面词,但 directive 文本本身被 fts 召回 → 仍拼进 prompt。

    回归 raw#272/273 转发失败:题面"看 iam 网关接入文档 iam_sid"无"tachi"字面,
    旧逻辑下 entity_refs=[] → entity scope directive 漏注入 → 路由分支不触发。
    修复: directive 文本进 fts 池, 命中后通过 directive_ids 路径强制拼接,
    不再依赖 entity slug 命中。
    """
    from helper.ask.retrieve import Hit
    from helper.ask.runtime import ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        m = Memory(
            scope_type="entity", scope_ref="tachi",
            directive="涉及 iam 网关 / app_id 类问题, 引导用户去艾特 tachi",
            route_app_id="cli_DEAD",
        )
        s.add(m); s.flush()
        mem_id = m.id

    fake_hits = [Hit(type="directive", ref=str(mem_id), title="t", body="b", score=1.0)]
    monkeypatch.setattr(
        "helper.ask.runtime.retrieve_relevant",
        lambda q, top_k=8, asker_domain="": fake_hits,
    )
    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")

    captured = {}

    def fake_ask(system: str, user: str, **kw):
        captured["system"] = system
        captured["user"] = user
        return "OK"

    llm_stub.set("ask", fake_ask)
    ask("看 iam 网关接入文档 iam_sid 怎么换域账号", asker_domain="alice")

    # directive 文本(不含 hash)拼进 system_prompt
    assert "## 用户偏好" in captured["system"]
    assert "tachi" in captured["system"]
    assert "iam 网关" in captured["system"]
    # cli_xxx hash 绝不进 LLM 视野 — 防 LLM 复述给用户
    assert "cli_DEAD" not in captured["system"]
    assert "cli_DEAD" not in captured["user"]


def test_directive_pass_pulls_via_token_overlap(db, settings):
    """_directive_pass 直读 Memory 表, 题面与 directive token 有交集就出。

    不依赖 fts_items 索引(directive 是行为指令, 优先级最高, 不和 raw/section
    走 bm25 抢 RRF top_k)。
    """
    from helper.ask.retrieve import _directive_pass
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="涉及 iam 网关 / app_id 类问题艾特 tachi",
            route_app_id="cli_DEAD",
        ))
        s.add(Memory(
            scope_type="global", scope_ref="",
            directive="回答尽量简洁",
        ))
        s.add(Memory(  # superseded — 不该出
            scope_type="global", scope_ref="",
            directive="iam 老指令",
            superseded_at=__import__("datetime").datetime.utcnow(),
        ))

    # 题面共词命中"iam" → tachi 那条出, 简洁那条不出
    hits = _directive_pass("看下 iam 网关接入文档 iam_sid 怎么换域账号")
    bodies = [h.body for h in hits]
    assert any("tachi" in b for b in bodies)
    assert not any("简洁" in b for b in bodies)
    assert not any("老指令" in b for b in bodies)


def test_directive_pass_skips_when_no_token_overlap(db, settings):
    """题面和 directive 完全无共词 → 不出(避免无关 directive 全部塞进 prompt)。"""
    from helper.ask.retrieve import _directive_pass
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="iam / 网关 / 认证 类问题艾特 tachi",
        ))

    hits = _directive_pass("今天天气怎么样")
    assert hits == []


def test_extract_splits_app_id_to_route_field(db, settings, llm_stub):
    """LLM 把 cli_xxx 抽到 route_app_id 字段, directive 文本不含 hash。

    回归: 之前 LLM 抽出来的 directive 文本里夹着 'tachi 的 appid 是 cli_xxx',
    在 ask system_prompt 里被 LLM 当成可复述的事实 — 用户问相邻话题时 LLM
    在回答末尾抄出这个 hash 推荐用户去问 tachi。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw(
        "iam 网关 / 查 app_id 类问题艾特 tachi, tachi 的 appid 是 cli_7847a145e02d020b9b7dcec8b6391ab6",
    )
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "tachi",
        "directive": "iam 网关 / 查 app_id 类问题艾特 tachi",
        "route_app_id": "cli_7847a145e02d020b9b7dcec8b6391ab6",
    }]}))

    extract_for_raw(raw_id)

    with session() as s:
        m = s.execute(select(Memory)).scalar_one()
        assert m.scope_ref == "tachi"
        assert m.route_app_id == "cli_7847a145e02d020b9b7dcec8b6391ab6"
        # directive 文本不含 hash
        assert "cli_" not in m.directive
        assert "tachi" in m.directive
        assert "iam" in m.directive


def test_extract_scrubs_app_id_when_llm_forgets(db, settings, llm_stub):
    """兜底: LLM 漏剥 hash 仍把 cli_xxx 写进 directive → 落库前正则抠掉。

    LLM prompt 已经让它分离, 但 prompt 不是硬约束, 落库前必须再做一次结构清洗。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    raw_id = _seed_raw("iam 类问题艾特 tachi, app_id cli_7847a145e02d020b9b7dcec8b6391ab6")
    # 模拟 LLM "懒" — 没把 hash 抽到 route_app_id 而是留在 directive 里
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "tachi",
        "directive": "iam 类问题艾特 tachi, app_id 是 cli_7847a145e02d020b9b7dcec8b6391ab6",
        "route_app_id": "",
    }]}))

    extract_for_raw(raw_id)

    with session() as s:
        m = s.execute(select(Memory)).scalar_one()
        assert "cli_" not in m.directive
        assert m.route_app_id == "cli_7847a145e02d020b9b7dcec8b6391ab6"


def test_lookup_directive_text_does_not_leak_app_id(db, settings):
    """directives_for_ask 输出的偏好段里, 即便 memory.route_app_id 有值, hash 也不出现。"""
    from helper.memory.lookup import directives_for_ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="iam 类问题艾特 tachi",
            route_app_id="cli_FAKE",
        ))

    block = directives_for_ask(entity_refs=["tachi"])
    assert "iam" in block
    assert "cli_" not in block


def test_resolve_route_app_id_by_name(db, settings):
    """resolve_route_app_id 按 entity 名反查, 取最新的 alive memory.route_app_id。"""
    from datetime import datetime, timezone

    from helper.memory.lookup import resolve_route_app_id
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        # 旧的 superseded
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="老 指令", route_app_id="cli_OLD",
            superseded_at=datetime.now(timezone.utc),
        ))
        # 新的 alive
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="新 指令", route_app_id="cli_NEW",
        ))
        # 不相干 entity
        s.add(Memory(
            scope_type="entity", scope_ref="other",
            directive="...", route_app_id="cli_OTHER",
        ))

    assert resolve_route_app_id("tachi") == "cli_NEW"
    assert resolve_route_app_id("other") == "cli_OTHER"
    assert resolve_route_app_id("does_not_exist") == ""
    assert resolve_route_app_id("") == ""


def test_ask_route_resolves_by_name_not_llm_hash(db, settings, llm_stub, stub_bundle, monkeypatch):
    """ask 路由分支: LLM 输出 ROUTE: tachi 后, 由代码反查真 app_id 发 RouteRequest。

    LLM 视野里没 hash, 不会编造 cli_tachi 之类错的 hash。
    """
    from helper.ask.runtime import RouteRequest, ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="iam 类问题艾特 tachi",
            route_app_id="cli_REAL",
        ))

    monkeypatch.setattr("helper.ask.runtime.retrieve_relevant", lambda q, top_k=8, asker_domain="": [])
    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")

    llm_stub.set("ask", "ROUTE: tachi")
    out = ask("iam 网关接入流程", asker_domain="alice")

    assert isinstance(out, RouteRequest)
    assert out.target_app_id == "cli_REAL"
    assert out.via_label == "tachi"


def test_ask_route_legacy_format_still_works(db, settings, llm_stub, stub_bundle, monkeypatch):
    """LLM 凭旧训练习惯抄 'ROUTE: cli_xxx | tachi' 旧格式 → 我们丢弃 cli_, 按 name 反查。

    防 LLM 拼了一个过期或编造的 hash 直接被当成 target_app_id 发出去。
    """
    from helper.ask.runtime import RouteRequest, ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        s.add(Memory(
            scope_type="entity", scope_ref="tachi",
            directive="iam 类问题艾特 tachi",
            route_app_id="cli_REAL",
        ))

    monkeypatch.setattr("helper.ask.runtime.retrieve_relevant", lambda q, top_k=8, asker_domain="": [])
    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")

    # LLM 抄回旧格式且 cli_ 部分是错的 — 我们应该忽略 cli_FAKE, 按 tachi 反查到 cli_REAL
    llm_stub.set("ask", "ROUTE: cli_FAKE | tachi")
    out = ask("iam 网关接入流程", asker_domain="alice")

    assert isinstance(out, RouteRequest)
    assert out.target_app_id == "cli_REAL"


def test_ask_route_fallback_when_no_app_id_registered(db, settings, llm_stub, stub_bundle, monkeypatch):
    """LLM 输出 ROUTE: <name> 但 memory 里没登记 route_app_id → 走兜底文案, 不发 RouteRequest。

    防 LLM 给了一个不存在的 bot 名字时把链路打挂。
    """
    from helper.ask.runtime import Answer, ask

    monkeypatch.setattr("helper.ask.runtime.retrieve_relevant", lambda q, top_k=8, asker_domain="": [])
    monkeypatch.setattr("helper.ask.runtime.current_bundle_version", lambda: "test")

    llm_stub.set("ask", "ROUTE: ghost_bot")
    out = ask("随便问个问题", asker_domain="alice")

    assert isinstance(out, Answer)
    assert "ghost_bot" in out.answer


def test_lookup_pulls_directive_by_id_regardless_of_scope(db, settings):
    """directives_for_ask 收到 directive_ids 后, 命中的 memory 不论 scope 都拼。"""
    from helper.memory.lookup import directives_for_ask
    from helper.storage import session
    from helper.storage.models import Memory

    with session() as s:
        m = Memory(scope_type="entity", scope_ref="tachi", directive="X 类问题艾特 tachi")
        s.add(m); s.flush()
        mem_id = m.id

    # entity_refs 不命中 → 旧路径返空
    assert directives_for_ask(entity_refs=[]) == ""
    # directive_ids 命中 → 拼上(走新路径)
    block = directives_for_ask(entity_refs=[], directive_ids=[mem_id])
    assert "tachi" in block
    assert "艾特 tachi" in block


# ---------- entity_alias (改动 1) ----------


def test_alias_resolve_fallback_when_no_record(db, settings):
    from helper.memory.alias import resolve_alias

    assert resolve_alias("未登记") == "未登记"
    assert resolve_alias("") == ""


def test_alias_resolve_canonical(db, settings):
    """add_alias 后两个名字都映射到 canonical (canonical 自映射也存在)。"""
    from helper.memory.alias import add_alias, resolve_alias
    from helper.storage import session
    from helper.storage.models import EntityAlias

    add_alias("小猫老师", "周婷", source="manual")

    assert resolve_alias("小猫老师") == "周婷"
    assert resolve_alias("周婷") == "周婷"  # 自映射

    with session() as s:
        rows = s.execute(select(EntityAlias)).scalars().all()
        names = {r.name: (r.canonical, r.source) for r in rows}
        assert names["小猫老师"] == ("周婷", "manual")
        assert names["周婷"] == ("周婷", "manual")


def test_alias_manual_not_overridden_by_auto(db, settings):
    """manual 是 owner 显式声明, auto 是相似度回写, 不能覆盖。"""
    from helper.memory.alias import add_alias, resolve_alias

    add_alias("小猫", "周婷", source="manual")
    add_alias("小猫", "陈雨晴", source="auto")  # 应被忽略

    assert resolve_alias("小猫") == "周婷"


def test_alias_mark_not_alias(db, settings):
    """owner 在周报选 "保留" 否决疑似同义 → 两个名字标 reverted, resolve 返自身。"""
    from helper.memory.alias import add_alias, mark_not_alias, resolve_alias
    from helper.storage import session
    from helper.storage.models import EntityAlias

    # 先有 auto 关联, owner 否决
    add_alias("X", "Y", source="auto")
    mark_not_alias("X", "Y")

    assert resolve_alias("X") == "X"
    assert resolve_alias("Y") == "Y"

    with session() as s:
        rows = s.execute(select(EntityAlias)).scalars().all()
        for r in rows:
            assert r.source == "reverted"


def test_extract_alias_declaration_lands_in_table(db, settings, llm_stub):
    """LLM 输出 aliases 数组 → entity_alias 表有记录, Memory 表无记录。"""
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import EntityAlias, Memory

    raw_id = _seed_raw("小猫老师就是周婷,记一下。")
    llm_stub.set(
        "memory_extract",
        json.dumps({
            "directives": [],
            "aliases": [{"name": "小猫老师", "canonical": "周婷"}],
        }),
    )

    n = extract_for_raw(raw_id)
    assert n == 0  # 没 directive 落 Memory

    with session() as s:
        memories = s.execute(select(Memory)).scalars().all()
        assert memories == []
        aliases = {r.name: r.canonical for r in s.execute(select(EntityAlias)).scalars()}
        assert aliases.get("小猫老师") == "周婷"
        assert aliases.get("周婷") == "周婷"


def test_extract_scope_ref_normalized_via_alias(db, settings, llm_stub):
    """先 add_alias, 再抽 directive scope_ref=别名 → Memory 落库 scope_ref=主名。"""
    from helper.memory import extract_for_raw
    from helper.memory.alias import add_alias
    from helper.storage import session
    from helper.storage.models import Memory

    add_alias("小猫", "周婷", source="manual")

    raw_id = _seed_raw("小猫的问题统一艾特她本人,bot 不要直接答。")
    llm_stub.set(
        "memory_extract",
        json.dumps({"directives": [{
            "scope_type": "entity", "scope_ref": "小猫",
            "directive": "她的问题艾特本人,bot 不直接答",
        }]}),
    )

    n = extract_for_raw(raw_id)
    assert n == 1

    with session() as s:
        rows = s.execute(select(Memory)).scalars().all()
        assert len(rows) == 1
        assert rows[0].scope_ref == "周婷"  # 归一到主名


# ---------- Memory 向量相似 fallback (改动 2) ----------


def _stub_embedding(monkeypatch, vector_map: dict[str, list[float]]):
    """把 _compute_embedding 替换成查表函数,
    没记录的 directive 返回空 bytes。"""
    import struct

    def fake(text: str) -> bytes:
        vec = vector_map.get(text)
        if vec is None or len(vec) != 1024:
            return b""
        return struct.pack(f"{len(vec)}e", *vec)

    monkeypatch.setattr("helper.memory.extract._compute_embedding", fake)


def _vec_with(seed: float) -> list[float]:
    """生成一个 1024 维向量, 每维都是 seed (常向量), 方便构造可控的余弦。"""
    return [seed] * 1024


def _vec_orthogonal(seed_a: float, seed_b: float, split: int = 512) -> list[float]:
    """构造跟 _vec_with(x) 余弦相似度可控的向量 — 前 split 维 seed_a, 后段 seed_b。"""
    return [seed_a] * split + [seed_b] * (1024 - split)


def test_extract_writes_embedding(db, settings, llm_stub, monkeypatch):
    """落 Memory 时 embedding 列非空且长度 = 2048 字节 (fp16 1024 维)。"""
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import Memory

    _stub_embedding(monkeypatch, {"指令文本 X": _vec_with(0.5)})

    raw_id = _seed_raw("一些原文,触发 LLM 抽出指令文本 X")
    llm_stub.set(
        "memory_extract",
        json.dumps({"directives": [{
            "scope_type": "entity", "scope_ref": "alice",
            "directive": "指令文本 X",
        }]}),
    )

    n = extract_for_raw(raw_id)
    assert n == 1

    with session() as s:
        rows = s.execute(select(Memory)).scalars().all()
        assert len(rows) == 1
        assert rows[0].embedding is not None
        assert len(rows[0].embedding) == 2048


def test_detect_semantic_match_above_threshold(db, settings, llm_stub, monkeypatch):
    """跨 scope + embedding 余弦 ≥ 0.85 → 挂"同义疑似"冲突 + alias_hint 填好。

    两条都用 _vec_with(0.5) 常向量, 余弦 = 1.0, 必然 ≥ 0.85。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog, Memory

    _stub_embedding(monkeypatch, {
        "directive_old": _vec_with(0.5),
        "directive_new": _vec_with(0.5),
    })

    # 先 seed 一条 alive memory (scope=小猫)
    raw_a = _seed_raw("旧消息")
    llm_stub.set(
        "memory_extract",
        json.dumps({"directives": [{
            "scope_type": "entity", "scope_ref": "小猫",
            "directive": "directive_old",
        }]}),
    )
    extract_for_raw(raw_a)

    # 再来一条 scope=周婷 的相似 directive
    raw_b = _seed_raw("新消息")
    llm_stub.set(
        "memory_extract",
        json.dumps({"directives": [{
            "scope_type": "entity", "scope_ref": "周婷",
            "directive": "directive_new",
        }]}),
    )
    extract_for_raw(raw_b)

    with session() as s:
        memories = s.execute(select(Memory).order_by(Memory.id)).scalars().all()
        assert len(memories) == 2  # 两条都落, 不是直接覆盖
        cls = s.execute(select(ConflictLog)).scalars().all()
        assert len(cls) == 1
        cl = cls[0]
        assert cl.target_type == "memory"
        assert cl.target_slug == str(memories[0].id)  # 旧那条
        assert cl.alias_hint == "周婷||小猫"  # 新在前, 旧在后
        assert "[同义疑似]" in cl.summary


def test_detect_below_threshold_no_conflict(db, settings, llm_stub, monkeypatch):
    """跨 scope + 余弦 < 0.85 → 不挂冲突。

    _vec_with(1.0) 跟 _vec_orthogonal(1.0, -1.0, 512) 余弦 = 0 < 0.85。
    """
    from helper.memory import extract_for_raw
    from helper.storage import session
    from helper.storage.models import ConflictLog

    _stub_embedding(monkeypatch, {
        "old_dir": _vec_with(1.0),
        "new_dir": _vec_orthogonal(1.0, -1.0, 512),
    })

    raw_a = _seed_raw("旧")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "甲", "directive": "old_dir",
    }]}))
    extract_for_raw(raw_a)

    raw_b = _seed_raw("新")
    llm_stub.set("memory_extract", json.dumps({"directives": [{
        "scope_type": "entity", "scope_ref": "乙", "directive": "new_dir",
    }]}))
    extract_for_raw(raw_b)

    with session() as s:
        cls = s.execute(select(ConflictLog)).scalars().all()
        assert cls == []


def test_resolve_memory_superseded_writes_auto_alias(db, settings):
    """memory + alias_hint 非空 + resolution=superseded → entity_alias 多 source=auto。"""
    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog, EntityAlias, Memory

    raw_id = _seed_raw("x")
    with session() as s:
        old_mem = Memory(scope_type="entity", scope_ref="小猫", directive="d1")
        new_mem = Memory(scope_type="entity", scope_ref="周婷", directive="d2")
        s.add_all([old_mem, new_mem])
        s.flush()
        cl = ConflictLog(
            raw_id=raw_id, target_type="memory",
            target_slug=str(old_mem.id),
            summary="[同义疑似] ...", severity="medium",
            alias_hint="周婷||小猫",
        )
        s.add(cl)
        s.flush()
        log_id = cl.id
        old_id = old_mem.id

    ok = resolve(log_id, resolution="superseded", resolver_domain="owner")
    assert ok is True

    with session() as s:
        # 老 memory supersede
        m = s.get(Memory, old_id)
        assert m.superseded_at is not None
        # alias 表写了 auto
        rows = s.execute(select(EntityAlias)).scalars().all()
        d = {r.name: (r.canonical, r.source) for r in rows}
        assert d.get("周婷") == ("小猫", "auto")
        # canonical 自映射也存在
        assert d.get("小猫") == ("小猫", "auto")


def test_resolve_memory_rejected_marks_not_alias(db, settings):
    """memory + alias_hint 非空 + resolution=rejected → 两边 source=reverted。"""
    from helper.conflict import resolve
    from helper.storage import session
    from helper.storage.models import ConflictLog, EntityAlias, Memory

    raw_id = _seed_raw("y")
    with session() as s:
        old_mem = Memory(scope_type="entity", scope_ref="A", directive="d1")
        s.add(old_mem)
        s.flush()
        cl = ConflictLog(
            raw_id=raw_id, target_type="memory",
            target_slug=str(old_mem.id),
            summary="[同义疑似] ...", severity="medium",
            alias_hint="B||A",
        )
        s.add(cl)
        s.flush()
        log_id = cl.id

    ok = resolve(log_id, resolution="rejected", resolver_domain="owner")
    assert ok is True

    with session() as s:
        rows = s.execute(select(EntityAlias)).scalars().all()
        for r in rows:
            assert r.source == "reverted"
        names = {r.name for r in rows}
        assert {"A", "B"} <= names
