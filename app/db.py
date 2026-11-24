"""SQLite persistence layer.

设计要点：
- WAL 模式，读写可并发；写事务使用 BEGIN IMMEDIATE 串行化。
- 来源表（waybills / weigh_records / price_records）与预演结果表
  （match_stats / monthly_totals / meta）在同一个数据库文件内，
  来源与结果在同一事务里一起提交，一起生效。
- 预演结果是预先物化物化的结果表，查询只走结果表，不读来源明细、
  不展开计费行。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(
    os.environ.get("FREIGHT_DB_PATH", "/data/freight.db")
)

# WAL 下需要开启 foreign_keys（连接级设置）。
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
)


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一个配置好的连接（调用方负责关闭）。"""
    path = Path(db_path) if db_path is not None else _current_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def get_connection() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式只读快照事务。

    同一次概览查询（月合计 + 异常分页）在同一个已提交状态上读取：
    BEGIN 之后写者无法改变本事务看到的快照，两条语句看到同一版本。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，rollback 即结束并释放快照


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 拿写锁，提交/回滚原子生效。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
    waybill_no   TEXT PRIMARY KEY,
    train_no     TEXT NOT NULL,
    origin       TEXT NOT NULL,
    departure_dt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weigh_records (
    record_no  TEXT PRIMARY KEY,
    train_no   TEXT NOT NULL,
    weight_kg  REAL NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_records (
    record_no   TEXT PRIMARY KEY,
    origin      TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    price_per_t REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    CHECK (valid_from <= valid_to)
);

-- 预演结果（每运单一行，聚合命中数，不展开计费行）
CREATE TABLE IF NOT EXISTS match_stats (
    waybill_no     TEXT PRIMARY KEY,
    departure_mon  TEXT NOT NULL,
    weigh_hits     INTEGER NOT NULL,
    price_hits     INTEGER NOT NULL,
    generated_rows INTEGER NOT NULL,
    is_anomaly     INTEGER NOT NULL
);

-- 预演结果（每月一行的合计，按发车月）
CREATE TABLE IF NOT EXISTS monthly_totals (
    departure_mon  TEXT PRIMARY KEY,
    generated_rows INTEGER NOT NULL
);

-- 数据版本号，每次成功写入 +1，用于确认查询读取的已提交状态
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_waybills_train
    ON waybills(train_no);
CREATE INDEX IF NOT EXISTS idx_waybills_origin_dt
    ON waybills(origin, departure_dt);
CREATE INDEX IF NOT EXISTS idx_weigh_train
    ON weigh_records(train_no);
CREATE INDEX IF NOT EXISTS idx_price_origin_window
    ON price_records(origin, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_match_anomaly
    ON match_stats(is_anomaly, departure_mon, waybill_no);
CREATE INDEX IF NOT EXISTS idx_match_month
    ON match_stats(departure_mon);
"""


def init_db(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')"
        )
        conn.commit()
    finally:
        conn.close()


# 允许测试通过环境变量/依赖覆盖切换数据库路径。
_db_path: Path | None = None


def set_db_path(path: str | Path | None) -> None:
    global _db_path
    _db_path = Path(path) if path is not None else None


def _current_db_path() -> Path:
    return _db_path if _db_path is not None else DEFAULT_DB_PATH
