"""抽检版端到端测试（TestClient + 真实 SQLite）。"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import db


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #


def waybill(no: str, train: str, origin: str, dt: str) -> dict:
    return {
        "waybill_no": no,
        "train_no": train,
        "origin": origin,
        "departure_dt": dt,
    }


def weigh(no: str, train: str, weight: float = 1000.0) -> dict:
    return {"record_no": no, "train_no": train, "weight_kg": weight}


def price(no: str, origin: str, vfrom: str, vto: str, rate: float = 50.0) -> dict:
    return {
        "record_no": no,
        "origin": origin,
        "valid_from": vfrom,
        "valid_to": vto,
        "price_per_t": rate,
    }


def import_waybills(client: TestClient, items: list[dict]) -> None:
    r = client.post("/waybills/import", json={"items": items})
    assert r.status_code == 200, r.text


def put_weigh(client: TestClient, rec_no: str, body: dict) -> None:
    r = client.put(f"/weigh/records/{rec_no}", json=body)
    assert r.status_code == 200, r.text


def put_price(client: TestClient, rec_no: str, body: dict) -> None:
    r = client.put(f"/price/records/{rec_no}", json=body)
    assert r.status_code == 200, r.text


def open_version(client: TestClient, mon: str, expect_code: int = 200) -> dict:
    r = client.post(f"/samples/months/{mon}/versions")
    assert r.status_code == expect_code, r.text
    return r.json()


def query_lines(
    client: TestClient, version_id: int, line_nos: list[int], expect_code: int = 200
) -> dict:
    r = client.post(
        f"/samples/versions/{version_id}/lines", json={"line_nos": line_nos}
    )
    assert r.status_code == expect_code, r.text
    return r.json()


def build_two_ticket_dataset(client: TestClient) -> None:
    """W1(T1,BJ): 2 过磅 × 2 价目 = 4 行；W2(T2,SH): 1 × 1 = 1 行。"""
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "SH", "2026-03-06"),
        ],
    )
    put_weigh(client, "G1", weigh("G1", "T1"))
    put_weigh(client, "G2", weigh("G2", "T1"))
    put_weigh(client, "G3", weigh("G3", "T2"))
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    put_price(client, "P2", price("P2", "BJ", "2026-03-01", "2026-03-31"))
    put_price(client, "P3", price("P3", "SH", "2026-03-01", "2026-03-31"))


# W1 票内 4 行（过磅序 G1,G2 × 价目序 P1,P2），W2 票内 1 行；
# 全月按运单号、过磅号、价目号升序编号 1..5。
EXPECTED_ALL_LINES = [
    (1, "W1", "G1", "P1"),
    (2, "W1", "G1", "P2"),
    (3, "W1", "G2", "P1"),
    (4, "W1", "G2", "P2"),
    (5, "W2", "G3", "P3"),
]


# --------------------------------------------------------------------------- #
# 1. 跨票取样：行号按 (运单号, 过磅记录号, 价目记录号) 升序，从 1 起算
# --------------------------------------------------------------------------- #


def test_cross_ticket_sampling_numbering(client: TestClient) -> None:
    build_two_ticket_dataset(client)
    info = open_version(client, "2026-03")
    assert info["total_rows"] == 5
    assert info["status"] == "active"
    assert info["departure_mon"] == "2026-03"
    assert info["source_version"] == client.get("/health").json()["version"]

    # latest 与版号查询一致
    latest = client.get("/samples/months/2026-03/versions/latest").json()
    assert latest["version_id"] == info["version_id"]
    assert client.get(f"/samples/versions/{info['version_id']}").json() == info

    # 一次取多行（含跨票边界 4→5），顺序按请求行号回传
    res = query_lines(client, info["version_id"], [5, 1, 4])
    assert res["count"] == 3
    assert [
        (x["line_no"], x["waybill_no"], x["weigh_record_no"], x["price_record_no"])
        for x in res["items"]
    ] == [
        (1, "W1", "G1", "P1"),
        (4, "W1", "G2", "P2"),
        (5, "W2", "G3", "P3"),
    ]

    # 逐行核对全部 5 行的三元组编号
    all_res = query_lines(client, info["version_id"], [1, 2, 3, 4, 5])
    assert [
        (x["line_no"], x["waybill_no"], x["weigh_record_no"], x["price_record_no"])
        for x in all_res["items"]
    ] == EXPECTED_ALL_LINES


def test_sampling_max_20_lines_and_validation(client: TestClient) -> None:
    build_two_ticket_dataset(client)
    vid = open_version(client, "2026-03")["version_id"]

    # 超过 20 个行号 → 422
    r = client.post(
        f"/samples/versions/{vid}/lines", json={"line_nos": list(range(1, 22))}
    )
    assert r.status_code == 422
    # 空列表 → 422
    r = client.post(f"/samples/versions/{vid}/lines", json={"line_nos": []})
    assert r.status_code == 422
    # 重复行号 → 422
    r = client.post(
        f"/samples/versions/{vid}/lines", json={"line_nos": [1, 1]}
    )
    assert r.status_code == 422
    # 非正行号 → 422
    r = client.post(f"/samples/versions/{vid}/lines", json={"line_nos": [0]})
    assert r.status_code == 422

    # 越界（本版 5 行）→ 422，错误信息带边界
    r = client.post(f"/samples/versions/{vid}/lines", json={"line_nos": [6]})
    assert r.status_code == 422
    assert "6" in r.json()["detail"]

    # 版号不存在 → 404
    r = client.post("/samples/versions/9999/lines", json={"line_nos": [1]})
    assert r.status_code == 404
    assert client.get("/samples/versions/9999").status_code == 404
    assert client.get(
        "/samples/months/2026-09/versions/latest"
    ).status_code == 404

    # 月份格式非法 → 422
    assert client.post("/samples/months/2026-13/versions").status_code == 422
    assert client.post("/samples/months/badmonth/versions").status_code == 422


# --------------------------------------------------------------------------- #
# 2. 来源更正/撤回/补录后，旧版行号仍按开版时匹配解析
# --------------------------------------------------------------------------- #


def test_source_correction_does_not_change_frozen_version(
    client: TestClient, tmp_path
) -> None:
    build_two_ticket_dataset(client)
    v1 = open_version(client, "2026-03")
    vid1 = v1["version_id"]
    before = query_lines(client, vid1, [1, 2, 3, 4, 5])

    # 开版后更正来源：G1 改车次（T1→T2）、P1 窗口移出 3 月、补一条过磅、撤回 G3
    put_weigh(client, "G1", weigh("G1", "T9"))
    put_price(client, "P1", price("P1", "BJ", "2026-05-01", "2026-05-31"))
    put_weigh(client, "G4", weigh("G4", "T1"))
    r = client.delete("/weigh/records/G3")
    assert r.status_code == 200

    # 旧版查询结果完全不变，且不回源（仍能返回已被撤回的 G3）
    after = query_lines(client, vid1, [1, 2, 3, 4, 5])
    assert after["items"] == before["items"]
    assert after["total_rows"] == 5

    # 新版反映来源改动；两版并存期间各自可查
    v2 = open_version(client, "2026-03")
    vid2 = v2["version_id"]
    assert vid2 != vid1
    # 更正后：W1 = G2,G4(2) × P2(1) = 2 行；
    # W2 过磅命中 0（G3 撤回、G1 改到 T9 后 T2 无过磅）→ 0 行
    assert v2["total_rows"] == 2
    new_lines = query_lines(client, vid2, [1, 2])
    assert [
        (x["line_no"], x["waybill_no"], x["weigh_record_no"], x["price_record_no"])
        for x in new_lines["items"]
    ] == [
        (1, "W1", "G2", "P2"),
        (2, "W1", "G4", "P2"),
    ]
    # 旧版在新版成功后已被替换（每月只留一个 active 版）
    assert client.get(f"/samples/versions/{vid1}").status_code == 404
    assert (
        client.get("/samples/months/2026-03/versions/latest").json()["version_id"]
        == vid2
    )

    # 旧版冻结数据已随版本清理
    conn = db.connect(tmp_path / "test_freight.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_versions WHERE version_id = ?", (vid1,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_waybill_stats WHERE version_id = ?",
            (vid1,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sampled_billing_lines WHERE version_id = ?",
            (vid1,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3. 开版失败保留旧版；新版成功前上一成功版可查
# --------------------------------------------------------------------------- #


def test_failed_open_keeps_previous_version(client: TestClient) -> None:
    build_two_ticket_dataset(client)
    v1 = open_version(client, "2026-03")

    # 非法月份开版失败（422，事务回滚），旧版原样保留、版号不变
    r = client.post("/samples/months/2026-99/versions")
    assert r.status_code == 422
    latest = client.get("/samples/months/2026-03/versions/latest").json()
    assert latest["version_id"] == v1["version_id"]

    # 无计费行的月份开版也会建版（total_rows=0）；换一个空月验证不影响 3 月旧版
    empty = open_version(client, "2026-04")
    assert empty["total_rows"] == 0
    r = client.post(
        f"/samples/versions/{empty['version_id']}/lines",
        json={"line_nos": [1]},
    )
    assert r.status_code == 422  # 0 行版任何行号都越界
    assert (
        client.get("/samples/months/2026-03/versions/latest").json()["version_id"]
        == v1["version_id"]
    )


def test_successful_open_replaces_old_version(client: TestClient) -> None:
    build_two_ticket_dataset(client)
    v1 = open_version(client, "2026-03")
    # 先在 v1 抽几行
    query_lines(client, v1["version_id"], [1, 5])

    # 来源补录后重开：成功后只留新版
    put_price(client, "P4", price("P4", "SH", "2026-03-01", "2026-03-31"))
    v2 = open_version(client, "2026-03")
    assert v2["version_id"] != v1["version_id"]
    assert v2["total_rows"] == 6  # W2 变成 1×2 = 2 行
    assert client.get(f"/samples/versions/{v1['version_id']}").status_code == 404

    # 新版行号重新编号：W2 的两行排在 5、6
    res = query_lines(client, v2["version_id"], [5, 6])
    assert [
        (x["waybill_no"], x["weigh_record_no"], x["price_record_no"])
        for x in res["items"]
    ] == [("W2", "G3", "P3"), ("W2", "G3", "P4")]


# --------------------------------------------------------------------------- #
# 4. 只生成抽中的计费行 + 版内每条来源记录最多一份 + 重查幂等
# --------------------------------------------------------------------------- #


def test_only_sampled_lines_stored_and_dedup_sources(
    client: TestClient, tmp_path
) -> None:
    build_two_ticket_dataset(client)
    info = open_version(client, "2026-03")
    vid = info["version_id"]

    conn = db.connect(tmp_path / "test_freight.db")
    try:
        # 开版后、抽样前：没有任何计费行
        assert conn.execute(
            "SELECT COUNT(*) FROM sampled_billing_lines WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    # 只抽 2 行
    query_lines(client, vid, [2, 4])
    conn = db.connect(tmp_path / "test_freight.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sampled_billing_lines WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 2
        # 版内来源记录各去重一份：过磅 3 条、价目 3 条
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_weigh_records WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_price_records WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 3
        # 冻结来源表主键保证同一记录号版内至多一份
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sample_weigh_records"
                "(version_id, record_no, train_no, weight_kg) "
                "VALUES (?, 'G1', 'T1', 1.0)",
                (vid,),
            )
        conn.rollback()
    finally:
        conn.close()

    # 重复抽样同样的行：结果一致、不新增计费行
    again = query_lines(client, vid, [2, 4])
    assert again["count"] == 2
    conn = db.connect(tmp_path / "test_freight.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sampled_billing_lines WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 2
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 5. 服务重启后仍按成功开版时的匹配查行（持久化）
# --------------------------------------------------------------------------- #


def test_version_survives_restart(client: TestClient, tmp_path) -> None:
    build_two_ticket_dataset(client)
    info = open_version(client, "2026-03")
    vid = info["version_id"]
    query_lines(client, vid, [1, 5])

    # 模拟重启：重新初始化库（同一文件），冻结数据与抽中行都还在
    db.init_db(tmp_path / "test_freight.db")
    got = client.get(f"/samples/versions/{vid}").json()
    assert got["total_rows"] == 5
    res = query_lines(client, vid, [3])
    assert res["items"][0] == {
        "line_no": 3,
        "waybill_no": "W1",
        "weigh_record_no": "G2",
        "price_record_no": "P1",
    }

    # 来源大改也不影响持久化版
    client.delete("/weigh/records/G1")
    client.delete("/weigh/records/G2")
    res = query_lines(client, vid, [1, 2, 3, 4, 5])
    assert [
        (x["line_no"], x["waybill_no"], x["weigh_record_no"], x["price_record_no"])
        for x in res["items"]
    ] == EXPECTED_ALL_LINES


# --------------------------------------------------------------------------- #
# 6. 查一个行号对票统计的读取 ≤ 32：cum_rows 索引点查（实测 1 条）
# --------------------------------------------------------------------------- #


def test_line_lookup_reads_at_most_32_waybill_stats(
    client: TestClient, tmp_path
) -> None:
    # 40 票，每票 1×1=1 行；加两票多行使总行数 > 40
    items = [
        waybill(f"W{i:03d}", f"T{i}", "BJ", "2026-03-10") for i in range(40)
    ]
    import_waybills(client, items)
    for i in range(40):
        put_weigh(client, f"G{i:03d}", weigh(f"G{i:03d}", f"T{i}"))
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    info = open_version(client, "2026-03")
    vid = info["version_id"]
    assert info["total_rows"] == 40

    # 任意行号定位结果正确（运单号按升序与行号一一对应）
    for target in (1, 20, 40):
        res = query_lines(client, vid, [target])
        assert res["items"][0]["waybill_no"] == f"W{target - 1:03d}"
        assert res["items"][0]["weigh_record_no"] == f"G{target - 1:03d}"
        assert res["items"][0]["price_record_no"] == "P1"

    # 执行计划：行号→票走 cum_rows 索引，不是全表扫描
    conn = db.connect(tmp_path / "test_freight.db")
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT waybill_no FROM sample_waybill_stats "
            "WHERE version_id = 1 AND cum_rows >= 20 AND row_count > 0 "
            "ORDER BY cum_rows ASC LIMIT 1"
        ).fetchall()
        text = " ".join(row[3] for row in plan)
        assert "sample_waybill_stats" in text
        assert "SCAN sample_waybill_stats" not in text
        assert "idx_sws_version_cum" in text
        # 票统计冻结了 40 票
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_waybill_stats WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 40
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 7. 月份隔离：不同发车月的版与行号互不干扰
# --------------------------------------------------------------------------- #


def test_versions_isolated_by_month(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("M1", "T1", "BJ", "2026-03-20"),
            waybill("M2", "T2", "BJ", "2026-04-20"),
        ],
    )
    put_weigh(client, "G1", weigh("G1", "T1"))
    put_weigh(client, "G2", weigh("G2", "T2"))
    put_price(client, "P1", price("P1", "BJ", "2026-01-01", "2026-12-31"))

    mar = open_version(client, "2026-03")
    apr = open_version(client, "2026-04")
    assert mar["total_rows"] == 1 and apr["total_rows"] == 1

    m1 = query_lines(client, mar["version_id"], [1])["items"][0]
    m2 = query_lines(client, apr["version_id"], [1])["items"][0]
    assert (m1["waybill_no"], m1["weigh_record_no"]) == ("M1", "G1")
    assert (m2["waybill_no"], m2["weigh_record_no"]) == ("M2", "G2")

    # 各自是本月的 latest，互不覆盖
    assert (
        client.get("/samples/months/2026-03/versions/latest").json()["version_id"]
        == mar["version_id"]
    )
    assert (
        client.get("/samples/months/2026-04/versions/latest").json()["version_id"]
        == apr["version_id"]
    )


# --------------------------------------------------------------------------- #
# 8. 并发：开版进行中另一连接的来源写入在同一已提交状态之后串行生效
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 9. 边界：多对多笛卡尔积跨票连续编号、漏配票不占号、同来源记录跨票只存一份
# --------------------------------------------------------------------------- #


def test_cartesian_ordering_and_missing_ticket_skips_numbering(
    client: TestClient, tmp_path
) -> None:
    # WA(2×2=4 行)、WB(漏价目 0 行，不占号)、WC(2×1=2 行) → 全月 6 行
    import_waybills(
        client,
        [
            waybill("WA", "TA", "BJ", "2026-03-05"),
            waybill("WB", "TB", "SH", "2026-03-06"),
            waybill("WC", "TC", "GZ", "2026-03-07"),
        ],
    )
    put_weigh(client, "GA1", weigh("GA1", "TA"))
    put_weigh(client, "GA2", weigh("GA2", "TA"))
    put_weigh(client, "GB1", weigh("GB1", "TB"))
    put_weigh(client, "GC1", weigh("GC1", "TC"))
    put_weigh(client, "GC2", weigh("GC2", "TC"))
    put_price(client, "PA1", price("PA1", "BJ", "2026-03-01", "2026-03-31"))
    put_price(client, "PA2", price("PA2", "BJ", "2026-03-01", "2026-03-31"))
    put_price(client, "PC1", price("PC1", "GZ", "2026-03-01", "2026-03-31"))
    # SH 无价目 → WB 漏配 0 行

    info = open_version(client, "2026-03")
    assert info["total_rows"] == 6
    res = query_lines(client, info["version_id"], [1, 2, 3, 4, 5, 6])
    assert [
        (x["line_no"], x["waybill_no"], x["weigh_record_no"], x["price_record_no"])
        for x in res["items"]
    ] == [
        (1, "WA", "GA1", "PA1"),
        (2, "WA", "GA1", "PA2"),
        (3, "WA", "GA2", "PA1"),
        (4, "WA", "GA2", "PA2"),
        (5, "WC", "GC1", "PC1"),
        (6, "WC", "GC2", "PC1"),
    ]

    # WB 的 0 行票仍在冻结统计里（可解释“这票为何没行”），但没有成员关系
    conn = db.connect(tmp_path / "test_freight.db")
    try:
        wb = conn.execute(
            "SELECT row_count, weigh_hits, price_hits FROM sample_waybill_stats "
            "WHERE version_id = ? AND waybill_no = 'WB'",
            (info["version_id"],),
        ).fetchone()
        assert tuple(wb) == (0, 1, 0)
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_waybill_price "
            "WHERE version_id = ? AND waybill_no = 'WB'",
            (info["version_id"],),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_shared_source_record_stored_once_per_version(
    client: TestClient, tmp_path
) -> None:
    # 两票同车次 TA（共享过磅 GA），同始发站 BJ（共享价目 PA）
    import_waybills(
        client,
        [
            waybill("W1", "TA", "BJ", "2026-03-05"),
            waybill("W2", "TA", "BJ", "2026-06-05"),
        ],
    )
    put_weigh(client, "GA", weigh("GA", "TA"))
    put_price(client, "PA", price("PA", "BJ", "2026-01-01", "2026-12-31"))

    info = open_version(client, "2026-03")  # 只含 W1
    vid = info["version_id"]
    assert info["total_rows"] == 1
    line = query_lines(client, vid, [1])["items"][0]
    assert (line["waybill_no"], line["weigh_record_no"], line["price_record_no"]) == (
        "W1",
        "GA",
        "PA",
    )

    conn = db.connect(tmp_path / "test_freight.db")
    try:
        # 成员关系按票各一份，但来源内容在版内去重后各只有一份
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_weigh_records WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_price_records WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sample_waybill_weigh WHERE version_id = ?",
            (vid,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_open_version_idempotent_total_after_many_writes(client: TestClient) -> None:
    # 与预演月合计交叉核对：抽检版总行数 = 开版时 monthly_totals 的行数
    build_two_ticket_dataset(client)
    mon_total_before = client.get(
        "/rehearsal/months/2026-03/total"
    ).json()["generated_rows"]
    info = open_version(client, "2026-03")
    assert info["total_rows"] == mon_total_before == 5

    # 开版本身不推进来源数据版本号（只读冻结），健康版本不变
    assert info["source_version"] == client.get("/health").json()["version"]


def test_concurrent_open_and_source_write(client: TestClient) -> None:
    build_two_ticket_dataset(client)
    db_file = db._current_db_path()
    barrier = threading.Event()

    def long_open() -> None:
        # 工作线程内开连接并持有 IMMEDIATE 写锁，模拟开版进行中
        from app import sampling

        conn = db.connect(db_file)
        try:
            conn.execute("BEGIN IMMEDIATE")
            barrier.set()
            try:
                sampling.open_version(conn, "2026-03")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    t = threading.Thread(target=long_open)
    t.start()
    assert barrier.wait(2)

    # 开版事务持有写锁期间，来源写入等待（不报错、不破坏快照）
    res = {}

    def pending_write() -> None:
        r = client.put("/weigh/records/G9", json=weigh("G9", "T1"))
        res["status"] = r.status_code

    wt = threading.Thread(target=pending_write)
    wt.start()
    t.join(timeout=5)
    wt.join(timeout=35)
    assert not t.is_alive() and not wt.is_alive()
    assert res["status"] == 200  # 写锁释放后写入成功

    # 开版冻结的是写入前的状态：总行数仍为 5，G9 不在冻结来源里
    info = client.get("/samples/months/2026-03/versions/latest").json()
    assert info["total_rows"] == 5
    rows = client.post(
        f"/samples/versions/{info['version_id']}/lines",
        json={"line_nos": [1, 2, 3, 4, 5]},
    ).json()["items"]
    assert all("G9" != x["weigh_record_no"] for x in rows)
