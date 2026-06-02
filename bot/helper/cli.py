"""CLI 入口 — 本地开发与运维。"""

from __future__ import annotations

import json

import click

from helper import __version__


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Helper bot — 决策规约工厂。"""


def _print_l1_items(raw_id: int, *, model: str = "") -> None:
    """打印一条 raw 抽出的 L1Item 列表(按 type 分组)。"""
    from sqlalchemy import select

    from helper.storage import session
    from helper.storage.models import L1Item

    with session() as sess:
        items = sess.execute(
            select(L1Item).where(L1Item.raw_id == raw_id).order_by(L1Item.idx)
        ).scalars().all()

    if not items:
        click.echo(f"L1 stored (model={model}): 0 items")
        return
    click.echo(f"L1 stored (model={model}): {len(items)} items")
    for it in items:
        try:
            payload = json.loads(it.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {"_raw": it.payload_json}
        click.echo(f"  [#{it.idx}] {it.type}")
        click.echo(
            "    "
            + json.dumps(payload, ensure_ascii=False, indent=2).replace("\n", "\n    ")
        )


@main.command()
def hello() -> None:
    """Smoke test:确认包能装、入口能跑。"""
    click.echo(f"helper {__version__} — local dev OK")


@main.command()
def init() -> None:
    """初始化本地数据目录:sqlite + git spec repo + 默认策略。幂等。"""
    from helper.config import get_settings
    from helper.storage.db import init_engine
    from helper.storage.spec_repo import init_spec_repo

    s = get_settings()
    s.helper_data_dir.mkdir(parents=True, exist_ok=True)

    db_path = s.helper_data_dir / "helper.db"
    init_engine(db_path)
    click.echo(f"sqlite      : {db_path}")

    repo = init_spec_repo(s.helper_spec_git_dir)
    click.echo(f"spec git    : {repo.working_dir}")

    # 显示 seed 后能加载到的策略概览
    from helper.policy import load_knowledge_policy, load_llm_routing

    kp = load_knowledge_policy(s.helper_spec_git_dir)
    lr = load_llm_routing(s.helper_spec_git_dir)
    click.echo(f"knowledge   : v{kp.version}")
    click.echo(f"llm_routing : v{lr.version}, {len(lr.tasks)} tasks")

    if s.admin_enabled:
        click.echo("admin       : ENABLED (HELPER_ADMIN_SK is set)")
    else:
        click.echo("admin       : DISABLED (HELPER_ADMIN_SK empty → /admin/* returns 404)")

    if s.wave_callback_configured:
        click.echo("wave webhook: keys configured")
    else:
        click.echo("wave webhook: keys missing (本地开发 OK,部署前从 /etc/helper/wave.env 加载)")


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8009, show_default=True, type=int)
@click.option("--reload", is_flag=True, default=False, help="Auto-reload (dev only)")
def serve(host: str, port: int, reload: bool) -> None:
    """启 FastAPI server。"""
    import uvicorn

    from helper.config import get_settings
    from helper.storage.db import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    uvicorn.run(
        "helper.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=s.log_level.lower(),
    )


@main.command()
@click.argument("text")
@click.option("--source", default="cli", show_default=True, help="raw 来源类型")
@click.option("--author", default="", help="作者域账号(可选)")
def ingest(text: str, source: str, author: str) -> None:
    """从命令行扔一条 raw input,跑 L1 结构化,写入 sqlite + l1_results。"""
    from helper.config import get_settings
    from helper.ingest import process_raw
    from helper.storage import init_engine, raw_store, session

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    with session() as sess:
        row = raw_store.append(
            sess,
            source_type=source,
            content_text=text,
            author_domain=author,
        )
        raw_id = row.id
        click.echo(f"raw#{raw_id} stored")

    result = process_raw(raw_id)
    if result is None or result.error:
        click.echo(f"L1 ERROR: {result.error if result else 'process_raw returned None'}", err=True)
        raise SystemExit(1)
    _print_l1_items(raw_id, model=result.model)


@main.command("raw-list")
@click.option("--limit", default=20, show_default=True, type=int)
def raw_list(limit: int) -> None:
    """列最近的 raw_inputs + L1 状态。"""
    from helper.config import get_settings
    from helper.storage import init_engine, raw_store, session
    from helper.storage.models import L1Result

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    with session() as sess:
        rows = raw_store.list_recent(sess, limit=limit)
        if not rows:
            click.echo("(empty)")
            return
        click.echo(f"{'id':>4}  {'src':<28}  {'L1':<6}  {'created_at':<19}  preview")
        click.echo("-" * 100)
        for r in rows:
            l1 = sess.get(L1Result, r.id)
            l1_status = "—" if l1 is None else ("ERR" if l1.error else "OK")
            preview = r.content_text.replace("\n", " ")[:60]
            click.echo(
                f"{r.id:>4}  {r.source_type[:28]:<28}  {l1_status:<6}  "
                f"{r.created_at:%Y-%m-%d %H:%M:%S}  {preview}"
            )


@main.command("raw-show")
@click.argument("raw_id", type=int)
def raw_show(raw_id: int) -> None:
    """看一条 raw + 它的 L1 结果。"""
    from helper.config import get_settings
    from helper.storage import init_engine, session
    from helper.storage.models import L1Result, RawInput

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    with session() as sess:
        raw = sess.get(RawInput, raw_id)
        if raw is None:
            click.echo(f"raw#{raw_id} not found", err=True)
            raise SystemExit(1)
        click.echo(f"raw#{raw.id}")
        click.echo(f"  source_type : {raw.source_type}")
        click.echo(f"  source_ref  : {raw.source_ref}")
        click.echo(f"  author      : {raw.author_domain}")
        click.echo(f"  created_at  : {raw.created_at}")
        click.echo(f"  processed   : {raw.processed}")
        # Wave IM 上下文(其它来源全为空,跳过显示)
        if raw.source_type.startswith("im_wave") or raw.chat_id or raw.wave_message_id:
            click.echo("  -- wave --")
            click.echo(f"  chat_id     : {raw.chat_id or '(单聊)'}")
            click.echo(f"  is_at_bot   : {raw.is_at_bot}")
            click.echo(f"  media_type  : {raw.media_type}")
            click.echo(f"  wave_msg_id : {raw.wave_message_id}")
            if raw.parent_message_id:
                click.echo(f"  reply_to    : {raw.parent_message_id}")
            if raw.thread_id:
                click.echo(f"  thread_id   : {raw.thread_id}")
            if raw.forward_from_user:
                click.echo(f"  forward_from: {raw.forward_from_user} (msg {raw.forward_from_message_id})")
        click.echo("  content_text:")
        click.echo("    " + raw.content_text.replace("\n", "\n    "))

        l1 = sess.get(L1Result, raw_id)
        if l1 is None:
            click.echo("\nL1: (not yet processed — try `helper l1-backfill`)")
            return
        if l1.error:
            click.echo(f"\nL1: ERROR — {l1.error}")
            return

    click.echo("")
    _print_l1_items(raw_id, model=l1.model)


@main.command("l1-backfill")
@click.option("--limit", default=50, show_default=True, type=int, help="单次最多处理多少条")
@click.option("--force-all", is_flag=True, default=False, help="所有非 filtered 的 raw 全部用当前 prompt 版本重抽 (会跑 LLM 调用)")
def l1_backfill(limit: int, force_all: bool) -> None:
    """对缺 L1 / L1 失败的 raw_inputs 重跑 L1。"""
    from helper.config import get_settings
    from helper.ingest import backfill_pending
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    done = backfill_pending(limit=limit, force_all=force_all)
    if not done:
        click.echo("nothing to backfill")
        return
    click.echo(f"backfilled {len(done)} raw_inputs: {done}")


@main.command("l1-purge-questions")
@click.option("--dry-run", is_flag=True, default=False, help="只列出要清的 raw, 不真改 DB")
@click.option("--limit", default=200, show_default=True, type=int)
def l1_purge_questions(dry_run: bool, limit: int) -> None:
    """一次性清"问句被 L1 抽成 section"的污染数据。

    扫: L1Result.error='' AND has L1Items AND raw.content_text 命中 prefilter.is_question。
    动作:
      - 删该 raw 的所有 L1Item
      - fts/vector 清掉 raw 自身索引和 l1 atom 索引
      - L1Result.error 改成 "purged:question"
      - raw.processed 保持 True (避免反复扫)

    本命令只针对历史污染, 走完后日常路径靠 prefilter.is_question 把入口堵上。
    """
    from helper.config import get_settings
    from helper.ingest.prefilter import is_question
    from helper.storage import fts as fts_idx
    from helper.storage import init_engine, session
    from helper.storage import vector as vec_idx
    from helper.storage.models import L1Item, L1Result, RawInput

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    from sqlalchemy import distinct, select

    candidates: list[tuple[int, str]] = []
    with session() as sess:
        # 候选: 有 L1Item 且 L1Result.error 空 (即抽出过原子) 的 raw
        stmt = (
            select(distinct(RawInput.id), RawInput.content_text)
            .join(L1Result, L1Result.raw_id == RawInput.id)
            .join(L1Item, L1Item.raw_id == RawInput.id)
            .where(L1Result.error == "")
            .order_by(RawInput.id.desc())
            .limit(limit)
        )
        for rid, text in sess.execute(stmt).all():
            if is_question(text or ""):
                candidates.append((rid, (text or "").strip()[:60]))

    if not candidates:
        click.echo("no question-polluted L1 found")
        return

    click.echo(f"found {len(candidates)} question raws with L1 items:")
    for rid, snip in candidates:
        click.echo(f"  raw#{rid}: {snip}")

    if dry_run:
        click.echo("(dry-run, no changes)")
        return

    purged = 0
    for rid, _snip in candidates:
        with session() as sess:
            # fts/vector 清掉
            try:
                fts_idx.delete_l1_atoms_for_raw(sess, rid)
            except Exception:  # noqa: BLE001
                click.echo(f"  raw#{rid}: fts.delete_l1_atoms warn (skipped)")
            try:
                vec_idx.delete_l1_atoms_for_raw(sess, rid)
            except Exception:  # noqa: BLE001
                click.echo(f"  raw#{rid}: vec.delete_l1_atoms warn (skipped)")
            try:
                fts_idx.delete(sess, kind="raw", ref=str(rid))
            except Exception:  # noqa: BLE001
                pass
            try:
                vec_idx.delete(sess, kind="raw", ref=str(rid))
            except Exception:  # noqa: BLE001
                pass
            # 删 L1Item, 改 L1Result
            from sqlalchemy import delete as _delete
            sess.execute(_delete(L1Item).where(L1Item.raw_id == rid))
            lr = sess.get(L1Result, rid)
            if lr is not None:
                lr.error = "purged:question"
                lr.model = "purge"
            sess.commit()
        purged += 1
    click.echo(f"purged {purged} raws")


@main.command("l1-purge-bot-replies")
@click.option("--dry-run", is_flag=True, default=False, help="只列出要清的 raw, 不真改 DB")
@click.option("--limit", default=500, show_default=True, type=int)
def l1_purge_bot_replies(dry_run: bool, limit: int) -> None:
    """一次性清 "bot 自答被 L1 抽成 section / 进 fts/vector 召回池" 的污染。

    扫: source_type LIKE 'im_wave_bot%' AND (有 L1Items OR fts/vector 有 raw 索引)。
    动作:
      - 删 L1Item
      - fts/vector 清 raw kind + l1 atom kind 索引
      - L1Result.error 改成 "purged:bot_reply" (没有就建一条占位)
      - raw 本身保留 (上下文 / 引用反查仍要用)

    日常路径靠 _persist_bot_reply 写 skipped:bot_reply + 召回侧 retrieve 硬隔离
    (im_wave_bot* 整类不进 hits) 双重保险, 这条 CLI 只针对历史脏数据。
    """
    from helper.config import get_settings
    from helper.storage import fts as fts_idx
    from helper.storage import init_engine, session
    from helper.storage import vector as vec_idx
    from helper.storage.models import L1Item, L1Result, RawInput

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    from sqlalchemy import select

    with session() as sess:
        rows = sess.execute(
            select(RawInput.id, RawInput.content_text)
            .where(RawInput.source_type.like("im_wave_bot%"))
            .order_by(RawInput.id.desc())
            .limit(limit)
        ).all()

    if not rows:
        click.echo("no im_wave_bot raws found")
        return

    click.echo(f"found {len(rows)} im_wave_bot raws to clean indices:")
    for rid, text in rows[:10]:
        snip = (text or "").strip()[:60].replace("\n", " ")
        click.echo(f"  raw#{rid}: {snip}")
    if len(rows) > 10:
        click.echo(f"  ... and {len(rows) - 10} more")

    if dry_run:
        click.echo("(dry-run, no changes)")
        return

    from sqlalchemy import delete as _delete
    purged = 0
    for rid, _text in rows:
        with session() as sess:
            try:
                fts_idx.delete_l1_atoms_for_raw(sess, rid)
            except Exception:  # noqa: BLE001
                pass
            try:
                vec_idx.delete_l1_atoms_for_raw(sess, rid)
            except Exception:  # noqa: BLE001
                pass
            try:
                fts_idx.delete(sess, kind="raw", ref=str(rid))
            except Exception:  # noqa: BLE001
                pass
            try:
                vec_idx.delete(sess, kind="raw", ref=str(rid))
            except Exception:  # noqa: BLE001
                pass
            sess.execute(_delete(L1Item).where(L1Item.raw_id == rid))
            lr = sess.get(L1Result, rid)
            if lr is None:
                sess.add(L1Result(raw_id=rid, error="purged:bot_reply", model="purge"))
            else:
                lr.error = "purged:bot_reply"
                lr.model = "purge"
            sess.commit()
        purged += 1
    click.echo(f"purged {purged} bot reply raws")


@main.command("wave-simulate")
@click.argument("text")
@click.option(
    "--mode",
    type=click.Choice(["dm", "group_at", "group_reply"]),
    default="dm",
    show_default=True,
    help="dm=单聊到 bot / group_at=群里@bot / group_reply=群里回复某条消息且@bot",
)
@click.option("--author", default="jiahe.xu", show_default=True, help="发送者域账号")
@click.option(
    "--chat-id", default="oc_simulated_chat_001", show_default=True, help="模拟群聊 chat_id"
)
@click.option("--reply-to", default="om_parent_001", show_default=True, help="group_reply 时引用的消息 ID")
@click.option("--no-l1", is_flag=True, default=False, help="只落 raw,不跑 L1(避免打 Athenai)")
def wave_simulate(text: str, mode: str, author: str, chat_id: str, reply_to: str, no_l1: bool) -> None:
    """本地模拟一条 Wave v2 消息事件,走 webhook handler 的字段抽取 + L1 链路。

    跳过签名/AES 解密(本地构造的明文 payload 直接喂解析函数);用真 Athenai 跑 L1。
    用来在没有 Wave 真凭据时验证: 6 字段抽取正确、L1 能跑通、raw-show 能看到完整上下文。
    """
    import json as _json

    from helper.config import get_settings
    from helper.im import wave_webhook as wh
    from helper.ingest import process_raw
    from helper.storage import init_engine, raw_store, session

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    our_app_id = s.wave_app_id or "cli_d172001413a848689fa9dbe1cf03eafa"

    # ---- 构造 v2 payload ----
    msg: dict = {
        "msg_id": f"om_sim_{author}_{int(__import__('time').time())}",
        "msg_type": "text",
        "content": _json.dumps({"text": text}, ensure_ascii=False),
        "mentions": [],
        "thread_id": "",
        "quote_msg_id": "",
        "recalled": False,
    }
    if mode in ("group_at", "group_reply"):
        msg["mentions"] = [{"id": our_app_id, "id_type": "app_id", "name": "helper"}]
    if mode == "group_reply":
        msg["quote_msg_id"] = reply_to

    receiver = (
        {"id": chat_id, "id_type": "chat_id"}
        if mode in ("group_at", "group_reply")
        else {"id": our_app_id, "id_type": "app_id"}
    )

    payload = {
        "schema": "1.0",
        "header": {
            "event_type": "im.msg.group.sent_v2" if mode != "dm" else "im.msg.direct.sent_v2",
            "event_id": f"sim_{__import__('uuid').uuid4().hex[:16]}",
            "app_id": our_app_id,
        },
        "event": {
            "message": msg,
            "sender": {
                "id": f"ou_{author}_simulated",
                "id_type": "union_id",
                "user_id": author,
                "tenant_id": s.wave_user_tenant_id,
            },
            "receiver": receiver,
        },
    }

    # ---- 走 wave_webhook 的字段抽取 ----
    extracted = wh._extract_message_text(payload)
    sender_id, sender_id_type = wh._extract_sender(payload)
    extracted_chat_id = wh._extract_chat_id(payload)
    is_at_bot = wh._is_at_bot(payload, our_app_id)
    msg_obj = wh._extract_message(payload) or {}
    event_type = payload["header"]["event_type"]
    event_id = payload["header"]["event_id"]

    with session() as sess:
        row = raw_store.append(
            sess,
            source_type=f"im_wave:{event_type}",
            source_ref=event_id,
            content_text=extracted if extracted is not None else _json.dumps(payload, ensure_ascii=False),
            author_domain=sender_id if sender_id_type == "user_id" else sender_id,
            attachments_json=_json.dumps(payload, ensure_ascii=False),
            chat_id=extracted_chat_id,
            is_at_bot=is_at_bot,
            parent_message_id=msg_obj.get("quote_msg_id", ""),
            thread_id=msg_obj.get("thread_id", ""),
            media_type=msg_obj.get("msg_type", ""),
            wave_message_id=msg_obj.get("msg_id", ""),
        )
        raw_id = row.id

    click.echo(f"raw#{raw_id} stored (mode={mode})")
    click.echo(
        f"  抽出: chat_id={extracted_chat_id or '(单聊)'} / is_at_bot={is_at_bot} / "
        f"media_type={msg_obj.get('msg_type', '')} / wave_msg_id={msg_obj.get('msg_id', '')}"
    )
    if msg_obj.get("quote_msg_id"):
        click.echo(f"  reply_to={msg_obj['quote_msg_id']}")

    if no_l1:
        click.echo("--no-l1 指定,跳过 L1。用 `helper raw-show <id>` 看落库结果")
        return

    # ---- 跑 L1 ----
    result = process_raw(raw_id)
    if result is None or result.error:
        click.echo(f"L1 ERROR: {result.error if result else 'process_raw returned None'}", err=True)
        raise SystemExit(1)
    click.echo("")
    _print_l1_items(raw_id, model=result.model)
    click.echo(f"\n看完整落库: helper raw-show {raw_id}")


@main.command("l1-dryrun")
@click.argument("raw_id", type=int)
@click.option(
    "--version", "-v", default="v2", show_default=True,
    type=click.Choice(["v1", "v2"]),
    help="L1 prompt 版本。v2=section+decision,v1=旧 5 类",
)
@click.option(
    "--instruction", "-i", default="",
    help="模拟用户随文档发的取舍指令(如『只读 xxx 部分』),作为 ## 用户附加说明 拼到 prompt 末尾",
)
def l1_dryrun(raw_id: int, version: str, instruction: str) -> None:
    """对指定 raw 用指定 prompt 版本跑 L1,**只打印,不写库**。

    用于在切换 prompt 前对比新老抽取结果。不动 l1_items / l1_results 表。
    """
    from helper.config import get_settings
    from helper.ingest.l1_structure import structure
    from helper.storage import init_engine, session
    from helper.storage.models import RawInput

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    with session() as sess:
        raw = sess.get(RawInput, raw_id)
        if raw is None:
            click.echo(f"raw#{raw_id} 不存在")
            raise SystemExit(1)
        text = raw.content_text or ""
        title = (raw.source_ref or "")[:40]

    click.echo(f"raw#{raw_id} ({title}) — {len(text)} chars, prompt={version}")
    if instruction.strip():
        click.echo(f"用户附加说明: {instruction.strip()}")
    click.echo("=" * 60)

    out = structure(text, prompt_version=version, user_instruction=instruction)
    if out.error:
        click.echo(f"ERROR: {out.error}")
        raise SystemExit(1)

    by_type: dict[str, int] = {}
    for it in out.items:
        by_type[it.type] = by_type.get(it.type, 0) + 1
    click.echo(f"抽出 {len(out.items)} 条: {by_type}")
    click.echo("-" * 60)

    for idx, it in enumerate(out.items):
        click.echo(f"\n[#{idx}] {it.type}")
        click.echo(
            "  "
            + json.dumps(it.payload, ensure_ascii=False, indent=2).replace("\n", "\n  ")
        )


@main.command("consume-l1")
@click.argument("raw_id", type=int)
def consume_l1(raw_id: int) -> None:
    """把指定 raw 的 L1Item 收口到 4 类候选(concept/fact/case/relation)。

    sink.process_raw 已经会自动调,这条命令用于手动重跑(比如改了 consumer 逻辑)。
    """
    from helper.cases import consume_case_items
    from helper.config import get_settings
    from helper.facts import consume_fact_items
    from helper.ontology import consume_concept_items, consume_relation_items
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    cs = consume_concept_items(raw_id)
    rs = consume_relation_items(raw_id)
    fs = consume_fact_items(raw_id)
    ks = consume_case_items(raw_id)
    click.echo(f"  concept   : {len(cs)}")
    click.echo(f"  relation  : {len(rs)}")
    click.echo(f"  fact      : {len(fs)}")
    click.echo(f"  case      : {len(ks)}")


@main.command("ontology-promote")
@click.option("--limit", default=50, type=int)
def ontology_promote(limit: int) -> None:
    """扫所有 candidate,把够格的晋升到 git。"""
    from helper.config import get_settings
    from helper.ontology import promote_eligible
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    out = promote_eligible(limit=limit)
    if not out:
        click.echo("nothing eligible to promote")
        return
    click.echo(f"promoted {len(out)}: {out}")


@main.command("ontology-maintain")
@click.option("--dry-run", is_flag=True, default=False)
def ontology_maintain(dry_run: bool) -> None:
    """周期体检: 合并近似 entity / 标记孤儿 / decay。"""
    from helper.config import get_settings
    from helper.ontology import run_maintenance
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    rep = run_maintenance(dry_run=dry_run)
    click.echo(f"merged: {rep.merged}")
    click.echo(f"archived orphans: {rep.archived_orphans}")
    click.echo(f"decayed promoted: {rep.decayed_promoted}")


@main.command("specgen-run")
def specgen_run() -> None:
    """L2 聚类 + 给每簇 draft 一条 candidate spec。"""
    from helper.config import get_settings
    from helper.specgen import cluster_l1_results, draft_spec_from_cluster
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    clusters = cluster_l1_results(min_cluster_size=2)
    if not clusters:
        click.echo("no clusters found")
        return
    click.echo(f"{len(clusters)} clusters (decision items)")
    for c in clusters[:10]:
        click.echo(f"  keys={c}")
        sc = draft_spec_from_cluster(c)
        if sc:
            click.echo(f"    → spec_candidate slug={sc.slug} title={sc.title}")


@main.command("knowledge-promote")
@click.option("--limit", default=100, type=int)
def knowledge_promote(limit: int) -> None:
    """扫所有候选(entity/relation/fact/case),把够格的全部晋升到 git。"""
    from helper.cases import promote_eligible as promote_cases
    from helper.config import get_settings
    from helper.facts import promote_eligible as promote_facts
    from helper.ontology import promote_eligible as promote_entities
    from helper.ontology import promote_eligible_relations
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    e = promote_entities(limit=limit)
    r = promote_eligible_relations(limit=limit)
    f = promote_facts(limit=limit)
    k = promote_cases(limit=limit)
    click.echo(f"  entities  : {len(e)} {e}")
    click.echo(f"  relations : {len(r)} {r}")
    click.echo(f"  facts     : {len(f)} {f}")
    click.echo(f"  cases     : {len(k)} {k}")


@main.command("spec-list")
@click.option("--status", default="pending", type=click.Choice(["pending", "approved", "rejected", "all"]))
def spec_list(status: str) -> None:
    """列 spec_candidates。"""
    from sqlalchemy import select

    from helper.config import get_settings
    from helper.storage import init_engine, session
    from helper.storage.models import SpecCandidate

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    with session() as sess:
        q = select(SpecCandidate).order_by(SpecCandidate.created_at.desc())
        if status != "all":
            q = q.where(SpecCandidate.review_status == status)
        rows = sess.execute(q).scalars().all()
        if not rows:
            click.echo("(empty)")
            return
        for r in rows:
            click.echo(f"  {r.slug:30s}  [{r.review_status}]  {r.title}")


@main.command("spec-promote")
@click.argument("slug")
@click.option("--reviewer", default="cli")
def spec_promote(slug: str, reviewer: str) -> None:
    """把 spec_candidate 标 approved 并落到 git。"""
    from helper.config import get_settings
    from helper.specgen import promote_spec
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    path = promote_spec(slug, reviewer=reviewer)
    if path is None:
        click.echo(f"not found: {slug}", err=True)
        raise SystemExit(1)
    click.echo(f"promoted → {path}")


@main.command("compile")
def compile_bundle() -> None:
    """git spec repo → bundle.json(给 ask runtime 用)。"""
    from helper.compiler import build_bundle
    from helper.config import get_settings
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    p = build_bundle()
    click.echo(f"bundle written: {p}")


@main.command()
@click.argument("question")
@click.option("--asker", default="cli", help="提问者域账号")
def ask(question: str, asker: str) -> None:
    """Ask runtime — 用当前 bundle 回答。"""
    import json as _json

    from helper.ask import ask as run_ask
    from helper.config import get_settings
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    ans = run_ask(question, asker_domain=asker)
    click.echo(f"answer:     {ans.answer}")
    click.echo(f"confidence: {ans.confidence}")
    click.echo(f"citations:  {_json.dumps(ans.citations, ensure_ascii=False)}")
    click.echo(f"bundle:     {ans.bundle_version}")
    click.echo(f"model:      {ans.model}")


@main.command("inquiry-evaluate")
@click.argument("raw_id", type=int)
def inquiry_evaluate(raw_id: int) -> None:
    """对一条 raw 跑追问策略评估。"""
    from helper.config import get_settings
    from helper.inquiry import evaluate_for_raw
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    hits = evaluate_for_raw(raw_id)
    if not hits:
        click.echo("(no inquiry triggered)")
        return
    for h in hits:
        click.echo(f"  [{h.strategy_id}] {h.question}")


@main.command("conflict-detect")
@click.argument("raw_id", type=int)
def conflict_detect(raw_id: int) -> None:
    """对一条 raw 检测冲突。"""
    from helper.config import get_settings
    from helper.conflict import detect_for_raw
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    hits = detect_for_raw(raw_id)
    if not hits:
        click.echo("(no conflict)")
        return
    for h in hits:
        click.echo(f"  #{h.log_id}  vs {h.target_type}={h.target_slug}  [{h.severity}]  {h.summary}")


@main.command("conflict-list")
@click.option("--status", default="open", type=click.Choice(["open", "superseded", "coexist", "rejected", "all"]))
def conflict_list(status: str) -> None:
    """列 conflict_log。"""
    from sqlalchemy import select

    from helper.config import get_settings
    from helper.storage import init_engine, session
    from helper.storage.models import ConflictLog

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    with session() as sess:
        q = select(ConflictLog).order_by(ConflictLog.created_at.desc())
        if status != "all":
            q = q.where(ConflictLog.resolution == status)
        rows = sess.execute(q).scalars().all()
        if not rows:
            click.echo("(empty)")
            return
        for r in rows:
            target = f"{r.target_type or 'spec'}={r.target_slug}"
            click.echo(f"  #{r.id}  raw={r.raw_id}  {target}  [{r.severity}]  {r.resolution}")
            click.echo(f"      {r.summary[:200]}")


@main.command("conflict-resolve")
@click.argument("log_id", type=int)
@click.option("--resolution", required=True, type=click.Choice(["superseded", "coexist", "rejected"]))
@click.option("--resolver", default="cli")
def conflict_resolve(log_id: int, resolution: str, resolver: str) -> None:
    """裁决一条 open 冲突。"""
    from helper.config import get_settings
    from helper.conflict import resolve as do_resolve
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    ok = do_resolve(log_id, resolution=resolution, resolver_domain=resolver)
    click.echo("OK" if ok else "not found")
    if not ok:
        raise SystemExit(1)


@main.command("inbox-weekly")
@click.option("--receiver", help="若给了就直接 send 到 IM,否则只打印")
@click.option("--receiver-id-type", default="user_id")
@click.option("--snapshot-owner", default="", help="把 1-N/2-N/3-N → ID 映射存进 inbox_digest(默认走 settings.helper_owner_domain)")
def inbox_weekly(receiver: str, receiver_id_type: str, snapshot_owner: str) -> None:
    """构建当周 digest;给 receiver 就发出去。

    --receiver 走 send_to 会自动落 InboxDigest 快照(用 owner 域账号反查)。
    本地预览也想存快照,用 --snapshot-owner=jiahe.xu 显式指定。
    """
    from helper.config import get_settings
    from helper.inbox import build_digest, render_card, send_to, snapshot_digest
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    if receiver:
        ok = send_to(receiver, receiver_id_type=receiver_id_type)
        click.echo("sent" if ok else "send failed")
        return
    d = build_digest()
    click.echo(render_card(d))
    owner = snapshot_owner or get_settings().helper_owner_domain
    if owner:
        snapshot_digest(owner, d)
        click.echo(f"\n(snapshot saved for owner={owner})")


@main.command("batch-ingest")
@click.argument("path", type=click.Path(exists=True))
@click.option("--author", default="batch")
@click.option("--no-l1", is_flag=True, default=False)
def batch_ingest(path: str, author: str, no_l1: bool) -> None:
    """批量 ingest 单个文件(.md / .txt / .json)。"""
    from helper.batch import ingest_file
    from helper.config import get_settings
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    res = ingest_file(path, author=author, run_l1=not no_l1)
    click.echo(f"file: {res.file}")
    click.echo(f"units total/with_decision: {res.units_total} / {res.units_with_decision}")
    click.echo(f"raw_ids: {res.raw_ids}")


@main.command("replay")
@click.option("--limit", default=50, type=int)
@click.option("--judge", is_flag=True, default=False, help="用 LLM 比较新旧版本好坏")
def replay(limit: int, judge: bool) -> None:
    """把 ask_answers 历史问题用当前 bundle 重跑。"""
    import json as _json

    from helper.config import get_settings
    from helper.eval import compare_versions, replay_all
    from helper.storage import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    if judge:
        rep = compare_versions(limit=limit)
        click.echo(_json.dumps(rep, ensure_ascii=False, indent=2))
        return
    items = replay_all(limit=limit)
    for it in items:
        click.echo(f"\nQ: {it.question[:80]}")
        click.echo(f"  old ({it.original_version}, {it.original_confidence}): {it.original_answer[:120]}")
        click.echo(f"  new ({it.new_version}, {it.new_confidence}): {it.new_answer[:120]}")


@main.command("reindex")
@click.option("--clear", is_flag=True, default=False, help="先清空 vec_items + vector_index + fts_items 再全量重建")
@click.option(
    "--kinds",
    default="raw,spec,entity,section,decision",
    show_default=True,
    help="逗号分隔,只重建这些 kind",
)
def reindex(clear: bool, kinds: str) -> None:
    """全量重建向量 + FTS 索引。换 embedding 模型 / 修复脏数据 / 接通新 kind 时用。

    默认增量:已 index 且 (content_hash, model) 没变的不动。
    --clear 先全清再重建,适合换模型时。
    """
    from sqlalchemy import select

    from helper.config import get_settings
    from helper.storage import fts, init_engine, session
    from helper.storage import vector as vec
    from helper.storage.models import EntityCandidate, L1Item, L1Result, SpecCandidate

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")
    selected = {k.strip() for k in kinds.split(",") if k.strip()}

    if clear:
        with session() as sess:
            vec.clear_all(sess)
            fts.clear_all(sess)
        click.echo("vec_items + vector_index + fts_items cleared")

    counts = {"raw": 0, "spec": 0, "entity": 0, "section": 0, "decision": 0}
    failed = {"raw": 0, "spec": 0, "entity": 0, "section": 0, "decision": 0}

    if "raw" in selected:
        with session() as sess:
            ok_l1 = sess.execute(
                select(L1Result.raw_id).where(L1Result.error == "")
            ).scalars().all()
        for rid in ok_l1:
            with session() as sess:
                rowid = vec.index_raw(sess, rid)
                fts.index_raw(sess, rid)
            if rowid is not None:
                counts["raw"] += 1
            else:
                failed["raw"] += 1

    if "spec" in selected:
        with session() as sess:
            spec_slugs = sess.execute(
                select(SpecCandidate.slug).where(SpecCandidate.review_status == "approved")
            ).scalars().all()
        for slug in spec_slugs:
            with session() as sess:
                rowid = vec.index_spec(sess, slug)
                fts.index_spec(sess, slug)
            if rowid is not None:
                counts["spec"] += 1
            else:
                failed["spec"] += 1

    if "entity" in selected:
        with session() as sess:
            ent_slugs = sess.execute(
                select(EntityCandidate.slug).where(EntityCandidate.promoted_at.isnot(None))
            ).scalars().all()
        for slug in ent_slugs:
            with session() as sess:
                rowid = vec.index_entity(sess, slug)
                fts.index_entity(sess, slug)
            if rowid is not None:
                counts["entity"] += 1
            else:
                failed["entity"] += 1

    # section / decision: 走 l1_items 全量
    for atom_kind in ("section", "decision"):
        if atom_kind not in selected:
            continue
        with session() as sess:
            pairs = sess.execute(
                select(L1Item.raw_id, L1Item.idx).where(L1Item.type == atom_kind)
            ).all()
        for raw_id, idx in pairs:
            with session() as sess:
                rowid = vec.index_l1_atom(sess, raw_id, idx)
                fts.index_l1_atom(sess, raw_id, idx)
            if rowid is not None:
                counts[atom_kind] += 1
            else:
                failed[atom_kind] += 1

    click.echo(
        f"indexed: raw={counts['raw']} spec={counts['spec']} entity={counts['entity']} "
        f"section={counts['section']} decision={counts['decision']}"
    )
    if any(failed.values()):
        click.echo(
            f"  (failures: raw={failed['raw']} spec={failed['spec']} entity={failed['entity']} "
            f"section={failed['section']} decision={failed['decision']})",
            err=True,
        )


@main.command("acl-backfill")
@click.option("--max-id", type=int, default=None, help="只处理 id ≤ max_id 的 raw, 调试用")
def acl_backfill(max_id: int | None) -> None:
    """对所有现存 raw 跑 ACL 打标 (M8)。

    新 raw 入库会自动打标; 这条命令负责对已存在的存量数据补打。
    幂等: 重跑会覆盖上次结果。
    """
    from helper.acl import backfill_all
    from helper.config import get_settings
    from helper.storage.db import init_engine

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    n = backfill_all(max_id=max_id)
    click.echo(f"acl-backfill: scanned {n} raw rows")


@main.command("acl-status")
def acl_status() -> None:
    """查 ACL 打标分布(每个 topic 多少 raw / atom 命中)。"""
    from sqlalchemy import func, select

    from helper.acl import current_acl
    from helper.config import get_settings
    from helper.storage import session
    from helper.storage.db import init_engine
    from helper.storage.models import (
        CaseCandidate, EntityCandidate, FactCandidate, L1Item,
        RawInput, RelationCandidate,
    )

    s = get_settings()
    init_engine(s.helper_data_dir / "helper.db")

    acl = current_acl()
    click.echo(f"ACL yaml version: {acl.version}")
    click.echo(f"  default_on_uncertain: {acl.default_on_uncertain!r}")
    click.echo(f"  topics: {[t.id for t in acl.topics]}")

    with session() as sess:
        for label, model in (
            ("raw_inputs", RawInput),
            ("l1_items", L1Item),
            ("entity_candidates", EntityCandidate),
            ("fact_candidates", FactCandidate),
            ("case_candidates", CaseCandidate),
            ("relation_candidates", RelationCandidate),
        ):
            rows = sess.execute(
                select(model.acl_topic_id, func.count())
                .group_by(model.acl_topic_id)
            ).all()
            dist = {tid or "(public)": cnt for tid, cnt in rows}
            click.echo(f"{label}: {dist}")


if __name__ == "__main__":
    main()
