"""召回硬隔离: bot 自答的 raw (source_type=im_wave_bot*) 永不进检索结果。

历史污染: l1-backfill --force-all 把 bot 回复的 raw 当漏抽跑了 L1, 抽出 section
进了 fts/vector 召回池。即便清掉了已索引数据, 召回侧仍要有硬隔离作产品边界保险:
反查 RawInput 时直接按 source_type 过滤, 即使将来索引层意外又混入也丢。
"""

from __future__ import annotations


def _seed_raw_with_l1(text, *, source_type, section_title, section_body):
    """造一条 raw + 跑成功的 L1Result + 一条 section atom + fts/vector 索引。"""
    import json as _json

    from helper.storage import fts, session, vector
    from helper.storage.models import L1Item, L1Result, RawInput

    with session() as s:
        r = RawInput(
            source_type=source_type,
            content_text=text,
            author_domain="alice",
            chat_id="oc_g",
        )
        s.add(r)
        s.flush()
        rid = r.id
        s.add(L1Result(raw_id=rid, error="", model="test"))
        s.add(L1Item(
            raw_id=rid,
            idx=0,
            type="section",
            payload_json=_json.dumps(
                {"title": section_title, "body": section_body, "topics": [], "entities": []},
                ensure_ascii=False,
            ),
        ))
        s.commit()

    # fts/vector 都建上 raw kind + section atom kind 索引 (模拟历史污染状态)
    with session() as s:
        fts.index_raw(s, rid)
        fts.index_l1_atom(s, rid, 0)
        s.commit()
    try:
        with session() as s:
            vector.index_raw(s, rid)
            vector.index_l1_atom(s, rid, 0)
            s.commit()
    except Exception:  # noqa: BLE001
        # 向量层在测试环境可能未初始化, 跳过 — fts 路径已能验证隔离
        pass

    return rid


def test_fts_raw_kind_drops_im_wave_bot(db, settings):
    """fts.search 命中 bot 回复的 raw → _hydrate_fts_hits 反查时整类丢弃。"""
    from helper.ask.retrieve import _fts_pass

    _seed_raw_with_l1(
        "刘佳翔确实是哥, 这是 bot 之前的回答",
        source_type="im_wave_bot",
        section_title="bot 自答抽出的伪知识",
        section_body="刘佳翔就是哥",
    )
    # 对照: 同样查询命中一条用户 raw, 应能召回
    user_rid = _seed_raw_with_l1(
        "刘佳翔今天提了 IAM 网关的方案",
        source_type="im_wave:im.msg.direct.sent_v2",
        section_title="IAM 网关方案",
        section_body="刘佳翔今天提了 IAM 网关的方案",
    )

    hits = _fts_pass("刘佳翔", set())
    refs = {(h.type, h.ref) for h in hits}

    # bot 回复的 raw / 抽出的 section 都不该出现
    bot_refs = {(t, r) for (t, r) in refs if t == "raw" and not r.startswith(str(user_rid))}
    bot_section_refs = {(t, r) for (t, r) in refs if t == "section" and not r.startswith(f"{user_rid}:")}
    assert all(t != "raw" or r == str(user_rid) for (t, r) in refs), (
        f"bot raw 不该召回, refs={refs}"
    )
    assert all(t != "section" or r.startswith(f"{user_rid}:") for (t, r) in refs), (
        f"bot 来源的 section atom 不该召回, refs={refs}"
    )
    # 用户 raw 应能召回 (起码 raw kind 命中)
    assert ("raw", str(user_rid)) in refs or any(
        t == "section" and r.startswith(f"{user_rid}:") for (t, r) in refs
    )


def test_persist_bot_reply_writes_skipped_l1result(db, settings):
    """_persist_bot_reply 落 raw 后必须立即写 L1Result(error=skipped:bot_reply),
    防 backfill --force-all 见 NULL 当成漏抽重抽。"""
    from helper.im.wave_actions import _persist_bot_reply
    from helper.storage import session
    from helper.storage.models import L1Result, RawInput

    _persist_bot_reply(
        text="bot 的回答内容",
        receiver_domain="alice",
        chat_id="oc_g",
        parent_message_id="om_user",
        bot_msg_id="om_bot_001",
    )

    with session() as s:
        raw = s.query(RawInput).filter(RawInput.wave_message_id == "om_bot_001").first()
        assert raw is not None
        assert raw.source_type == "im_wave_bot"

        lr = s.get(L1Result, raw.id)
        assert lr is not None, "_persist_bot_reply 必须落 L1Result 占位"
        assert lr.error == "skipped:bot_reply"
        assert lr.model == "bot_route"
