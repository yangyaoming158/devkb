"""T12.1 迁移可逆性与 schema 一致性（《P1实现规格》§5 迁移验收）。

在独立临时库执行 `upgrade head → downgrade 0001 → upgrade head`，全程验证
P0 四表种子数据不丢；升级后核对 P1 新列/索引/表与规格一致。
测试自建临时库，不复用 conftest 的会话级已迁移库（那个库不允许被降级）。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from devkb.db import DEFAULT_DATABASE_URL

ROOT = Path(__file__).parents[2]
EMBEDDING_LITERAL = "[" + ",".join(["0.5"] + ["0"] * 1023) + "]"  # vector(1024)，ADR-0002


async def _admin_exec(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


async def _execute(db_url: str, sql: str, params: dict[str, Any] | None = None) -> None:
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _fetch_all(
    db_url: str, sql: str, params: dict[str, Any] | None = None
) -> list[tuple[Any, ...]]:
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params or {})
            return [tuple(row) for row in result.all()]
    finally:
        await engine.dispose()


def _scalar(db_url: str, sql: str, params: dict[str, Any] | None = None) -> Any:
    rows = asyncio.run(_fetch_all(db_url, sql, params))
    assert len(rows) == 1, f"期望单行结果：{sql!r} 返回 {rows!r}"
    return rows[0][0]


def _alembic(db_url: str, action: str, target: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    prev = os.environ.get("DEVKB_DATABASE_URL")
    os.environ["DEVKB_DATABASE_URL"] = db_url
    try:
        if action == "upgrade":
            command.upgrade(cfg, target)
        elif action == "downgrade":
            command.downgrade(cfg, target)
        else:  # pragma: no cover - 防御未知动作
            raise ValueError(action)
    finally:
        if prev is None:
            os.environ.pop("DEVKB_DATABASE_URL", None)
        else:
            os.environ["DEVKB_DATABASE_URL"] = prev


@pytest.fixture
def fresh_db_url() -> Iterator[str]:
    base = make_url(os.environ.get("DEVKB_DATABASE_URL", DEFAULT_DATABASE_URL))
    admin_url = base.set(database="postgres").render_as_string(hide_password=False)
    db_name = f"devkb_mig_{uuid.uuid4().hex[:8]}"
    db_url = base.set(database=db_name).render_as_string(hide_password=False)
    try:
        asyncio.run(_admin_exec(admin_url, f'CREATE DATABASE "{db_name}"'))
    except Exception as exc:
        pytest.fail(f"PostgreSQL 不可达（本地请先 make up）：{exc}")
    yield db_url
    asyncio.run(_admin_exec(admin_url, f'DROP DATABASE "{db_name}" WITH (FORCE)'))


def _seed_p0_rows(db_url: str) -> dict[str, uuid.UUID]:
    ids = {
        "project": uuid.uuid4(),
        "document": uuid.uuid4(),
        "chunk_embedded": uuid.uuid4(),
        "chunk_plain": uuid.uuid4(),
        "run": uuid.uuid4(),
    }
    asyncio.run(
        _execute(
            db_url,
            "INSERT INTO projects (id, slug, name) VALUES (:id, 'mig-demo', '迁移演示')",
            {"id": ids["project"]},
        )
    )
    asyncio.run(
        _execute(
            db_url,
            "INSERT INTO documents (id, project_id, rel_path, title, doc_type, content_hash)"
            " VALUES (:id, :pid, 'docs/demo.md', 'Demo', 'markdown', 'hash-doc')",
            {"id": ids["document"], "pid": ids["project"]},
        )
    )
    for key, embedding in (("chunk_embedded", EMBEDDING_LITERAL), ("chunk_plain", None)):
        asyncio.run(
            _execute(
                db_url,
                "INSERT INTO chunks (id, project_id, document_id, ordinal, title_path,"
                " content, content_hash, token_count, start_line, end_line, embedding)"
                " VALUES (:id, :pid, :did, :ordinal, 'Demo > 小节', 'OrderService 创建订单',"
                " :hash, 7, 1, 3, CAST(:embedding AS vector))",
                {
                    "id": ids[key],
                    "pid": ids["project"],
                    "did": ids["document"],
                    "ordinal": 0 if key == "chunk_embedded" else 1,
                    "hash": f"hash-{key}",
                    "embedding": embedding,
                },
            )
        )
    asyncio.run(
        _execute(
            db_url,
            "INSERT INTO agent_runs (id, project_id, question, status)"
            " VALUES (:id, :pid, '演示问题？', 'succeeded')",
            {"id": ids["run"], "pid": ids["project"]},
        )
    )
    return ids


def _assert_p0_rows_intact(db_url: str, ids: dict[str, uuid.UUID]) -> None:
    counts = {
        table: _scalar(db_url, f"SELECT count(*) FROM {table}")
        for table in ("projects", "documents", "chunks", "agent_runs")
    }
    assert counts == {"projects": 1, "documents": 1, "chunks": 2, "agent_runs": 1}
    assert (
        _scalar(db_url, "SELECT content FROM chunks WHERE id = :id", {"id": ids["chunk_embedded"]})
        == "OrderService 创建订单"
    )
    # 嵌入向量逐字节回读一致（不丢即不需要重嵌，规格 §5.1）
    distance = _scalar(
        db_url,
        "SELECT embedding <=> CAST(:ref AS vector) FROM chunks WHERE id = :id",
        {"ref": EMBEDDING_LITERAL, "id": ids["chunk_embedded"]},
    )
    assert float(distance) == 0.0
    assert (
        _scalar(
            db_url,
            "SELECT embedding IS NULL FROM chunks WHERE id = :id",
            {"id": ids["chunk_plain"]},
        )
        is True
    )


def _column_names(db_url: str, table: str) -> set[str]:
    rows = asyncio.run(
        _fetch_all(
            db_url,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :table",
            {"table": table},
        )
    )
    return {row[0] for row in rows}


def _index_defs(db_url: str, table: str) -> dict[str, str]:
    rows = asyncio.run(
        _fetch_all(
            db_url,
            "SELECT indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = 'public' AND tablename = :table",
            {"table": table},
        )
    )
    return {row[0]: row[1] for row in rows}


def test_upgrade_downgrade_upgrade_preserves_p0_data(fresh_db_url: str) -> None:
    _alembic(fresh_db_url, "upgrade", "head")
    ids = _seed_p0_rows(fresh_db_url)

    # ---- downgrade 到 P0 基线：P1 扩展消失，P0 数据原样 ----
    _alembic(fresh_db_url, "downgrade", "0001")
    assert _scalar(fresh_db_url, "SELECT version_num FROM alembic_version") == "0001"
    existing_tables = {
        row[0]
        for row in asyncio.run(
            _fetch_all(
                fresh_db_url,
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
            )
        )
    }
    assert "agent_steps" not in existing_tables
    assert "tool_invocations" not in existing_tables
    assert {"search_text", "search_tsv"} & _column_names(fresh_db_url, "chunks") == set()
    _assert_p0_rows_intact(fresh_db_url, ids)

    # ---- 再升级到 head：数据仍在，新列默认值就位 ----
    _alembic(fresh_db_url, "upgrade", "head")
    assert _scalar(fresh_db_url, "SELECT version_num FROM alembic_version") == "0002"
    _assert_p0_rows_intact(fresh_db_url, ids)
    assert (
        _scalar(
            fresh_db_url,
            "SELECT count(*) FROM chunks WHERE search_text = ''",
        )
        == 2
    )


def test_head_schema_matches_p1_spec(fresh_db_url: str) -> None:
    _alembic(fresh_db_url, "upgrade", "head")
    ids = _seed_p0_rows(fresh_db_url)

    # §5.1 chunks 检索字段与索引
    chunk_columns = _column_names(fresh_db_url, "chunks")
    assert {"search_text", "search_tsv"} <= chunk_columns
    chunk_indexes = _index_defs(fresh_db_url, "chunks")
    assert "USING gin (search_tsv)" in chunk_indexes["ix_chunks_search_tsv"]
    hnsw = chunk_indexes["ix_chunks_embedding_hnsw"]
    assert "USING hnsw (embedding vector_cosine_ops)" in hnsw
    assert "m='16'" in hnsw and "ef_construction='64'" in hnsw
    assert "WHERE (embedding IS NOT NULL)" in hnsw

    # search_tsv 是生成列：写 search_text 即自动生效，无需 Repository 双写
    asyncio.run(
        _execute(
            fresh_db_url,
            "UPDATE chunks SET search_text = 'OrderService create_order 订单 创建' WHERE id = :id",
            {"id": ids["chunk_embedded"]},
        )
    )
    assert (
        _scalar(
            fresh_db_url,
            "SELECT search_tsv @@ plainto_tsquery('simple', 'OrderService')"
            " FROM chunks WHERE id = :id",
            {"id": ids["chunk_embedded"]},
        )
        is True
    )

    # §5.2 agent_steps：最小字段集 + unique(run_id, seq)
    assert _column_names(fresh_db_url, "agent_steps") == {
        "id",
        "project_id",
        "run_id",
        "seq",
        "node",
        "attempt",
        "status",
        "input_summary",
        "output_summary",
        "latency_ms",
        "error",
        "created_at",
    }
    unique_defs = [
        definition
        for definition in _index_defs(fresh_db_url, "agent_steps").values()
        if "UNIQUE" in definition and "(run_id, seq)" in definition
    ]
    assert len(unique_defs) == 1

    # §5.3 tool_invocations：最小字段集
    assert _column_names(fresh_db_url, "tool_invocations") == {
        "id",
        "project_id",
        "run_id",
        "step_id",
        "tool_name",
        "arguments",
        "result_summary",
        "status",
        "latency_ms",
        "error",
        "created_at",
    }
