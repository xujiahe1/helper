"""SQLite ORM models. SQLite 仅放运行时易变数据(raw input / dedup / cache),
权威决策规约一律走 git spec repo,见 docs/architecture.md §9。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, text
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

    # ---- Topic ACL(M8): 内容打标, 非白名单用户问到带标的内容直接装作不知道。
    # 空串 = 公开可见; 非空 = 命中 topic_acl.yaml 里某个 topic.id, 仅 allowed_domains 可见。
    # 派生层(L1Item) 在各自表上也冗余这一列, retrieve 阶段直接列过滤, 不 join。
    acl_topic_id: Mapped[str] = mapped_column(String(32), default="")

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
    # 改动 3: 来源 SpecTopic 编号; 老 candidate (改动 3 之前手工聚簇生成的) 留 None。
    # 软关联, 不强 FK — topic 删除时不级联清掉 candidate。
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class SpecTopic(Base):
    """改动 3: 语义聚类簇 — bge-m3 embedding + 余弦阈值 0.78 自动聚簇。

    每条 type='decision' 的 L1Item 在 sink 阶段算 embedding (复用 helper.embed),
    随后异步 assign_topic 与已有 topic 做余弦比较, 命中阈值合入旧簇 (centroid 增量
    平均更新), 否则新建一个 topic。

    主链路触发 draft 由 `scan_topics_for_draft` 周期性扫这张表决定 (普适 / 饱和≥3 /
    静默期 ≥ 90d), 替代旧的 "调用方自传 cluster_keys" 模式。

    centroid: fp16 packed 1024d (2048 字节), 与 Memory.embedding 同编码;
              空 bytes 表示尚未计算 (例如新建中途出错)。
    """

    __tablename__ = "spec_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    centroid: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    decision_count: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    last_promoted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


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
    # open/superseded/coexist/rejected/auto_superseded/auto_rejected/auto_coexist
    resolution: Mapped[str] = mapped_column(String(16), default="open")
    resolved_by: Mapped[str] = mapped_column(String(64), default="")     # 域账号 / auto-judge
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auto_reason: Mapped[str] = mapped_column(Text, default="")           # auto_* 时的裁决理由
    # 已 approved spec 的"待裁决覆写"场景: 新草稿不直接落 SpecCandidate, 而是塞这里
    # 等 owner 在周报里裁决后, 再用这里的 payload 决定覆写 / 丢弃 / -v2 旁路。
    # 普通 conflict 这字段为空。 JSON 形如 {"slug","title","statement","rationale","keys"}。
    pending_payload_json: Mapped[str] = mapped_column(Text, default="")
    # 同义疑似冲突场景: 存 "name_a||name_b", resolve 时回写 entity_alias 表。
    # 普通 conflict (精确撞 scope / spec 撞 slug) 此字段为空。
    alias_hint: Mapped[str] = mapped_column(Text, default="")
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

    二分 type:
      section:  {title, body, topics[], entities[]}  ← 语义独立单元,原文保留
      decision: {scene, signals[], tradeoffs[], choice, rationale,
                 source_raw_ids?, primary_raw_id?}

    归属人(谁说的)统一靠 raw_inputs.author_domain 反查 — payload 里不存 speaker 字段。

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
    acl_topic_id: Mapped[str] = mapped_column(String(32), default="")  # 见 RawInput.acl_topic_id
    # 改动 3: type='decision' 的 item 在 sink 阶段算 bge-m3 embedding (1024d fp16, 2048 字节);
    # 其它 type 留空 bytes (省成本)。 失败 / 未算 → b"", 不影响主链路。
    embedding: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    # 改动 3: 归属 SpecTopic.id, 未归簇 = None (老数据 / 非 decision / embed 失败)。
    topic_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    __table_args__ = (
        Index("ix_l1_items_type", "type"),
    )


class Memory(Base):
    """Procedural memory — 用户对 bot 行为的指令(M5)。

    与 semantic 原子(section / decision)正交:
    - section / decision 是"描述世界",进 retrieve 给 LLM 当素材
    - 这个是"约束 bot 行为",进 ask 的 SYSTEM_PROMPT 当指令

    全公司共享(任何人能写,后写覆盖,撤销显式触发);冲突复用 ConflictLog
    target_type='memory' 走 inbox 周报三段式裁决,不另起机制。
    """

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String(16), default="global")  # entity/global
    scope_ref: Mapped[str] = mapped_column(String(128), default="")        # entity slug;global 时空
    directive: Mapped[str] = mapped_column(Text)                           # 指令文本(给 LLM 读, 不含 cli_xxx hash)
    # 路由结构数据 — 这条 directive 触发路由时的目标 bot app_id。
    # 单独存而不写进 directive 文本: hash 不该进 LLM 视野(否则 LLM 会复述给用户),
    # 也避免 LLM 从 directive 文本里抄出错的 app_id; 路由动作内部按 scope_ref(entity 名)
    # 反查这一列拿真 hash。无路由意图的 directive 留空。
    route_app_id: Mapped[str] = mapped_column(String(64), default="")
    source_raw_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("raw_inputs.id", ondelete="SET NULL"), nullable=True
    )
    author_domain: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 取代它的 memory id
    # memory_audit 任务最近一次审过这条 directive 的时间。null = 从没审过(首次审优先)。
    # 7 天节流: audit.run_if_needed() 只重审 last_audited_at 超过 7 天或为空的 alive memory。
    # superseded_by=0 表示被 audit 自动 supersede(非 raw 来源,人工裁决/conflict resolve 的
    # superseded_by 永远是真实 raw_id,不会为 0)。
    last_audited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # directive 文本的 bge-m3 1024 维向量, fp16 编码 (2048 字节)。
    # 跨 scope 语义相似 fallback 用: scope 不同但向量余弦 ≥ 0.85 → 挂"同义疑似"冲突。
    # 空 bytes / None 表示没算 (老数据未 backfill 或 embed 失败), 语义 fallback 直接跳过这条。
    embedding: Mapped[bytes] = mapped_column(LargeBinary, default=b"")


class EntityAlias(Base):
    """同义实体表 — 把同一个人/对象的不同名字归到一个 canonical 主名。

    用法: 任何 scope_ref 落库前先经过 resolve_alias(name) → canonical_name,
    Memory 表里只存归一后的名字。 这样 "小猫老师" / "周婷" / "陈雨晴" 这种
    "同一个人三个叫法" 不会在 Memory 表里产生三条独立 alive directive。

    每行表示一个名字 → 主名映射:
      ('小猫老师', '周婷', 'manual', ...)
      ('周婷',    '周婷', 'manual', ...)   ← 主名自映射, 方便统一查
      ('陈雨晴',  '周婷', 'auto',   ...)   ← 系统判同义后落 (owner 在周报"采纳"过)

    source:
      - manual:   owner 在消息里显式说 "X 就是 Y" 抽出来的
      - auto:     向量阈值挂冲突 + owner 选 "采纳/都留" 后系统回写
      - reverted: owner 选 "保留" → 标记两者**不是**同义, 后续不再合并
                  (这条同样落表, 只是 canonical 等于 name 自身, 防止 auto 路径再触发)
    """

    __tablename__ = "entity_alias"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    canonical: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual/auto/reverted
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PendingRouting(Base):
    """bot 路由的"待回执"凭据 — helper 私聊外部 bot 后, 等对方回复关联回原会话。

    用户在群里 @helper 问的某类问题, 通过 procedural memory 配置成应路由给外部 bot。
    helper 私聊外部 bot 转发查询 → 外部 bot 回复(进 webhook, sender.id_type=app_id)
    → 按 target_app_id + 时间窗找最近未消费 PendingRouting → 按 original_chat_id
    判断在群里 / 私聊里把答案回贴。

    consumed_at 非空 = 已关联回贴; expired_at 非空 = 超时失败已通知用户。
    两者互斥; 都为空 = 还在等。
    """

    __tablename__ = "pending_routings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sent_msg_id: Mapped[str] = mapped_column(String(64), default="")  # helper → target_bot 的 msg_id
    target_app_id: Mapped[str] = mapped_column(String(64))            # 外部 bot app_id
    via_label: Mapped[str] = mapped_column(String(64), default="")    # 显示用名字, e.g. "tachi"
    original_raw_id: Mapped[int] = mapped_column(Integer)             # 用户原问题 raw_id
    original_chat_id: Mapped[str] = mapped_column(String(64), default="")     # 群 chat_id, 单聊空
    original_wave_msg_id: Mapped[str] = mapped_column(String(64), default="") # 用户原消息 id, 用于 quote
    original_asker_domain: Mapped[str] = mapped_column(String(64), default="")  # 原提问人, @他
    tracker_card_msg_id: Mapped[str] = mapped_column(String(64), default="")    # 思考中卡片 id, 收回执时原地替换
    tracker_receiver_id: Mapped[str] = mapped_column(String(64), default="")    # 卡片 receiver, 用于 update_card_active 兜底
    tracker_receiver_id_type: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChatContextCutoff(Base):
    """/clear 命令的"会话上下文加载起点"标记。

    用户发 /clear 时记一行: scope_key (群=chat_id, 私聊=user:<domain>) 对应
    当时最大 raw_id, 之后 list_chat_history 拉历史时用 RawInput.id > cutoff_raw_id
    过滤 — 老消息只是不再被加载到 prompt, 数据本身不删 (ingest 流水线仍可正常用)。
    重复 /clear 走 upsert, 同一 scope 只留最新。
    """

    __tablename__ = "chat_context_cutoffs"

    scope_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    cutoff_raw_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
