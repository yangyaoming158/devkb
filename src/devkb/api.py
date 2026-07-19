"""FastAPI app 与 3 个路由（T19.2/T19.3，《P1实现规格》§12.2）；不放业务逻辑。

所有业务经 AppService（service.py）；本文件只做 Pydantic 输入校验、
DevKbError → HTTP 状态码/稳定错误体映射。同步返回，无 SSE。
错误体形状：{"error": {"code", "message", "error_id"}}——error_id 与 stderr
日志可关联（《P1实现规格》§13）。

P1 无鉴权：默认只监听 127.0.0.1，不得直接暴露公网（README）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import structlog
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from devkb.agent.state import MAX_QUESTION_CHARS
from devkb.errors import DevKbError, InternalError
from devkb.retrieval import MAX_FINAL_TOP_K
from devkb.service import AppService

logger = structlog.get_logger(__name__)

_STATUS_BY_CODE = {
    "INVALID_INPUT": 400,
    "NOT_FOUND": 404,
    "LLM_ERROR": 502,
    "LLM_RESPONSE_FORMAT": 502,
    "LLM_TIMEOUT": 504,
    "DATABASE_ERROR": 503,
    "EMBEDDING_ERROR": 503,
}

# 严格 slug 校验（《P1实现规格》§13）：小写字母/数字开头，其后允许 - _
ProjectSlug = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
]


class AskRequest(BaseModel):
    project: ProjectSlug
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    ]
    top_k: Annotated[int, Field(ge=1, le=MAX_FINAL_TOP_K)] | None = None


def _error_response(exc: DevKbError) -> JSONResponse:
    error_id = exc.error_id if isinstance(exc, InternalError) else uuid.uuid4().hex[:12]
    status = _STATUS_BY_CODE.get(exc.code, 500)
    logger.error("api_error", code=exc.code, status=status, error_id=error_id, message=str(exc))
    return JSONResponse(
        status_code=status,
        content={"error": {"code": exc.code, "message": str(exc), "error_id": error_id}},
    )


def create_app(service: AppService | None = None) -> FastAPI:
    """service 缺省时在 lifespan 内按配置自建并负责关闭；注入的由调用方管理。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned: AppService | None = None
        if getattr(app.state, "service", None) is None:
            from devkb.config import get_settings

            owned = AppService(get_settings())
            app.state.service = owned
        try:
            yield
        finally:
            if owned is not None:
                await owned.aclose()

    app = FastAPI(title="devkb", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service

    @app.exception_handler(DevKbError)
    async def devkb_error_handler(request: Request, exc: DevKbError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 覆盖 FastAPI 内置 422 detail：统一稳定错误体，且只报字段位置——
        # 内置 detail 会回显 input（含完整问题正文），违反 §13 不回显/不泄漏纪律
        error_id = uuid.uuid4().hex[:12]
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        logger.error("api_validation_error", error_id=error_id, fields=fields)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_INPUT",
                    "message": f"请求参数校验失败：{'、'.join(fields)}",
                    "error_id": error_id,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        error_id = uuid.uuid4().hex[:12]
        logger.error(
            "api_unexpected_error", error_id=error_id, error=f"{type(exc).__name__}: {exc}"
        )
        return _error_response(InternalError(f"内部错误（error_id={error_id}）", error_id=error_id))

    @app.post("/ask")
    async def ask(request: Request, body: AskRequest) -> dict[str, Any]:
        service: AppService = request.app.state.service
        # shield：客户端断开（连接取消）不打断已建立的 run，终态仍会写库（T19.4）
        return await asyncio.shield(service.ask(body.project, body.question, top_k=body.top_k))

    @app.get("/runs/{run_id}")
    async def get_run(
        request: Request,
        run_id: uuid.UUID,
        project: Annotated[ProjectSlug, Query()],
    ) -> dict[str, Any]:
        service: AppService = request.app.state.service
        return await service.run_trace(project, str(run_id))

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        service: AppService = request.app.state.service
        return await service.healthz()

    return app
