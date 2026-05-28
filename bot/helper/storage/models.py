"""SQLite ORM models. SQLite 仅放运行时易变数据(raw input / dedup / cache),
权威决策规约一律走 git spec repo,见 docs/architecture.md §9。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RawInput(Base):
    """毛坯输入。事件溯源唯一权威源,所有派生(L1/L2/spec)都基于它重建。

    Wave-IM 字段(私聊+群+@bot+回复+转发+多媒体): 全部直接展平在主表上,
    入站时一次抽完写完,L1/L2/检索都直接读列,不再到 attachments_json 里二次解析。
    Web/CLI 等其它来源对应字段留空。
    """

    __tablename__ = "raw_inputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32))  # im_at / im_forward / doc / voice / cli
    source_ref: Mapped[str] = mapped_column(String(255), default="")  # 如 wave chat_id:msg_id
    author_domain: Mapped[str] = mapped_column(String(64), default="")
    content_text: Mapped[str] = mapped_column(Text)
    attachments_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)

    # ---- Wave IM 上下文(非 IM 来源全部留空) ----
    # 会话定位: chat_id 群聊 / 单聊与 bot 时空串。区分"我说的"vs"群里抓的"
    chat_id: Mapped[str] = mapped_column(String(64), default="")
    # 是否真的 @ 了 bot(单聊默认视为 True;群聊看 mentions 里有没有 app_id=本 bot 或 id_type=all)
    is_at_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    # 转发场景: Wave 没有"转发原说话人"的协议字段,但用户可能手动复述
    # ("以下是 X 的话: ...")。把转发上下文用空串占位,后续再用 LLM 抽
    forward_from_user: Mapped[str] = mapped_column(String(64), default="")
    forward_from_message_id: Mapped[str] = mapped_column(String(64), default="")
    # 回复关系: quote_msg_id = 直接被引用的那条;thread_id = 话题根
    parent_message_id: Mapped[str] = mapped_column(String(64), default="")
    thread_id: Mapped[str] = mapped_column(String(64), default="")
    # text / rich_text / image / video / file / card / markdown / ...
    media_type: Mapped[str] = mapped_column(String(32), default="")
    # Wave 自己的会话消息 ID(om_xxx),34 字符,与回调 header.event_id 不是一回事
    wave_message_id: Mapped[str] = mapped_column(String(64), default="")

    # 业务层去重: (chat_id, wave_message_id) 唯一。
    # 部分索引避开 web/cli 来源(wave_message_id 为空串),只对 IM 行生效。
    # 私聊 chat_id 也是空串,但 Wave msg_id 全平台唯一,(chat_id="", wave_message_id) 仍唯一。
    __table_args__ = (
        Index(
            "uq_raw_inputs_im_msg",
            "chat_id",
            "wave_message_id",
            unique=True,
            sqlite_where=text("wave_message_id <> ''"),
        ),
    )


class IdentityCache(Base):
    """Wave union_id/user_id ↔ 域账号 + 姓名 缓存。

    身份信息一次性走 Wave /openapi/contact/v1/users/get 拿全(域账号 + name),
    避免每次入站都打外部接口。不对接 IAM——Wave API 直接给得到。
    """

    __tablename__ = "identity_cache"

    wave_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    domain_account: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), default="")
    en_name: Mapped[str] = mapped_column(String(128), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class WaveEventDedup(Base):
    """Wave 回调 event_id 去重(7.1h 窗口内)。"""

    __tablename__ = "wave_event_dedup"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class EntityCandidate(Base):
    """Ontology 涌现层 — 从 L1 signals 抽出的 entity 候选。

    阈值未到的留在 sqlite,达标晋升到 git ontology/entities/<slug>.md。
    晋升后该行 promoted_at 非空,作为"已转正"标记;后续仍可被 raw 增量引用。
    """

    __tablename__ = "entity_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)  # 规范化名(小写下划线)
    name: Mapped[str] = mapped_column(String(255))               # 人类可读
    entity_type: Mapped[str] = mapped_column(String(64), default="decision_concept")
    description: Mapped[str] = mapped_column(Text, default="")
    raw_refs_json: Mapped[str] = mapped_column(Text, default="[]")  # raw_id 列表
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    git_path: Mapped[str] = mapped_column(String(255), default="")  # ontology/entities/<slug>.md
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 取代它的 raw_id


class SpecCandidate(Base):
    """L2 聚类产物 — N 条 L1 结果聚成的候选 spec。

    人 review 后通过 promoter 落到 git specs/<slug>.md。
    """

    __tablename__ = "spec_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    statement: Mapped[str] = mapped_column(Text)               # 一句话决策规则
    rationale: Mapped[str] = mapped_column(Text, default="")
    cluster_raw_ids_json: Mapped[str] = mapped_column(Text, default="[]")  # 支撑 raw 列表
    review_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    git_path: Mapped[str] = mapped_column(String(255), default="")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 取代它的 raw_id


class ConflictLog(Base):
    """新输入与已有(spec / fact / case / concept / relation)冲突 — 等用户/专家裁决。

    target_type ∈ {spec, fact, case, concept, relation}:决定后续 resolve 时
    要去哪张候选表打 superseded_at 标记。target_slug 是被冲突候选的 slug。

    历史列名 spec_slug 已映射成 target_slug(改属性不改物理列,旧数据自动归档为 type=spec),
    由 _backfill_missing_columns 把 target_type 列补出来,默认 'spec'。
    """

    __tablename__ = "conflict_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_inputs.id", ondelete="CASCADE")
    )
    target_type: Mapped[str] = mapped_column(String(16), default="spec")
    # 物理列保留 spec_slug,逻辑名 target_slug。
    target_slug: Mapped[str] = mapped_column("spec_slug", String(255))
    summary: Mapped[str] = mapped_column(Text)           # LLM judge 给的冲突摘要
    severity: Mapped[str] = mapped_column(String(16), default="medium")  # low/medium/high
    resolution: Mapped[str] = mapped_column(String(16), default="open")  # open/superseded/coexist/rejected
    resolved_by: Mapped[str] = mapped_column(String(64), default="")     # 域账号
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class InquiryLog(Base):
    """追问触发 + 命中率打标 — M2 灵魂模块。"""

    __tablename__ = "inquiry_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_inputs.id", ondelete="CASCADE")
    )
    strategy_id: Mapped[str] = mapped_column(String(64))  # 命中的策略 yaml id
    question: Mapped[str] = mapped_column(Text)           # 实际问出的问题
    answer_raw_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 用户回答 → 新 raw
    hit: Mapped[str] = mapped_column(String(16), default="unknown")  # yes/no/unknown
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ReactionLog(Base):
    """im.msg.feedback.action_v1 事件落库。

    用户对 bot 回复点 👍/👎 触发,**同 (operator_id, msg_id) 联合主键**做覆盖更新
    (KM 文档建议:不要做加减统计,直接以最新状态为准)。
    """

    __tablename__ = "reaction_log"

    operator_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    msg_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operator_id_type: Mapped[str] = mapped_column(String(16), default="union_id")
    operator_user_id: Mapped[str] = mapped_column(String(64), default="")
    action_type: Mapped[str] = mapped_column(String(32))  # like/dislike/cancel_like/cancel_dislike
    related_ask_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 这条 bot 消息对应哪次 Ask
    action_time: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AskAnswer(Base):
    """Ask runtime 一次问答的留存 — 用于引用追溯 + Replay/Eval。"""

    __tablename__ = "ask_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asker_domain: Mapped[str] = mapped_column(String(64), default="")
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), default="unknown")  # high/medium/low/unknown
    citations_json: Mapped[str] = mapped_column(Text, default="[]")   # [{type:raw|spec, ref:N|slug}]
    spec_bundle_version: Mapped[str] = mapped_column(String(64), default="")  # git commit
    model: Mapped[str] = mapped_column(String(64), default="")
    wave_msg_id: Mapped[str] = mapped_column(String(64), default="")  # bot 回复消息 id (供 reaction 反查)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ScheduledTask(Base):
    """用户通过对话创建的定时任务。bot 进程内 APScheduler 每分钟扫这张表。

    设计:
    - cron_expr 用 5 字段标准 cron("min hour dom mon dow"),APScheduler CronTrigger.from_crontab 解析
    - task_type: periodic_ask(MVP 唯一支持) / weekly_report / monthly_report / spec_freshness(后续)
    - params_json: 任务参数 JSON 字符串。periodic_ask 形如 {"question": "本周项目进展如何"}
    - receiver_id / receiver_id_type: 任务执行结果发给谁。MVP 限定 = 创建者本人(user_id)
    - enabled=False 是软删,保留历史可审计
    - last_run_at: 幂等 — 同一分钟扫到不重跑
    - cancel 走改 enabled,不删行,留 audit log
    """

    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[str] = mapped_column(String(64))  # 创建者域账号
    cron_expr: Mapped[str] = mapped_column(String(64))      # "0 9 * * 1" 形式
    task_type: Mapped[str] = mapped_column(String(32))      # periodic_ask / ...
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    receiver_id: Mapped[str] = mapped_column(String(64))    # MVP = owner_user_id
    receiver_id_type: Mapped[str] = mapped_column(String(16), default="user_id")
    summary: Mapped[str] = mapped_column(Text, default="")  # 给用户看的人类可读描述
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ScheduleConfirm(Base):
    """待用户复述确认的解析结果。每个 user_id 同一时刻只允许一个待确认。

    用户说创建定时任务 → bot 解析 → 写入此表 → bot 复述要求 yes/no。
    用户回 yes → 提交到 scheduled_tasks + 删本行;
    回 no/改成 X → 让用户重新描述,删本行;
    超时 5 分钟自动作废。
    """

    __tablename__ = "schedule_confirm"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 一人一时刻一个
    cron_expr: Mapped[str] = mapped_column(String(64))
    task_type: Mapped[str] = mapped_column(String(32))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    receiver_id: Mapped[str] = mapped_column(String(64))
    receiver_id_type: Mapped[str] = mapped_column(String(16), default="user_id")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class InboxDigest(Base):
    """最近一次发给某 owner 的 inbox 周报快照 — 用于把 1-N/2-N/3-N 解析回真实 ID。

    每次 send_to(owner) 或主动 /inbox 时 upsert 一行(owner_domain 主键),
    items_json 形如:
      {
        "specs":     [spec_candidate_id, ...],          # 1-N → specs[N-1]
        "conflicts": [conflict_log_id, ...],            # 2-N → conflicts[N-1]
        "inquiries": [inquiry_log_id, ...],             # 3-N → inquiries[N-1]
      }
    回执处理时按编号 1-based 取下标。owner 同时只保留最新一份。
    """

    __tablename__ = "inbox_digest"

    owner_domain: Mapped[str] = mapped_column(String(64), primary_key=True)
    items_json: Mapped[str] = mapped_column(Text, default="{}")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class VectorIndex(Base):
    """vec0 表的 sidecar 元信息 — 把"vec0 rowid"对回业务对象 (kind, ref)。

    设计:
    - vec_items (virtual table, 见 db.py) 只放 rowid + 1024 维 embedding,vec_items 不能加普通列
    - 这张普通表存:vec_items.rowid → (kind, ref, content_hash, model, indexed_at)
    - (kind, ref) 唯一索引 → 同一对象重新 index 时找回 rowid,更新 vec_items 那一行 + 这张表的 hash/time
    - content_hash 用于 dedup:文本没变就跳过 LLM 调用
    - model 字段记录索引时用的 embedding model,换模型时可以选择性 reindex

    kind ∈ {'spec', 'raw', 'entity'};ref 是 spec slug / raw_id 字符串 / entity slug。
    """

    __tablename__ = "vector_index"

    rowid: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("uq_vector_kind_ref", "kind", "ref", unique=True),
        Index("ix_vector_kind", "kind"),
    )


class L1Result(Base):
    """L1 抽取的 raw 级元信息(成功/失败/模型)。

    实际抽出的知识原子在 L1Item 表里(0..N 条 / raw)。
    legacy 字段 scene/signals_json/tradeoffs_json/choice/rationale 保留在 sqlite
    里(不读)— SQLite 不支持 DROP COLUMN,新代码不再写入也不再读取。
    """

    __tablename__ = "l1_results"

    raw_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_inputs.id", ondelete="CASCADE"), primary_key=True
    )
    scene: Mapped[str] = mapped_column(Text, default="")
    signals_json: Mapped[str] = mapped_column(Text, default="[]")
    tradeoffs_json: Mapped[str] = mapped_column(Text, default="[]")
    choice: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class L1Item(Base):
    """L1 抽取出的单条知识原子。一条 raw 出 0..N 条 L1Item。

    type ∈ {decision, fact, case, concept, relation}。
    payload_json 是 type-specific 字段:
      decision: {scene, signals[], tradeoffs[], choice, rationale}
      fact:     {subject, predicate, object, scope}
      case:     {scene, what_happened, outcome, referenced_spec?}
      concept:  {name, entity_type, description}
      relation: {entity_a, relation, entity_b}

    复合主键 (raw_id, idx) — 重跑 raw 时 sink 先 DELETE 同 raw_id 全部行再写,保证幂等。
    """

    __tablename__ = "l1_items"

    raw_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_inputs.id", ondelete="CASCADE"), primary_key=True
    )
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_l1_items_type", "type"),
    )


class FactCandidate(Base):
    """决策性事实候选 — 主谓宾 + 适用范围。

    fact 与 entity 相比是一句陈述(有谓词);与 spec 相比无"决策选择"。
    达晋升阈值 → facts/<slug>.md。
    """

    __tablename__ = "fact_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    statement: Mapped[str] = mapped_column(Text)         # 一句话陈述,人类可读
    subject: Mapped[str] = mapped_column(String(255), default="")
    predicate: Mapped[str] = mapped_column(String(255), default="")
    object: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(Text, default="")
    raw_refs_json: Mapped[str] = mapped_column(Text, default="[]")  # [[raw_id, idx], ...]
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    git_path: Mapped[str] = mapped_column(String(255), default="")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CaseCandidate(Base):
    """反例 / 决策案例候选 — 一个具体场景里发生了什么、结果如何。

    case 是 episode 级别 — 通常 1 case = 1 文件,不需要聚类,但允许 mention_count
    去重("同一案例被多人复述")。达阈值 → cases/<slug>.md。
    """

    __tablename__ = "case_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    scene: Mapped[str] = mapped_column(Text, default="")
    what_happened: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(Text, default="")
    referenced_spec: Mapped[str] = mapped_column(String(128), default="")  # 触发的 spec slug,可空
    raw_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    git_path: Mapped[str] = mapped_column(String(255), default="")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class RelationCandidate(Base):
    """实体关系候选 — (entity_a, relation, entity_b)。

    达晋升阈值 → ontology/relationships/<slug>.md。
    slug = f"{a_slug}__{relation}__{b_slug}"。
    """

    __tablename__ = "relation_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True)
    entity_a: Mapped[str] = mapped_column(String(128))   # entity slug 或自由文本
    relation: Mapped[str] = mapped_column(String(64))    # 谓词:has_one / supersedes / part_of / ...
    entity_b: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    raw_refs_json: Mapped[str] = mapped_column(Text, default="[]")
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    git_path: Mapped[str] = mapped_column(String(255), default="")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
