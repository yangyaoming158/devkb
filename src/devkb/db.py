"""数据库引擎与会话工厂（显式装配，不做全局单例）。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# 与 docker-compose.yml 默认值一致；config.py 与 alembic/env.py 均引用此常量
DEFAULT_DATABASE_URL = "postgresql+asyncpg://devkb:devkb-local@127.0.0.1:5432/devkb"


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
