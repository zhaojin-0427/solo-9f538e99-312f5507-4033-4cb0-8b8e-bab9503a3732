"""pytest 夹具：每个用例独立临时 SQLite 库，覆盖 FastAPI 连接依赖。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_file = tmp_path / "test_freight.db"
    db.set_db_path(db_file)
    db.init_db(db_file)

    def _override() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_file)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[db.get_connection] = _override
    # 不触发 startup（库已初始化），避免 lifespan 去碰默认路径。
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()
    db.set_db_path(None)
