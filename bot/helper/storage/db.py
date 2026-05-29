"""SQLite 连接初始化 + session 上下文。M1 同步即可。

M4 起加 sqlite-vec 扩展:每条新连接打开时自动 load_extension(sqlite_vec)。
配套的 vec_items 虚拟表(1024 维)在 init_engine 里建。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex

from helper.storage.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None

# bge-m3 输出 1024 维。换模型要 reindex,见 helper.storage.vector。
VEC_DIM = 1024


def _load_sqlite_vec(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
    """SQLAlchemy connect listener — 每条 sqlite 连接打开时 load_extension。

    注意:macOS 系统 Python 的 sqlite3 不支持 enable_load_extension,需用 venv / brew python。
    extension 不会自动加载 — 必须每条连接都显式 load。
    """
    dbapi_conn.enable_load_extension(True)
    sqlite_vec.load(dbapi_conn)
    dbapi_conn.enable_load_extension(False)


def _ensure_vec_table(engine: Engine) -> None:
    """vec0 虚拟表 vec_items(embedding float[1024])。

    vec0 不能加任意普通列;业务侧 (kind, ref, hash, model, indexed_at) 走 sidecar
    helper.storage.models.VectorIndex,通过 rowid 关联。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items "
                f"USING vec0(embedding float[{VEC_DIM}])"
            )
        )


def _ensure_fts_table(engine: Engine) -> None:
    """fts5 虚拟表 fts_items(text, kind UNINDEXED, ref UNINDEXED)。

    词面召回:Jaccard 全表扫在 1000+ 篇 / 几万 raw 时秒级,换 fts5 + bm25。
    分词在 Python 端用 jieba 做完(中文分词比 sqlite 内建 tokenizer 准),
    入库写"jieba 切完空格拼"的字符串,fts5 用 unicode61 简单分词。
    (kind, ref) 用 UNINDEXED 标记 — 不进倒排,只作为反查键 + DELETE 谓词。
    业务侧 supersede / delete 走 `DELETE FROM fts_items WHERE kind=? AND ref=?`。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts_items "
                "USING fts5(text, kind UNINDEXED, ref UNINDEXED, tokenize = 'unicode61')"
            )
        )


def init_engine(db_path: Path) -> Engine:
    """建库 + 建表 + 轻量补列。重复调用安全。

    create_all 不会给已存在的表加列。M1 还没有 alembic,这里用 PRAGMA + ALTER
    打个补丁: 模型里声明的列若 sqlite 表没有,直接 ALTER ADD。SQLite ADD COLUMN
    要求显式默认值,统一用列类型对应的零值('' / 0 / 当前时间)。
    """
    global _engine, _SessionLocal
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(f"sqlite:///{db_path}", future=True)
    event.listen(_engine, "connect", _load_sqlite_vec)
    Base.metadata.create_all(_engine)
    _backfill_missing_columns(_engine)
    _backfill_missing_indexes(_engine)
    _ensure_vec_table(_engine)
    _ensure_fts_table(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _backfill_missing_columns(engine: Engine) -> None:
    """对每张 ORM 表: 比对实际列 vs 模型列,缺的 ALTER TABLE ADD COLUMN。

    SQLite ALTER 限制: 只支持 ADD,不支持改/删。够 M1 单向加字段的需求。
    所有新加列都允许 NULL / 给静态默认(空串/0/false),不影响已有行。
    """
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl_type = col.type.compile(dialect=engine.dialect)
            default_clause = _ddl_default(col)
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default_clause}')
                )


def _backfill_missing_indexes(engine: Engine) -> None:
    """create_all 不会在已存在的表上补索引(只有首建表时才一起建)。
    这里把每张表声明的 Index 跑 CreateIndex(if_not_exists),适配 SQLite 部分索引等。
    """
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {idx["name"] for idx in insp.get_indexes(table.name)}
        for idx in table.indexes:
            if idx.name in existing:
                continue
            with engine.begin() as conn:
                conn.execute(CreateIndex(idx, if_not_exists=True))


def _ddl_default(col):  # noqa: ANN001
    """把 ORM 列的静态默认翻译成 SQLite ADD COLUMN 的 DEFAULT 子句。
    callable 默认(如 datetime.now)无法翻译 → 不带默认,旧行该列读出来是 NULL。
    """
    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return ""
    v = d.arg
    if isinstance(v, bool):
        return f" DEFAULT {1 if v else 0}"
    if isinstance(v, (int, float)):
        return f" DEFAULT {v}"
    if isinstance(v, str):
        return f" DEFAULT '{v}'"
    return ""


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("DB not initialized — call init_engine(db_path) first")
    return _engine


@contextmanager
def session() -> Iterator[Session]:
    if _SessionLocal is None:
        raise RuntimeError("DB not initialized — call init_engine(db_path) first")
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
