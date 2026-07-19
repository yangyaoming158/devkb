"""全测试通用夹具。

CLI 测试经 CliRunner 触发 configure_logging 时，structlog 会缓存 pytest 的
临时 stderr（cache_logger_on_first_use=True）；该流在用例结束后被关闭，
后续任何测试打日志都会 ValueError: I/O operation on closed file。
每个用例后重置为 structlog 默认配置（不缓存、写当前 stdout）以隔离影响。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()
