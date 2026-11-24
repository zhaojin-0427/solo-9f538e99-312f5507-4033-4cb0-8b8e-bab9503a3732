"""抽检版业务逻辑。

需求对应：
1. 按发车月开抽检版，返回版号与总行数；开版在单个 BEGIN IMMEDIATE
   事务内完成，冻结同一已提交状态（当时的 match_stats + 来源记录），
   期间来源仍可补录/更正/撤回，但本版之后一律按冻结匹配解析。
2. 开版失败整体回滚、保留旧版；成功后只留新版（同事务内删除旧版数据，
   外键 ON DELETE CASCADE 连带清理）。
3. 凭版号提交至多 20 个行号，返回运单号/过磅记录号/价目记录号；
   两次内连接的结果按这三项依次升序、从 1 编号，越界报错。
4. 全程只生成抽中的计费行（sampled_billing_lines 惰性写入，重查幂等）；
   版内每条来源记录最多存一份（冻结表主键 = 版号+记录号）。
5. 查一个行号：cum_rows 索引点查只读 1 条票统计（上限 32 票），
   再加该票的过磅/价目各一条冻结来源，不回源、不扫全月。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from .service import META_VERSION, RehearsalError

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class SamplingError(RehearsalError):
    """抽检服务基类（422）。"""


class VersionNotFoundError(SamplingError):
    """抽检版不存在（404）。"""


def _validate_month(departure_mon: str) -> str:
    mon = departure_mon.strip()
    if not _MONTH_RE.match(mon):
        raise SamplingError("发车月格式必须为 YYYY-MM（如 2026-03）")
    return mon


# --------------------------------------------------------------------------- #
# 开版
# --------------------------------------------------------------------------- #


def open_version(conn: sqlite3.Connection, departure_mon: str) -> dict:
    """冻结一个发车月的匹配快照。调用方须已开启 BEGIN IMMEDIATE 事务。

    事务内顺序：删旧版 → 插新版头 → 冻结票统计 → 冻结去重来源。
    任一步失败由外层回滚，旧版完整保留。
    """
    mon = _validate_month(departure_mon)

    old = conn.execute(
        "SELECT version_id FROM sample_versions "
        "WHERE departure_mon = ? AND status = 'active'",
        (mon,),
    ).fetchone()
    if old is not None:
        # 先删旧版头，子表经 ON DELETE CASCADE 一并清理；
        # 与新版插入同事务，失败回滚则旧版原样保留。
        conn.execute(
            "DELETE FROM sample_versions WHERE version_id = ?",
            (old["version_id"],),
        )

    source_version = int(
        conn.execute(
            "SELECT value FROM meta WHERE key = ?", (META_VERSION,)
        ).fetchone()["value"]
    )
    cur = conn.execute(
        "INSERT INTO sample_versions"
        "(departure_mon, total_rows, source_version, status, created_at) "
        "VALUES (?, 0, ?, 'active', ?)",
        (
            mon,
            source_version,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    version_id = cur.lastrowid

    # 1) 冻结每票命中统计（match_stats 在开版事务的同一已提交状态上读取），
    #    并按运单号升序计算月内累计行数 cum_rows。
    stats = conn.execute(
        """
        SELECT m.waybill_no, w.train_no, w.origin,
               m.weigh_hits, m.price_hits, m.generated_rows AS row_count
          FROM match_stats m
          JOIN waybills w ON w.waybill_no = m.waybill_no
         WHERE m.departure_mon = ?
         ORDER BY m.waybill_no ASC
        """,
        (mon,),
    ).fetchall()

    total_rows = 0
    stat_rows: list[tuple] = []
    for r in stats:
        total_rows += r["row_count"]
        stat_rows.append(
            (
                version_id,
                r["waybill_no"],
                r["train_no"],
                r["origin"],
                r["weigh_hits"],
                r["price_hits"],
                r["row_count"],
                total_rows,
            )
        )
    conn.executemany(
        "INSERT INTO sample_waybill_stats"
        "(version_id, waybill_no, train_no, origin, weigh_hits, price_hits, "
        " row_count, cum_rows) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        stat_rows,
    )

    # 2) 冻结“开版当时实际命中”的成员关系（同一事务、同一已提交状态）。
    #    成员关系直接由两次内连接产生，来源之后改车次/撤回/改窗口都不影响本版。
    conn.execute(
        """
        INSERT INTO sample_waybill_weigh(version_id, waybill_no, record_no)
        SELECT DISTINCT ?, w.waybill_no, g.record_no
          FROM waybills w
          JOIN weigh_records g ON g.train_no = w.train_no
         WHERE substr(w.departure_dt, 1, 7) = ?
        """,
        (version_id, mon),
    )
    conn.execute(
        """
        INSERT INTO sample_waybill_price(version_id, waybill_no, record_no)
        SELECT DISTINCT ?, w.waybill_no, p.record_no
          FROM waybills w
          JOIN price_records p
            ON p.origin = w.origin
           AND w.departure_dt BETWEEN p.valid_from AND p.valid_to
         WHERE substr(w.departure_dt, 1, 7) = ?
        """,
        (version_id, mon),
    )

    # 3) 冻结去重来源记录内容：版内每条来源记录最多一份
    #    （主键 = 版号+记录号），供审核员核对单据内容；行号解析不依赖它。
    conn.execute(
        """
        INSERT OR IGNORE INTO sample_weigh_records
            (version_id, record_no, train_no, weight_kg, note)
        SELECT DISTINCT ?, g.record_no, g.train_no, g.weight_kg, g.note
          FROM sample_waybill_weigh m
          JOIN weigh_records g ON g.record_no = m.record_no
         WHERE m.version_id = ?
        """,
        (version_id, version_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO sample_price_records
            (version_id, record_no, origin, valid_from, valid_to,
             price_per_t, note)
        SELECT DISTINCT ?, p.record_no, p.origin, p.valid_from, p.valid_to,
               p.price_per_t, p.note
          FROM sample_waybill_price m
          JOIN price_records p ON p.record_no = m.record_no
         WHERE m.version_id = ?
        """,
        (version_id, version_id),
    )

    conn.execute(
        "UPDATE sample_versions SET total_rows = ? WHERE version_id = ?",
        (total_rows, version_id),
    )
    return get_version_info(conn, version_id)


# --------------------------------------------------------------------------- #
# 行号查询
# --------------------------------------------------------------------------- #


def _load_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
    v = conn.execute(
        "SELECT * FROM sample_versions WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if v is None:
        raise VersionNotFoundError(f"抽检版不存在: {version_id}")
    return v


def resolve_lines(
    conn: sqlite3.Connection, version_id: int, line_nos: list[int]
) -> dict:
    """把行号解析为三元组。调用方须已开启事务（快照即可）。

    每个行号的读取：1 条票统计（cum_rows 索引点查）+ 该票冻结的
    过磅/价目记录列表，不回源、不扫全月，命中统计读取 ≤ 32 票上限。
    抽中的行才落 sampled_billing_lines，重复提交幂等。
    """
    v = _load_version(conn, version_id)
    total_rows = v["total_rows"]

    out_of_range = sorted(n for n in line_nos if n < 1 or n > total_rows)
    if out_of_range:
        raise SamplingError(
            f"行号越界（本版共 {total_rows} 行，编号 1..{total_rows}）: "
            + ", ".join(map(str, out_of_range))
        )

    items: list[dict] = []
    for line_no in line_nos:
        # 已抽过的行直接回表，不重复生成。
        cached = conn.execute(
            "SELECT line_no, waybill_no, weigh_record_no, price_record_no "
            "FROM sampled_billing_lines WHERE version_id = ? AND line_no = ?",
            (version_id, line_no),
        ).fetchone()
        if cached is not None:
            items.append(dict(cached))
            continue

        # 行号 → 票：cum_rows >= line_no 的第一票（索引点查，1 条统计）。
        stat = conn.execute(
            """
            SELECT waybill_no, train_no, origin, weigh_hits, price_hits,
                   row_count, cum_rows
              FROM sample_waybill_stats
             WHERE version_id = ? AND cum_rows >= ? AND row_count > 0
             ORDER BY cum_rows ASC LIMIT 1
            """,
            (version_id, line_no),
        ).fetchone()
        # 越界已在上面统一拦截，这里仅作防御。
        if stat is None:  # pragma: no cover - 防御性分支
            raise SamplingError(f"行号无法定位到运单: {line_no}")

        prev_cum = stat["cum_rows"] - stat["row_count"]
        offset = line_no - prev_cum - 1  # 0 基，落在本票的行区间内

        # 过磅/价目成员均来自开版冻结的命中关系，按记录号升序；
        # 来源表后续怎么改都不影响这里的结果。
        weigh_rows = conn.execute(
            "SELECT record_no FROM sample_waybill_weigh "
            "WHERE version_id = ? AND waybill_no = ? ORDER BY record_no ASC",
            (version_id, stat["waybill_no"]),
        ).fetchall()
        price_rows = conn.execute(
            "SELECT record_no FROM sample_waybill_price "
            "WHERE version_id = ? AND waybill_no = ? ORDER BY record_no ASC",
            (version_id, stat["waybill_no"]),
        ).fetchall()

        if len(weigh_rows) != stat["weigh_hits"] or len(price_rows) != stat[
            "price_hits"
        ]:  # pragma: no cover - 冻结一致性防御
            raise SamplingError(
                f"版 {version_id} 冻结数据与命中统计不一致，行号 {line_no}"
            )

        w_idx, p_idx = divmod(offset, stat["price_hits"])
        weigh_no = weigh_rows[w_idx]["record_no"]
        price_no = price_rows[p_idx]["record_no"]

        # 只生成抽中的计费行；主键保证版内同一行号至多一份。
        conn.execute(
            "INSERT OR IGNORE INTO sampled_billing_lines"
            "(version_id, line_no, waybill_no, weigh_record_no, "
            " price_record_no) VALUES (?, ?, ?, ?, ?)",
            (version_id, line_no, stat["waybill_no"], weigh_no, price_no),
        )
        items.append(
            {
                "line_no": line_no,
                "waybill_no": stat["waybill_no"],
                "weigh_record_no": weigh_no,
                "price_record_no": price_no,
            }
        )

    items.sort(key=lambda x: x["line_no"])
    return {
        "version_id": version_id,
        "total_rows": total_rows,
        "count": len(items),
        "items": items,
    }


# --------------------------------------------------------------------------- #
# 只读查询
# --------------------------------------------------------------------------- #


def get_version_info(conn: sqlite3.Connection, version_id: int) -> dict:
    v = _load_version(conn, version_id)
    return {
        "version_id": v["version_id"],
        "departure_mon": v["departure_mon"],
        "total_rows": v["total_rows"],
        "source_version": v["source_version"],
        "status": v["status"],
        "created_at": v["created_at"],
    }


def latest_version(conn: sqlite3.Connection, departure_mon: str) -> dict:
    mon = _validate_month(departure_mon)
    row = conn.execute(
        "SELECT * FROM sample_versions "
        "WHERE departure_mon = ? AND status = 'active' "
        "ORDER BY version_id DESC LIMIT 1",
        (mon,),
    ).fetchone()
    if row is None:
        raise VersionNotFoundError(f"发车月 {mon} 尚无抽检版")
    return dict(row)
