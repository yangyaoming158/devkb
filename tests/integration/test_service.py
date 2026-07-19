"""T19.1 AppService 集成：CLI/API 共用入口的问答/轨迹/隔离与数据库异常映射。

用 FakeEmbedder/FakeLLM/approx_token_counter 注入（CI 禁真模型）；
覆盖 agentic 与 fixed-rag 双链路、项目隔离统一 NotFound、DB 不可达的稳定错误码。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

from devkb.config import Settings
from devkb.embedding import FakeEmbedder
from devkb.errors import DatabaseError, NotFoundError
from devkb.ingest.markdown import approx_token_counter
from devkb.llm import FakeLLM
from devkb.service import AppService

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)


def _service(db_url: str, script: list[str | Exception]) -> AppService:
    settings = Settings(llm_api_key=SecretStr("integration-test"), database_url=db_url)
    return AppService(
        settings,
        embedder=FakeEmbedder(),
        llm=FakeLLM(script, model="deepseek-v4-flash"),
        count_tokens=approx_token_counter,
    )


def _slug() -> str:
    return f"t19-{uuid.uuid4().hex[:8]}"


async def test_ask_agentic_end_to_end_with_runs_and_trace(migrated_db_url: str) -> None:
    slug = _slug()
    service = _service(migrated_db_url, [PLAN, EVAL_OK, GEN])
    try:
        report = await service.ingest(slug, CORPUS_MD)
        assert report.count("failed") == 0

        answer = await service.ask(slug, "库存怎么保证并发安全？")
        assert answer["mode"] == "full"

        runs = await service.list_runs(slug)
        assert runs[0]["run_id"] == answer["run_id"] and runs[0]["status"] == "succeeded"

        trace = await service.run_trace(slug, answer["run_id"])
        nodes = [step["node"] for step in trace["steps"]]
        assert "plan" in nodes and "generate" in nodes and "finalize" in nodes
        run = trace["run"]
        assert run["answer"] is not None
        assert run["answer"]["answer_text"] == answer["answer_text"]
    finally:
        await service.aclose()


async def test_ask_fixed_rag_pipeline_returns_p0_answer_shape(migrated_db_url: str) -> None:
    slug = _slug()
    service = _service(migrated_db_url, ["库存扣减使用行锁保证并发安全 [E1]。"])
    try:
        await service.ingest(slug, CORPUS_MD)
        answer = await service.ask(slug, "库存怎么保证并发安全？", pipeline="fixed-rag")
        # P0 对照链路保持 P0 Answer 形状（无 mode/claims）
        assert set(answer) == {"answer_text", "citations", "warnings", "stats", "run_id"}
        assert [c["evidence_id"] for c in answer["citations"]] == ["E1"]
    finally:
        await service.aclose()


async def test_unknown_project_raises_not_found(migrated_db_url: str) -> None:
    service = _service(migrated_db_url, [])
    try:
        with pytest.raises(NotFoundError):
            await service.ask("no-such-project", "库存怎么保证并发安全？")
        with pytest.raises(NotFoundError):
            await service.list_runs("no-such-project")
        with pytest.raises(NotFoundError):
            await service.backfill_search("no-such-project")
    finally:
        await service.aclose()


async def test_run_trace_isolated_by_project_and_rejects_bad_run_id(
    migrated_db_url: str,
) -> None:
    slug_a, slug_b = _slug(), _slug()
    service = _service(migrated_db_url, [PLAN, EVAL_OK, GEN])
    try:
        await service.ingest(slug_a, CORPUS_MD)
        await service.ingest(slug_b, CORPUS_MD)
        answer = await service.ask(slug_a, "库存怎么保证并发安全？")

        assert (await service.run_trace(slug_a, answer["run_id"]))["run"]["status"] == "succeeded"
        with pytest.raises(NotFoundError):
            await service.run_trace(slug_b, answer["run_id"])
        with pytest.raises(NotFoundError):
            await service.run_trace(slug_a, "not-a-uuid")
    finally:
        await service.aclose()


async def test_database_unavailable_maps_to_stable_database_error() -> None:
    # 端口 9（discard）永不监听；口令不得泄漏进用户可见消息
    service = _service("postgresql+asyncpg://devkb:supersecretpw@127.0.0.1:9/devkb", [])
    try:
        with pytest.raises(DatabaseError) as exc_info:
            await service.list_runs("any-project")
        assert exc_info.value.code == "DATABASE_ERROR"
        assert "supersecretpw" not in str(exc_info.value)
    finally:
        await service.aclose()
