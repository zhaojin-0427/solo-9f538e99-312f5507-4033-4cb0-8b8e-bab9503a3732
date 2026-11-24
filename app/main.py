"""FastAPI 入口：运费入仓预演。

写接口：来源 + 预演结果在同一事务提交；异常时整体回滚，旧结果仍可查。
读接口：月合计与异常分页在同一只读快照中读取同一已提交版本。
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db, service
from .models import (
    AnomalyPage,
    Health,
    MonthTotal,
    Overview,
    PriceIn,
    RehearsalWaybill,
    WaybillBatch,
    WeighIn,
    WriteResult,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="运费入仓预演",
    version="1.0.0",
    description=(
        "一张运单关联过磅单（按车次）与价目（按始发站+发车日，有效期含首尾）"
        "两张来源表，预演两次内连接的计费行数与漏配/重复异常。"
        "来源与预演结果同事务原子生效；查询只读预演结果表。"
    ),
    lifespan=lifespan,
)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, service.NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, service.ConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, service.RehearsalError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    raise exc


def _write(conn: sqlite3.Connection, fn) -> WriteResult:
    try:
        with db.immediate(conn):
            result = fn(conn)
        return WriteResult(**result)
    except Exception as exc:  # noqa: BLE001 - 统一转 HTTP 错误，事务已回滚
        raise _translate(exc)


# --------------------------------------------------------------------------- #
# 运单
# --------------------------------------------------------------------------- #


@app.post(
    "/waybills/import",
    response_model=WriteResult,
    status_code=status.HTTP_200_OK,
    tags=["来源-运单"],
    summary="一次导入一批运单（每票唯一单号/车次/始发站/发车日）",
)
def import_waybills(
    batch: WaybillBatch, conn: sqlite3.Connection = Depends(db.get_connection)
):
    """批次内或库内若有重复运单号，整批拒绝（409），已有结果不受影响。"""
    return _write(conn, lambda c: service.import_waybills(c, batch.items))


# --------------------------------------------------------------------------- #
# 过磅单
# --------------------------------------------------------------------------- #


@app.put(
    "/weigh/records/{record_no}",
    response_model=WriteResult,
    tags=["来源-过磅单"],
    summary="补录或替换过磅单（按记录号 upsert，按车次匹配运单）",
)
def upsert_weigh(
    record_no: str,
    data: WeighIn,
    conn: sqlite3.Connection = Depends(db.get_connection),
):
    if record_no.strip() != data.record_no:
        raise HTTPException(
            status_code=422, detail="路径记录号与请求体 record_no 不一致"
        )
    return _write(conn, lambda c: service.upsert_weigh(c, data))


@app.delete(
    "/weigh/records/{record_no}",
    response_model=WriteResult,
    tags=["来源-过磅单"],
    summary="撤回过磅单",
)
def withdraw_weigh(
    record_no: str, conn: sqlite3.Connection = Depends(db.get_connection)
):
    return _write(conn, lambda c: service.withdraw_weigh(c, record_no.strip()))


# --------------------------------------------------------------------------- #
# 价目
# --------------------------------------------------------------------------- #


@app.put(
    "/price/records/{record_no}",
    response_model=WriteResult,
    tags=["来源-价目"],
    summary="补录或替换价目（按记录号 upsert，有效期含首尾两日）",
)
def upsert_price(
    record_no: str,
    data: PriceIn,
    conn: sqlite3.Connection = Depends(db.get_connection),
):
    if record_no.strip() != data.record_no:
        raise HTTPException(
            status_code=422, detail="路径记录号与请求体 record_no 不一致"
        )
    return _write(conn, lambda c: service.upsert_price(c, data))


@app.delete(
    "/price/records/{record_no}",
    response_model=WriteResult,
    tags=["来源-价目"],
    summary="撤还价目",
)
def withdraw_price(
    record_no: str, conn: sqlite3.Connection = Depends(db.get_connection)
):
    return _write(conn, lambda c: service.withdraw_price(c, record_no.strip()))


# --------------------------------------------------------------------------- #
# 预演结果查询
# --------------------------------------------------------------------------- #


@app.get(
    "/rehearsal/months/{departure_mon}/total",
    response_model=MonthTotal,
    tags=["预演结果"],
    summary="按发车月返回两次内连接生成的总行数（读预聚合，不逐单扫描）",
)
def get_month_total(
    departure_mon: str,
    conn: sqlite3.Connection = Depends(db.get_connection),
):
    with db.snapshot(conn):
        return service.month_total(conn, departure_mon)


@app.get(
    "/rehearsal/anomalies",
    response_model=AnomalyPage,
    tags=["预演结果"],
    summary="分页列出任一侧命中数不等于 1 的运单及两侧命中数",
)
def list_anomalies(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, description="上一页返回的 next_cursor"),
    departure_mon: str | None = Query(default=None, description="可选：限定发车月"),
    conn: sqlite3.Connection = Depends(db.get_connection),
):
    with db.snapshot(conn):
        return service.anomaly_page(conn, limit, cursor, departure_mon)


@app.get(
    "/rehearsal/months/{departure_mon}/overview",
    response_model=Overview,
    tags=["预演结果"],
    summary="同一次查询的月合计与异常页（同一已提交快照、同一版本号）",
)
def get_overview(
    departure_mon: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    conn: sqlite3.Connection = Depends(db.get_connection),
):
    with db.snapshot(conn):
        return service.overview(conn, departure_mon, limit, cursor)


@app.get(
    "/rehearsal/waybills/{waybill_no}",
    response_model=RehearsalWaybill,
    tags=["预演结果"],
    summary="查询单票运单的预演命中情况",
)
def get_waybill(
    waybill_no: str,
    conn: sqlite3.Connection = Depends(db.get_connection),
):
    with db.snapshot(conn):
        try:
            return service.rehearsal_detail(conn, waybill_no.strip())
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))


# --------------------------------------------------------------------------- #
# 管理/校验
# --------------------------------------------------------------------------- #


@app.post(
    "/admin/rebuild",
    response_model=WriteResult,
    tags=["管理"],
    summary="全量重建预演结果（仅用于核对，正常写入走增量）",
)
def rebuild(conn: sqlite3.Connection = Depends(db.get_connection)):
    return _write(conn, service.full_rebuild)


@app.get("/health", response_model=Health, tags=["管理"])
def health(conn: sqlite3.Connection = Depends(db.get_connection)):
    with db.snapshot(conn):
        return Health(
            status="ok",
            version=service.get_version(conn),
            db_path=str(db._current_db_path()),
        )


@app.exception_handler(sqlite3.Error)
def _sqlite_error(_request, exc: sqlite3.Error):  # pragma: no cover - 兜底
    return JSONResponse(
        status_code=500,
        content={"detail": f"数据库错误，事务已回滚: {exc}"},
    )
