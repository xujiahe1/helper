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


class ConflictLog(Base):
    """新输入与已有 spec 冲突 — 等用户/专家裁决。"""

    __tablename__ = "conflict_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_inputs.id", ondelete="CASCADE")
    )
    spec_slug: Mapped[str] = mapped_column(String(128))  # 与之冲突的已有 spec
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


class L1Result(Base):
    """L1 结构化结果。一条 raw_input 至多一条 L1Result(以 raw_id 为主键),
    重跑 = upsert。失败也写一条记录,error 字段非空 → 下次手动 backfill 才重试。
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
