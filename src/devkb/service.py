"""CLI/API 共用应用服务与异常映射（T19.1，《P1实现规格》§5/§13）。

CLI 与 FastAPI 都经 AppService 调用摄取/检索/问答逻辑，不各自复制装配；
数据库、模型加载、LLM SDK、解析异常统一映射为稳定 DevKbError——CLI 顶层据此
渲染错误码而非原始 traceback，API 返回稳定错误码与可关联 error_id（T19.2）。
底层错误细节只进 stderr 日志：DSN/主机名等可能含敏感信息，不回给用户。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.exc import SQLAlchemyError

from devkb.agent.service import agentic_answer_question, get_run_trace
from devkb.agent.state import MAX_QUESTION_CHARS
from devkb.answer import answer_question
from devkb.config import Settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import Embedder
from devkb.errors import (
    DatabaseError,
    DevKbError,
    EmbeddingError,
    InternalError,
    InvalidInputError,
    NotFoundError,
)
from devkb.llm import LLMClient
from devkb.repositories import ChunkRepo, ProjectRepo, RunRepo, get_alembic_version
from devkb.retrieval import MAX_FINAL_TOP_K

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from devkb.ingest.markdown import TokenCounter
    from devkb.ingest.pipeline import IngestReport
    from devkb.models import Project

logger = structlog.get_logger(__name__)

PIPELINES = ("agentic", "fixed-rag")


@asynccontextmanager
async def map_errors() -> AsyncIterator[None]:
    """把底层异常映射为稳定 DevKbError；已是 DevKbError 的原样透传。"""
    try:
        yield
    except DevKbError:
        raise
    except (SQLAlchemyError, OSError) as exc:
        logger.error("database_error", error=f"{type(exc).__name__}: {exc}")
        raise DatabaseError(f"数据库不可用或操作失败（{type(exc).__name__}）") from exc
    except Exception as exc:
        error_id = uuid.uuid4().hex[:12]
        logger.error("internal_error", error_id=error_id, error=f"{type(exc).__name__}: {exc}")
        raise InternalError(f"内部错误（error_id={error_id}）", error_id=error_id) from exc


class AppService:
    """长生命周期应用服务：持有 engine/session factory，Embedder/LLM 懒初始化。

    CLI 每条命令构造一个并 aclose()；API 在 lifespan 构造一个跨请求复用。
    embedder/llm/count_tokens 允许注入（FakeEmbedder/FakeLLM，CI 禁真模型）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        llm: LLMClient | None = None,
        count_tokens: TokenCounter | None = None,
    ) -> None:
        self._settings = settings
        self._engine = create_engine(settings.database_url)
        self._session_factory = create_session_factory(self._engine)
        self._embedder = embedder
        self._embedder_init_lock = asyncio.Lock()
        self._llm = llm
        self._count_tokens = count_tokens

    async def aclose(self) -> None:
        await self._engine.dispose()

    def _load_embedder(self) -> Embedder:
        """同步构造生产 Embedder（模型加载数秒级）；只经 _get_embedder 的线程调用。"""
        from devkb.embedding import SentenceTransformerEmbedder

        try:
            return SentenceTransformerEmbedder(
                self._settings.embedding_model_id,
                device=self._settings.embedding_device,
                batch_size=self._settings.embedding_batch_size,
            )
        except Exception as exc:
            raise EmbeddingError(f"嵌入模型加载失败：{type(exc).__name__}") from exc

    async def _get_embedder(self) -> Embedder:
        """懒初始化移入线程（首问模型加载不阻塞事件循环，T19.4 复查修复）。

        asyncio.Lock 保证并发首问只加载一次；后续命中缓存零开销。
        """
        if self._embedder is None:
            async with self._embedder_init_lock:
                if self._embedder is None:
                    self._embedder = await asyncio.to_thread(self._load_embedder)
        return self._embedder

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            from devkb.llm import OpenAICompatLLM

            self._llm = OpenAICompatLLM(
                api_key=self._settings.llm_api_key.get_secret_value(),
                base_url=self._settings.llm_base_url,
                model=self._settings.llm_model,
            )
        return self._llm

    def _get_count_tokens(self) -> TokenCounter:
        if self._count_tokens is None:
            from devkb.ingest.markdown import qwen_token_counter

            try:
                self._count_tokens = qwen_token_counter(self._settings.embedding_model_id)
            except Exception as exc:
                raise EmbeddingError(f"分词器加载失败：{type(exc).__name__}") from exc
        return self._count_tokens

    async def _resolve_project(self, session: AsyncSession, slug: str) -> Project:
        project = await ProjectRepo(session).get_by_slug(slug)
        if project is None:
            raise NotFoundError(f"项目 '{slug}' 不存在（先执行 devkb ingest）")
        return project

    async def ask(
        self,
        project_slug: str,
        question: str,
        *,
        top_k: int | None = None,
        pipeline: str = "agentic",
    ) -> dict[str, Any]:
        """问答入口（P1 默认 agentic，fixed-rag 为 P0 对照）；输入越限即拒。"""
        if pipeline not in PIPELINES:
            raise InvalidInputError(f"pipeline 只支持 {' | '.join(PIPELINES)}（当前 {pipeline}）")
        # 校验最终生效值：显式传入与配置默认都不得越过检索硬上限
        effective_top_k = top_k if top_k is not None else self._settings.retrieval_top_k
        if not 1 <= effective_top_k <= MAX_FINAL_TOP_K:
            raise InvalidInputError(f"top_k 须在 1..{MAX_FINAL_TOP_K}（当前 {effective_top_k}）")
        question = question.strip()
        if not question or len(question) > MAX_QUESTION_CHARS:
            raise InvalidInputError(
                f"问题须为 1..{MAX_QUESTION_CHARS} 字符（当前 {len(question)}）"
            )
        async with map_errors():
            embedder = await self._get_embedder()
            llm = self._get_llm()
            async with self._session_factory() as session:
                project = await self._resolve_project(session, project_slug)
                answer_fn = answer_question if pipeline == "fixed-rag" else agentic_answer_question
                return await answer_fn(
                    session,
                    project.id,
                    question,
                    embedder=embedder,
                    llm=llm,
                    top_k=effective_top_k,
                )

    async def list_runs(self, project_slug: str, limit: int = 20) -> list[dict[str, Any]]:
        async with map_errors(), self._session_factory() as session:
            project = await self._resolve_project(session, project_slug)
            runs = await RunRepo(session, project.id).list_recent(limit)
            return [
                {
                    "run_id": str(run.id),
                    "status": run.status,
                    "question": run.question,
                    "tokens_in": run.tokens_in,
                    "tokens_out": run.tokens_out,
                    "latency_ms": run.latency_ms,
                    "created_at": run.created_at.isoformat(),
                }
                for run in runs
            ]

    async def run_trace(self, project_slug: str, run_id: str) -> dict[str, Any]:
        """只读轨迹装配（runs show/replay 与 GET /runs 共用）；跨项目统一 NotFound。"""
        try:
            run_uuid = uuid.UUID(run_id)
        except ValueError as exc:
            raise NotFoundError(f"非法 run_id：{run_id}") from exc
        async with map_errors(), self._session_factory() as session:
            project = await self._resolve_project(session, project_slug)
            try:
                return await get_run_trace(session, project.id, run_uuid)
            except NotFoundError as exc:
                raise NotFoundError(f"run {run_id} 不存在于项目 '{project_slug}'") from exc

    async def ingest(self, project_slug: str, directory: Path) -> IngestReport:
        """摄取目录（项目不存在则创建）；仍是显式管理操作，不由 Agent 调用。"""
        from devkb.ingest.pipeline import ingest_directory

        async with map_errors():
            count_tokens = self._get_count_tokens()
            embedder = await self._get_embedder()
            async with self._session_factory() as session:
                repo = ProjectRepo(session)
                project = await repo.get_by_slug(project_slug)
                if project is None:
                    project = await repo.create(slug=project_slug, name=project_slug)
                    await session.commit()
                return await ingest_directory(
                    session,
                    project.id,
                    directory,
                    embedder=embedder,
                    count_tokens=count_tokens,
                    target_tokens=self._settings.chunk_target_tokens,
                )

    async def healthz(self) -> dict[str, Any]:
        """健康探测：进程存活 + 数据库连通 + 迁移版本；不加载模型、不调用 LLM。"""
        async with map_errors(), self._session_factory() as session:
            migration = await get_alembic_version(session)
        return {"status": "ok", "database": "ok", "migration": migration}

    async def backfill_search(self, project_slug: str) -> tuple[int, int]:
        async with map_errors(), self._session_factory() as session:
            project = await self._resolve_project(session, project_slug)
            result = await ChunkRepo(session, project.id).backfill_search_text()
            await session.commit()
            return result
