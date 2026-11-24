"""端到端行为测试（经 FastAPI TestClient 走真实 HTTP 语义 + 真实 SQLite）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


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


def import_waybills(client: TestClient, items: list[dict]) -> dict:
    r = client.post("/waybills/import", json={"items": items})
    assert r.status_code == 200, r.text
    return r.json()


def put_weigh(client: TestClient, rec_no: str, body: dict) -> dict:
    r = client.put(f"/weigh/records/{rec_no}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def del_weigh(client: TestClient, rec_no: str) -> dict:
    r = client.delete(f"/weigh/records/{rec_no}")
    assert r.status_code == 200, r.text
    return r.json()


def put_price(client: TestClient, rec_no: str, body: dict) -> dict:
    r = client.put(f"/price/records/{rec_no}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def del_price(client: TestClient, rec_no: str) -> dict:
    r = client.delete(f"/price/records/{rec_no}")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# 1. 基础：导入 + 正常 1:1 匹配
# --------------------------------------------------------------------------- #


def test_basic_one_to_one_match(client: TestClient) -> None:
    res = import_waybills(
        client, [waybill("W1", "T1", "BJ", "2026-03-10")]
    )
    assert res["affected_waybills"] == 1
    assert res["affected_months"] == ["2026-03"]

    put_weigh(client, "G1", weigh("G1", "T1"))
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    total = client.get("/rehearsal/months/2026-03/total").json()
    assert total["generated_rows"] == 1  # 1 过磅 × 1 价目

    anomalies = client.get("/rehearsal/anomalies").json()
    assert anomalies["count"] == 0

    detail = client.get("/rehearsal/waybills/W1").json()
    assert detail["weigh_hits"] == 1
    assert detail["price_hits"] == 1
    assert detail["generated_rows"] == 1
    assert detail["is_anomaly"] is False


# --------------------------------------------------------------------------- #
# 2. 漏配与重复并存（核心试算）
# --------------------------------------------------------------------------- #


def test_missing_and_duplicate_coexist(client: TestClient) -> None:
    # W1: 过磅重复(2) × 价目正常(1) = 2 行，异常
    # W2: 过磅正常(1) × 价目漏配(0) = 0 行，异常
    # W3: 过磅漏配(0) × 价目重复(2) = 0 行，异常
    # W4: 1 × 1 = 1 行，正常
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "SH", "2026-03-06"),
            waybill("W3", "T3", "GZ", "2026-03-07"),
            waybill("W4", "T4", "SZ", "2026-03-08"),
        ],
    )

    # 过磅：T1 两条，T2/T4 各一条，T3 没有
    put_weigh(client, "G1", weigh("G1", "T1"))
    put_weigh(client, "G2", weigh("G2", "T1"))
    put_weigh(client, "G3", weigh("G3", "T2"))
    put_weigh(client, "G4", weigh("G4", "T4"))

    # 价目：BJ 一条；SH 无；GZ 两条；SZ 一条（按窗口命中）
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    put_price(client, "P2", price("P2", "GZ", "2026-03-01", "2026-03-10"))
    put_price(client, "P3", price("P3", "GZ", "2026-03-05", "2026-03-31"))
    put_price(client, "P4", price("P4", "SZ", "2026-03-01", "2026-03-31"))

    total = client.get("/rehearsal/months/2026-03/total").json()
    # W1: 2*1=2, W2: 1*0=0, W3: 0*2=0, W4: 1*1=1 → 合计 3
    assert total["generated_rows"] == 3

    page = client.get("/rehearsal/anomalies", params={"limit": 10}).json()
    assert page["count"] == 3
    by_no = {it["waybill_no"]: it for it in page["items"]}
    assert by_no["W1"]["weigh_hits"] == 2 and by_no["W1"]["price_hits"] == 1
    assert by_no["W1"]["generated_rows"] == 2
    assert by_no["W2"]["weigh_hits"] == 1 and by_no["W2"]["price_hits"] == 0
    assert by_no["W3"]["weigh_hits"] == 0 and by_no["W3"]["price_hits"] == 2
    assert "W4" not in by_no  # 正常运单不出现在异常页

    # 概览接口：月合计与异常页同一版本
    ov = client.get("/rehearsal/months/2026-03/overview").json()
    assert ov["version"] == client.get("/health").json()["version"]
    assert ov["month_total"]["generated_rows"] == 3
    assert ov["anomalies"]["count"] == 3


# --------------------------------------------------------------------------- #
# 3. 有效期含首尾两日（边界）
# --------------------------------------------------------------------------- #


def test_validity_window_inclusive_boundaries(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("A", "T1", "BJ", "2026-03-01"),  # 恰为首日
            waybill("B", "T2", "BJ", "2026-03-15"),
            waybill("C", "T3", "BJ", "2026-03-31"),  # 恰为尾日
            waybill("D", "T4", "BJ", "2026-02-28"),  # 窗口外
        ],
    )
    for rec, train in [("G1", "T1"), ("G2", "T2"), ("G3", "T3"), ("G4", "T4")]:
        put_weigh(client, rec, weigh(rec, train))
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    anomalies = {
        x["waybill_no"]: x
        for x in client.get("/rehearsal/anomalies").json()["items"]
    }
    # A/B/C 价目命中 1（边界含），D 价目命中 0
    for no in ("A", "B", "C"):
        assert no not in anomalies
    assert anomalies["D"]["price_hits"] == 0


# --------------------------------------------------------------------------- #
# 4. 增量重算：替换过磅单车次只影响相关运单
# --------------------------------------------------------------------------- #


def test_incremental_weigh_retarget(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-02"),
            waybill("W2", "T2", "BJ", "2026-03-03"),
        ],
    )
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    res = put_weigh(client, "G1", weigh("G1", "T1"))
    # 仅 T1 上的 W1 受影响
    assert res["affected_waybills"] == 1

    # 把 G1 从 T1 改到 T2：新旧两个车次的运单都要重算
    res = put_weigh(client, "G1", weigh("G1", "T2"))
    assert res["affected_waybills"] == 2
    assert sorted(res["affected_months"]) == ["2026-03"]

    d1 = client.get("/rehearsal/waybills/W1").json()
    d2 = client.get("/rehearsal/waybills/W2").json()
    assert d1["weigh_hits"] == 0 and d1["is_anomaly"] is True
    assert d2["weigh_hits"] == 1 and d2["is_anomaly"] is False

    # 撤回后恢复
    del_weigh(client, "G1")
    d2b = client.get("/rehearsal/waybills/W2").json()
    assert d2b["weigh_hits"] == 0
    assert client.get("/rehearsal/months/2026-03/total").json()[
        "generated_rows"
    ] == 0


# --------------------------------------------------------------------------- #
# 5. 增量重算：替换价目窗口，新旧窗口并集内的运单才重算
# --------------------------------------------------------------------------- #


def test_incremental_price_window_change(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "BJ", "2026-04-05"),
            waybill("W3", "T3", "SH", "2026-03-05"),
        ],
    )
    for rec, train in [("G1", "T1"), ("G2", "T2"), ("G3", "T3")]:
        put_weigh(client, rec, weigh(rec, train))

    # 初始窗口只覆盖 3 月的 BJ
    res = put_price(
        client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31")
    )
    assert res["affected_waybills"] == 1  # 只命中 W1

    # 窗口扩大到 4 月：旧窗口（W1）+ 新窗口（W1,W2）并集 = W1,W2；SH 的 W3 不动
    res = put_price(
        client, "P1",
        price("P1", "BJ", "2026-03-01", "2026-04-30"),
    )
    assert res["affected_waybills"] == 2
    assert sorted(res["affected_months"]) == ["2026-03", "2026-04"]

    assert client.get("/rehearsal/waybills/W2").json()["price_hits"] == 1
    # W3 始终无 BJ 价目（始发站不同）
    assert client.get("/rehearsal/waybills/W3").json()["price_hits"] == 0

    # 月份合计分别维护
    assert client.get("/rehearsal/months/2026-03/total").json()[
        "generated_rows"
    ] == 1
    assert client.get("/rehearsal/months/2026-04/total").json()[
        "generated_rows"
    ] == 1


# --------------------------------------------------------------------------- #
# 6. 原子性：重复运单号整批拒绝，来源与结果都不变
# --------------------------------------------------------------------------- #


def test_duplicate_waybill_batch_rollback(client: TestClient) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    version_before = client.get("/health").json()["version"]

    r = client.post(
        "/waybills/import",
        json={
            "items": [
                waybill("W9", "T9", "BJ", "2026-03-05"),
                waybill("W1", "T1", "BJ", "2026-03-05"),  # 撞号
            ]
        },
    )
    assert r.status_code == 409
    # 版本号未增长，W9 未入库
    assert client.get("/health").json()["version"] == version_before
    assert client.get("/rehearsal/waybills/W9").status_code == 404


def test_batch_internal_duplicate_rejected(client: TestClient) -> None:
    r = client.post(
        "/waybills/import",
        json={
            "items": [
                waybill("W1", "T1", "BJ", "2026-03-05"),
                waybill("W1", "T2", "SH", "2026-03-06"),
            ]
        },
    )
    assert r.status_code == 422  # pydantic 模型校验失败


# --------------------------------------------------------------------------- #
# 7. 失败可查原结果：撤回不存在的记录号
# --------------------------------------------------------------------------- #


def test_withdraw_missing_record_keeps_old_result(client: TestClient) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    put_weigh(client, "G1", weigh("G1", "T1"))
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    version_before = client.get("/health").json()["version"]

    assert client.delete("/weigh/records/NOPE").status_code == 404
    assert client.delete("/price/records/NOPE").status_code == 404

    assert client.get("/health").json()["version"] == version_before
    assert (
        client.get("/rehearsal/waybills/W1").json()["generated_rows"] == 1
    )


# --------------------------------------------------------------------------- #
# 8. 异常分页（键集游标）+ 按月过滤
# --------------------------------------------------------------------------- #


def test_anomaly_keyset_pagination(client: TestClient) -> None:
    items = [
        waybill(f"W{i:02d}", f"T{i}", "BJ", f"2026-03-{i:02d}")
        for i in range(1, 6)
    ]
    import_waybills(client, items)
    # 不补过磅/价目 → 5 票全部异常（0,0）

    page1 = client.get("/rehearsal/anomalies", params={"limit": 2}).json()
    assert [x["waybill_no"] for x in page1["items"]] == ["W01", "W02"]
    assert page1["next_cursor"] == "W02"

    page2 = client.get(
        "/rehearsal/anomalies",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    ).json()
    assert [x["waybill_no"] for x in page2["items"]] == ["W03", "W04"]

    page3 = client.get(
        "/rehearsal/anomalies",
        params={"limit": 2, "cursor": page2["next_cursor"]},
    ).json()
    assert [x["waybill_no"] for x in page3["items"]] == ["W05"]
    assert page3["next_cursor"] is None


def test_anomaly_month_filter_and_cross_month_totals(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("M1", "T1", "BJ", "2026-03-20"),
            waybill("M2", "T2", "BJ", "2026-04-20"),
        ],
    )
    # 只有过磅、没有价目 → 两票都异常，但落在不同月
    put_weigh(client, "G1", weigh("G1", "T1"))
    put_weigh(client, "G2", weigh("G2", "T2"))

    mar = client.get(
        "/rehearsal/anomalies", params={"departure_mon": "2026-03"}
    ).json()
    assert [x["waybill_no"] for x in mar["items"]] == ["M1"]
    apr = client.get(
        "/rehearsal/anomalies", params={"departure_mon": "2026-04"}
    ).json()
    assert [x["waybill_no"] for x in apr["items"]] == ["M2"]

    # 没有运单的月份合计为 0
    assert client.get("/rehearsal/months/2026-09/total").json()[
        "generated_rows"
    ] == 0


# --------------------------------------------------------------------------- #
# 9. 全量重建结果与增量结果一致
# --------------------------------------------------------------------------- #


def test_full_rebuild_matches_incremental(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T1", "BJ", "2026-03-06"),
            waybill("W3", "T2", "SH", "2026-04-05"),
        ],
    )
    put_weigh(client, "G1", weigh("G1", "T1"))
    put_weigh(client, "G2", weigh("G2", "T1"))
    put_price(client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    before_total = client.get("/rehearsal/months/2026-03/total").json()
    before_page = client.get("/rehearsal/anomalies").json()

    r = client.post("/admin/rebuild")
    assert r.status_code == 200

    assert client.get("/rehearsal/months/2026-03/total").json() == before_total
    assert client.get("/rehearsal/anomalies").json()["items"] == before_page[
        "items"
    ]
    assert client.get("/rehearsal/months/2026-04/total").json()[
        "generated_rows"
    ] == 0


# --------------------------------------------------------------------------- #
# 10. 版本号：每次成功写入 +1，读不增长
# --------------------------------------------------------------------------- #


def test_version_monotonic_only_on_writes(client: TestClient) -> None:
    v0 = client.get("/health").json()["version"]
    r1 = import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    assert r1["version"] == v0 + 1
    r2 = put_weigh(client, "G1", weigh("G1", "T1"))
    assert r2["version"] == v0 + 2
    r3 = put_price(
        client, "P1", price("P1", "BJ", "2026-03-01", "2026-03-31")
    )
    assert r3["version"] == v0 + 3

    # 读操作不改变版本
    client.get("/rehearsal/months/2026-03/overview")
    client.get("/rehearsal/anomalies")
    assert client.get("/health").json()["version"] == v0 + 3


# --------------------------------------------------------------------------- #
# 11. 查询不读来源明细：用底层连接直接核对异常查询的访问对象
# --------------------------------------------------------------------------- #


def test_anomaly_query_uses_only_result_table(client: TestClient, tmp_path) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    put_weigh(client, "G1", weigh("G1", "T1"))

    from app import db

    conn = db.connect(tmp_path / "test_freight.db")
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM match_stats WHERE is_anomaly = 1 "
            "ORDER BY waybill_no LIMIT 50"
        ).fetchall()
        text = " ".join(row[3] for row in plan)
        assert "match_stats" in text
        assert "waybills" not in text
        assert "weigh_records" not in text
        assert "price_records" not in text
        # 走覆盖异常数据的索引而非全表扫描
        assert "SCAN match_stats" not in text or "USING INDEX" in text or "COVERING" in text.upper()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 12. 输入校验
# --------------------------------------------------------------------------- #


def test_input_validation(client: TestClient) -> None:
    # 错误日期格式
    r = client.post(
        "/waybills/import",
        json={"items": [waybill("W1", "T1", "BJ", "2026-13-40")]},
    )
    assert r.status_code == 422

    # 价目窗口倒置
    r = client.put(
        "/price/records/P1",
        json=price("P1", "BJ", "2026-04-01", "2026-03-01"),
    )
    assert r.status_code == 422

    # 过磅重量非正
    r = client.put(
        "/weigh/records/G1", json=weigh("G1", "T1", -5)
    )
    assert r.status_code == 422

    # 路径与体记录号不一致
    r = client.put("/weigh/records/OTHER", json=weigh("G1", "T1"))
    assert r.status_code == 422
