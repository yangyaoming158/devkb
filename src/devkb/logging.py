"""structlog 配置：dev 彩色控制台 / test 及非交互环境 JSON；敏感键脱敏。"""

from __future__ import annotations

import logging
import sys

import structlog
from structlog.typing import EventDict, WrappedLogger

_SENSITIVE_KEYS = {"api_key", "authorization", "password", "secret", "token"}


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(level: str = "INFO", app_env: str = "dev") -> None:
    logging.basicConfig(level=level.upper(), stream=sys.stderr, format="%(message)s")
    pretty = app_env == "dev" and sys.stderr.isatty()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _redact,
            structlog.dev.ConsoleRenderer() if pretty else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        cache_logger_on_first_use=True,
    )
