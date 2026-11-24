"""业务逻辑：来源写入 + 增量重算 + 结果查询。

关键约束（对应需求）：
1. 一张运单关联两张来源表，过磅按车次匹配，价目按始发站 + 发车日落入
   有效期（含首尾两日）匹配；两次内连接生成行数 = 过磅命中数 × 价目命中数。
2. 任一侧命中数 != 1 即为异常运单（漏配 = 0，重复 = 多条）。
3. 写入只重算“新旧匹配条件涉及”的运单与发车月，不做全量重算。
4. 来源与预演结果在同一 BEGIN IMMEDIATE 事务内提交；失败回滚，
   原结果仍可查询。
5. 月合计是预聚合结果表，不逐单扫描；异常页只走 match_stats 上的
   异常索引；查询不读来源明细，全程不展开计费行。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .models import PriceIn, WaybillIn, WeighIn

META_VERSION = "version"


class RehearsalError(Exception):
    """服务层基类。"""


class NotFoundError(RehearsalError):
    """记录不存在（撤回不存在的记录号）。"""


class ConflictError(RehearsalError):
    """运单号已存在等冲突。"""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_VERSION,)
    ).fetchone()
    return int(row["value"]) if row else 0


def _bump_version(conn: sqlite3.Connection) -> int:
    new_version = get_version(conn) + 1
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = ?",
        (str(new_version), META_VERSION),
    )
    return new_version


def _months_for(conn: sqlite3.Connection, waybill_nos: set[str]) -> list[str]:
    if not waybill_nos:
        return []
    marks = ",".join("?" for _ in waybill_nos)
    rows = conn.execute(
        f"SELECT DISTINCT substr(departure_dt,1,7) AS mon "
        f"FROM waybills WHERE waybill_no IN ({marks})",
        tuple(waybill_nos),
    ).fetchall()
    return sorted(r["mon"] for r in rows)


# --------------------------------------------------------------------------- #
# 增量重算
# --------------------------------------------------------------------------- #


def _recompute(
    conn: sqlite3.Connection,
    waybill_nos: set[str],
    months: list[str],
) -> None:
    """只重算给定运单的命中统计，并刷新相关月份合计。

    集合化 SQL：相关子查询统计两侧命中数，不生成计费明细行。
    """
    if not waybill_nos:
        return
    marks = ",".join("?" for _ in waybill_nos)

    # 1) 每运单一行：过磅命中（车次）× 价目命中（始发站 + 发车日在有效期内，
    #    BETWEEN 含首尾两日）。
    # 注：此处用 INSERT OR REPLACE 而非 ON CONFLICT DO UPDATE——
    # 当前 sqlite 解析器对 “INSERT ... SELECT ... ON CONFLICT” 组合存在
    # 语法歧义（near "DO"），而主键覆盖整行，REPLACE 语义等价。
    conn.execute(
        f"""
        INSERT OR REPLACE INTO match_stats
            (waybill_no, departure_mon, weigh_hits, price_hits,
             generated_rows, is_anomaly)
        SELECT waybill_no, departure_mon, weigh_hits, price_hits,
               weigh_hits * price_hits AS generated_rows,
               CASE WHEN weigh_hits <> 1 OR price_hits <> 1
                    THEN 1 ELSE 0 END AS is_anomaly
        FROM (
            SELECT w.waybill_no,
                   substr(w.departure_dt, 1, 7) AS departure_mon,
                   (SELECT COUNT(*)
                      FROM weigh_records g
                     WHERE g.train_no = w.train_no) AS weigh_hits,
                   (SELECT COUNT(*)
                      FROM price_records p
                     WHERE p.origin = w.origin
                       AND w.departure_dt BETWEEN p.valid_from
                                              AND p.valid_to) AS price_hits
              FROM waybills w
             WHERE w.waybill_no IN ({marks})
        )
        """,
        tuple(waybill_nos),
    )

    # 2) 相关月份的合计直接对结果表求和（不逐单调用、不碰来源明细）。
    if months:
        m_marks = ",".join("?" for _ in months)
        conn.execute(
            f"""
            INSERT OR REPLACE INTO monthly_totals
                (departure_mon, generated_rows)
            SELECT departure_mon, SUM(generated_rows)
              FROM match_stats
             WHERE departure_mon IN ({m_marks})
             GROUP BY departure_mon
            """,
            tuple(months),
        )
        # 防御：月份已无任何运单时清掉残留合计。
        conn.execute(
            f"""
            DELETE FROM monthly_totals
             WHERE departure_mon IN ({m_marks})
               AND departure_mon NOT IN (
                   SELECT DISTINCT departure_mon FROM match_stats
                    WHERE departure_mon IN ({m_marks})
               )
            """,
            tuple(months) + tuple(months),
        )


def _finish(
    conn: sqlite3.Connection, waybill_nos: set[str], extra_months: list[str]
) -> dict:
    months = sorted(set(_months_for(conn, waybill_nos)) | set(extra_months))
    _recompute(conn, waybill_nos, months)
    version = _bump_version(conn)
    return {
        "version": version,
        "affected_waybills": len(waybill_nos),
        "affected_months": months,
    }


# --------------------------------------------------------------------------- #
# 来源写入：运单
# --------------------------------------------------------------------------- #


def import_waybills(conn: sqlite3.Connection, items: list[WaybillIn]) -> dict:
    nos = [it.waybill_no for it in items]
    marks = ",".join("?" for _ in nos)
    existing = {
        r["waybill_no"]
        for r in conn.execute(
            f"SELECT waybill_no FROM waybills WHERE waybill_no IN ({marks})",
            tuple(nos),
        ).fetchall()
    }
    if existing:
        # 整批拒绝、回滚，调用方仍可查上一个已提交版本的结果。
        raise ConflictError(
            "运单号已存在，整批导入已拒绝: " + ", ".join(sorted(existing))
        )

    conn.executemany(
        "INSERT INTO waybills(waybill_no, train_no, origin, departure_dt) "
        "VALUES (?, ?, ?, ?)",
        [
            (it.waybill_no, it.train_no, it.origin, it.departure_dt.isoformat())
            for it in items
        ],
    )
    # 新运单没有旧匹配条件，月份只可能来自本批运单自身。
    return _finish(conn, set(nos), [])


# --------------------------------------------------------------------------- #
# 来源写入：过磅单（按车次匹配）
# --------------------------------------------------------------------------- #


def _waybills_by_trains(conn: sqlite3.Connection, trains: set[str]) -> set[str]:
    if not trains:
        return set()
    marks = ",".join("?" for _ in trains)
    return {
        r["waybill_no"]
        for r in conn.execute(
            f"SELECT waybill_no FROM waybills WHERE train_no IN ({marks})",
            tuple(trains),
        ).fetchall()
    }


def upsert_weigh(conn: sqlite3.Connection, data: WeighIn) -> dict:
    old = conn.execute(
        "SELECT train_no FROM weigh_records WHERE record_no = ?",
        (data.record_no,),
    ).fetchone()
    created = old is None
    affected_trains = {data.train_no}
    if old is not None:
        affected_trains.add(old["train_no"])

    conn.execute(
        "INSERT INTO weigh_records(record_no, train_no, weight_kg, note) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(record_no) DO UPDATE SET "
        "train_no=excluded.train_no, weight_kg=excluded.weight_kg, "
        "note=excluded.note",
        (data.record_no, data.train_no, data.weight_kg, data.note),
    )
    result = _finish(conn, _waybills_by_trains(conn, affected_trains), [])
    result["detail"] = {"created": created, "record_no": data.record_no}
    return result


def withdraw_weigh(conn: sqlite3.Connection, record_no: str) -> dict:
    old = conn.execute(
        "SELECT train_no FROM weigh_records WHERE record_no = ?",
        (record_no,),
    ).fetchone()
    if old is None:
        raise NotFoundError(f"过磅单记录号不存在: {record_no}")
    affected = _waybills_by_trains(conn, {old["train_no"]})
    conn.execute("DELETE FROM weigh_records WHERE record_no = ?", (record_no,))
    result = _finish(conn, affected, [])
    result["detail"] = {"withdrawn": record_no}
    return result


# --------------------------------------------------------------------------- #
# 来源写入：价目（按始发站 + 发车日 ∈ [valid_from, valid_to] 匹配）
# --------------------------------------------------------------------------- #


def _waybills_in_price_scope(
    conn: sqlite3.Connection, scopes: list[tuple[str, date, date]]
) -> set[str]:
    """scopes: (origin, valid_from, valid_to)，新旧窗口的并集。"""
    if not scopes:
        return set()
    origins = {s[0] for s in scopes}
    marks = ",".join("?" for _ in origins)
    candidates = conn.execute(
        f"SELECT waybill_no, origin, departure_dt FROM waybills "
        f"WHERE origin IN ({marks})",
        tuple(origins),
    ).fetchall()
    affected: set[str] = set()
    windows = [(o, f.isoformat(), t.isoformat()) for o, f, t in scopes]
    for row in candidates:
        for origin, vfrom, vto in windows:
            if row["origin"] == origin and vfrom <= row["departure_dt"] <= vto:
                affected.add(row["waybill_no"])
                break
    return affected


def upsert_price(conn: sqlite3.Connection, data: PriceIn) -> dict:
    old = conn.execute(
        "SELECT origin, valid_from, valid_to FROM price_records "
        "WHERE record_no = ?",
        (data.record_no,),
    ).fetchone()
    created = old is None
    scopes: list[tuple[str, date, date]] = [
        (data.origin, data.valid_from, data.valid_to)
    ]
    if old is not None:
        scopes.append(
            (
                old["origin"],
                date.fromisoformat(old["valid_from"]),
                date.fromisoformat(old["valid_to"]),
            )
        )

    conn.execute(
        "INSERT INTO price_records"
        "(record_no, origin, valid_from, valid_to, price_per_t, note) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(record_no) DO UPDATE SET "
        "origin=excluded.origin, valid_from=excluded.valid_from, "
        "valid_to=excluded.valid_to, price_per_t=excluded.price_per_t, "
        "note=excluded.note",
        (
            data.record_no,
            data.origin,
            data.valid_from.isoformat(),
            data.valid_to.isoformat(),
            data.price_per_t,
            data.note,
        ),
    )
    result = _finish(conn, _waybills_in_price_scope(conn, scopes), [])
    result["detail"] = {"created": created, "record_no": data.record_no}
    return result


def withdraw_price(conn: sqlite3.Connection, record_no: str) -> dict:
    old = conn.execute(
        "SELECT origin, valid_from, valid_to FROM price_records "
        "WHERE record_no = ?",
        (record_no,),
    ).fetchone()
    if old is None:
        raise NotFoundError(f"价目记录号不存在: {record_no}")
    scopes = [
        (
            old["origin"],
            date.fromisoformat(old["valid_from"]),
            date.fromisoformat(old["valid_to"]),
        )
    ]
    affected = _waybills_in_price_scope(conn, scopes)
    conn.execute("DELETE FROM price_records WHERE record_no = ?", (record_no,))
    result = _finish(conn, affected, [])
    result["detail"] = {"withdrawn": record_no}
    return result


# --------------------------------------------------------------------------- #
# 全量重建（管理/校验用，正常写入路径不会调用）
# --------------------------------------------------------------------------- #


def full_rebuild(conn: sqlite3.Connection) -> dict:
    conn.execute("DELETE FROM match_stats")
    conn.execute("DELETE FROM monthly_totals")
    all_nos = {
        r["waybill_no"] for r in conn.execute("SELECT waybill_no FROM waybills")
    }
    months = [
        r["mon"]
        for r in conn.execute(
            "SELECT DISTINCT substr(departure_dt,1,7) AS mon FROM waybills"
        ).fetchall()
    ]
    _recompute(conn, all_nos, months)
    version = _bump_version(conn)
    return {
        "version": version,
        "affected_waybills": len(all_nos),
        "affected_months": months,
    }


# --------------------------------------------------------------------------- #
# 查询（只读结果表；同快照由路由层的 snapshot() 保证）
# --------------------------------------------------------------------------- #


def month_total(conn: sqlite3.Connection, departure_mon: str) -> dict:
    row = conn.execute(
        "SELECT generated_rows FROM monthly_totals WHERE departure_mon = ?",
        (departure_mon,),
    ).fetchone()
    return {
        "departure_mon": departure_mon,
        "generated_rows": row["generated_rows"] if row else 0,
    }


def anomaly_page(
    conn: sqlite3.Connection,
    limit: int,
    cursor: str | None = None,
    departure_mon: str | None = None,
) -> dict:
    """异常运单键集分页，只命中 match_stats 的异常索引。"""
    sql = "SELECT waybill_no, departure_mon, weigh_hits, price_hits, " \
          "generated_rows, is_anomaly FROM match_stats WHERE is_anomaly = 1"
    params: list = []
    if departure_mon is not None:
        sql += " AND departure_mon = ?"
        params.append(departure_mon)
    if cursor is not None:
        sql += " AND waybill_no > ?"
        params.append(cursor)
    sql += " ORDER BY waybill_no ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    items = [
        {
            "waybill_no": r["waybill_no"],
            "departure_mon": r["departure_mon"],
            "weigh_hits": r["weigh_hits"],
            "price_hits": r["price_hits"],
            "generated_rows": r["generated_rows"],
            "is_anomaly": bool(r["is_anomaly"]),
        }
        for r in rows
    ]
    next_cursor = rows[-1]["waybill_no"] if len(rows) == limit else None
    return {
        "items": items,
        "next_cursor": next_cursor,
        "limit": limit,
        "count": len(items),
    }


def overview(
    conn: sqlite3.Connection,
    departure_mon: str,
    limit: int,
    cursor: str | None,
) -> dict:
    """月合计 + 异常页：调用方须已开启 snapshot 快照事务。"""
    return {
        "version": get_version(conn),
        "month_total": month_total(conn, departure_mon),
        "anomalies": anomaly_page(conn, limit, cursor, departure_mon),
    }


def rehearsal_detail(conn: sqlite3.Connection, waybill_no: str) -> dict:
    r = conn.execute(
        "SELECT waybill_no, departure_mon, weigh_hits, price_hits, "
        "generated_rows, is_anomaly FROM match_stats WHERE waybill_no = ?",
        (waybill_no,),
    ).fetchone()
    if r is None:
        raise NotFoundError(f"运单不存在或尚无预演结果: {waybill_no}")
    return {
        "waybill_no": r["waybill_no"],
        "departure_mon": r["departure_mon"],
        "weigh_hits": r["weigh_hits"],
        "price_hits": r["price_hits"],
        "generated_rows": r["generated_rows"],
        "is_anomaly": bool(r["is_anomaly"]),
    }
