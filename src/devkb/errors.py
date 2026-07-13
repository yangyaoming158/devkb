"""异常层级（《P0实现规格》§9）。CLI 顶层统一捕获渲染，退出码非 0。"""

from __future__ import annotations


class DevKbError(Exception):
    """项目异常基类，携带稳定错误码（日志与 CLI 渲染用）。"""

    code = "DEVKB_ERROR"


class ConfigError(DevKbError):
    code = "CONFIG_ERROR"


class IngestError(DevKbError):
    code = "INGEST_ERROR"


class ParseError(IngestError):
    code = "PARSE_ERROR"


class UnsupportedFileError(IngestError):
    code = "UNSUPPORTED_FILE"


class EmbeddingError(DevKbError):
    code = "EMBEDDING_ERROR"


class LLMError(DevKbError):
    code = "LLM_ERROR"


class LLMTimeoutError(LLMError):
    code = "LLM_TIMEOUT"


class LLMResponseFormatError(LLMError):
    code = "LLM_RESPONSE_FORMAT"


class NotFoundError(DevKbError):
    code = "NOT_FOUND"
