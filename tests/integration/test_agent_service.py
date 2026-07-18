"""T17.4 agentic service：Answer v1 落库、warning/调用数/cost 正确、失败终态。"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent.service import agentic_answer_question
from devkb.embedding import FakeEmbedder
from devkb.errors import InvalidInputError
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.llm import FakeLLM, compute_cost
from devkb.repositories import ProjectRepo, RunRepo

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)
GEN_BAD_QUOTE = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":["这段引文在任何证据中都不存在"]}],"not_found":[]}'
)


async def _seeded_project(session: AsyncSession) -> uuid.UUID:
    project = await ProjectRepo(session).create(slug=f"t17-{uuid.uuid4().hex[:8]}", name="t17")
    await session.commit()
    report = await ingest_directory(
        session, project.id, CORPUS_MD, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    return project.id


async def test_agentic_answer_v1_persists_warnings_calls_and_cost(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert answer["mode"] == "full"
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1"]
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.status == "succeeded"
    assert run.answer is not None and run.answer["mode"] == "full"
    assert run.answer["warnings"] == answer["warnings"]
    assert run.model == "deepseek-v4-flash"
    # 调用数与 tokens 由明细可复算（FakeLLM 每次 100+50）
    assert run.usage is not None and run.usage["llm_calls"] == 3
    assert run.tokens_in == 300 and run.tokens_out == 150
    # cost = 3 × 单次调用成本（价目表内模型，逐档复算）
    per_call = compute_cost(
        "deepseek-v4-flash",
        {"prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 80, "completion_tokens": 50},
    )
    assert per_call is not None and run.cost == 3 * per_call == Decimal("0.000540")
    assert run.latency_ms is not None and run.latency_ms >= 0


async def test_regeneration_run_persists_verify_warning_and_extra_calls(
    session: AsyncSession,
) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM([PLAN, EVAL_OK, GEN_BAD_QUOTE, GEN], model="deepseek-v4-flash")

    answer = await agentic_answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert answer["mode"] == "full"
    assert "verify:l0_l1_failed" in answer["warnings"]
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.usage is not None
    assert run.usage["llm_calls"] == 4  # 含 1 次 L1 触发的重生成
    assert run.answer is not None and "verify:l0_l1_failed" in run.answer["warnings"]


async def test_invalid_question_rejected_before_run_creation(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    with pytest.raises(InvalidInputError):
        await agentic_answer_question(
            session, project_id, "x" * 4001, embedder=FakeEmbedder(), llm=FakeLLM([]), top_k=4
        )
    assert await RunRepo(session, project_id).list_recent(5) == []


async def test_unexpected_graph_failure_marks_run_failed(
    session: AsyncSession, monkeypatch: Any
) -> None:
    project_id = await _seeded_project(session)

    async def broken_run_agent(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("graph exploded")

    monkeypatch.setattr("devkb.agent.service.run_agent", broken_run_agent)
    with pytest.raises(RuntimeError):
        await agentic_answer_question(
            session, project_id, "问题", embedder=FakeEmbedder(), llm=FakeLLM([]), top_k=4
        )

    runs = await RunRepo(session, project_id).list_recent(5)
    assert runs and runs[0].status == "failed"
    assert runs[0].answer is not None and "error" in runs[0].answer
