"""T19.1 异常映射与 AppService 输入校验：底层异常 → 稳定 DevKbError（规格 §13）。

映射器不把底层错误串（DSN/主机/口令）带进用户可见消息；未预期异常
包装为 InternalError 并携带 error_id 供 stderr 日志关联。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import OperationalError

from devkb.config import Settings
from devkb.errors import DatabaseError, InternalError, InvalidInputError, NotFoundError
from devkb.service import AppService, map_errors


async def test_map_errors_passes_devkb_error_through() -> None:
    with pytest.raises(NotFoundError):
        async with map_errors():
            raise NotFoundError("项目不存在")


async def test_map_errors_wraps_sqlalchemy_error_without_leaking_details() -> None:
    cause = Exception("password=hunter2 host=10.0.0.1")
    with pytest.raises(DatabaseError) as exc_info:
        async with map_errors():
            raise OperationalError("SELECT 1", None, cause)
    assert exc_info.value.code == "DATABASE_ERROR"
    message = str(exc_info.value)
    assert "hunter2" not in message and "10.0.0.1" not in message


async def test_map_errors_wraps_oserror_as_database_error() -> None:
    with pytest.raises(DatabaseError) as exc_info:
        async with map_errors():
            raise ConnectionRefusedError("connect to 127.0.0.1:5432 refused")
    assert exc_info.value.code == "DATABASE_ERROR"
    assert "5432" not in str(exc_info.value)


async def test_map_errors_wraps_unexpected_as_internal_error_with_error_id() -> None:
    with pytest.raises(InternalError) as exc_info:
        async with map_errors():
            raise ValueError("boom with internal detail")
    error = exc_info.value
    assert error.code == "INTERNAL_ERROR"
    assert error.error_id and error.error_id in str(error)
    assert "boom" not in str(error)


def _service() -> AppService:
    # 端口 9（discard）永不监听：校验必须在任何数据库/模型访问之前拒绝
    settings = Settings(
        llm_api_key=SecretStr("unit-test-key"),
        database_url="postgresql+asyncpg://devkb:unit-test-pw@127.0.0.1:9/devkb",
    )
    return AppService(settings)


async def test_ask_rejects_invalid_pipeline_top_k_and_question_length() -> None:
    service = _service()
    try:
        with pytest.raises(InvalidInputError):
            await service.ask("demo", "问题", pipeline="agent-x")
        with pytest.raises(InvalidInputError):
            await service.ask("demo", "问题", top_k=0)
        with pytest.raises(InvalidInputError):
            await service.ask("demo", "问题", top_k=13)
        with pytest.raises(InvalidInputError):
            await service.ask("demo", "   ")
        with pytest.raises(InvalidInputError):
            await service.ask("demo", "长" * 4001)
    finally:
        await service.aclose()
