"""集成测试基座：临时数据库 + Alembic 迁移，测后销毁。

需要可达的 PostgreSQL（本地 `make up`；CI 用 service container）。
嵌入一律用确定性假向量——CI 禁真模型（CLAUDE.md 测试纪律）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from devkb.db import DEFAULT_DATABASE_URL, create_engine, create_session_factory

ROOT = Path(__file__).parents[2]


async def _admin_exec(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_db_url() -> Iterator[str]:
    base = make_url(os.environ.get("DEVKB_DATABASE_URL", DEFAULT_DATABASE_URL))
    admin_url = base.set(database="postgres").render_as_string(hide_password=False)
    test_db = f"devkb_test_{uuid.uuid4().hex[:8]}"
    test_url = base.set(database=test_db).render_as_string(hide_password=False)

    try:
        asyncio.run(_admin_exec(admin_url, f'CREATE DATABASE "{test_db}"'))
    except Exception as exc:
        pytest.fail(f"PostgreSQL 不可达（本地请先 make up）：{exc}")

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    prev = os.environ.get("DEVKB_DATABASE_URL")
    os.environ["DEVKB_DATABASE_URL"] = test_url
    try:
        command.upgrade(cfg, "head")
        yield test_url
    finally:
        if prev is None:
            os.environ.pop("DEVKB_DATABASE_URL", None)
        else:
            os.environ["DEVKB_DATABASE_URL"] = prev
        asyncio.run(_admin_exec(admin_url, f'DROP DATABASE "{test_db}" WITH (FORCE)'))


@pytest.fixture
async def session(migrated_db_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_engine(migrated_db_url)
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()
