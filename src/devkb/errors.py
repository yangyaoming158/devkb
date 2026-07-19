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


class DatabaseError(DevKbError):
    """数据库连接/事务/查询失败的稳定包装（《P1实现规格》§13）。"""

    code = "DATABASE_ERROR"


class InternalError(DevKbError):
    """未预期异常的稳定包装；error_id 用于与 stderr 日志关联（《P1实现规格》§13）。"""

    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, error_id: str) -> None:
        super().__init__(message)
        self.error_id = error_id


class InvalidInputError(DevKbError):
    """输入越限（问题长度/top_k 等，《P1实现规格》§13）。"""

    code = "INVALID_INPUT"
