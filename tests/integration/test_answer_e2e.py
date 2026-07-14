"""T9.4 e2e（FakeEmbedder + FakeLLM）：ask 全链路 Answer 结构、虚假 E# 剔除、重问与失败落库。"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.answer import answer_question
from devkb.embedding import FakeEmbedder
from devkb.errors import LLMError, LLMTimeoutError
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.llm import FakeLLM
from devkb.repositories import ProjectRepo, RunRepo

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"


async def _seeded_project(session: AsyncSession) -> uuid.UUID:
    project = await ProjectRepo(session).create(slug=f"t9-{uuid.uuid4().hex[:8]}", name="t9")
    await session.commit()
    report = await ingest_directory(
        session, project.id, CORPUS_MD, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    return project.id


async def test_answer_structure_and_run_persisted(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM(["库存扣减使用行锁保证并发安全 [E1]，超时订单由补偿任务回补 [E2]。"])

    answer = await answer_question(
        session, project_id, "库存怎么保证并发安全？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    # Answer JSON 结构（规格 §6）
    assert set(answer) == {"answer_text", "citations", "warnings", "stats", "run_id"}
    assert answer["warnings"] == []
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1", "E2"]
    for c in answer["citations"]:
        assert c["rel_path"].endswith(".md")
        assert c["start_line"] >= 1 and c["end_line"] >= c["start_line"]
        assert c["title_path"] and c["chunk_id"]
    stats = answer["stats"]
    assert stats["model"] == "fake-llm"
    assert stats["tokens_in"] == 100 and stats["tokens_out"] == 50

    # prompt 契约：证据编号与问题都进入 user prompt，系统提示要求仅依据证据
    assert "[E1]" in llm.prompts[0]["user"] and "库存怎么保证并发安全？" in llm.prompts[0]["user"]
    assert "只依据" in llm.prompts[0]["system"]

    # 落库：answer JSONB、usage 原始分档字段、状态
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.status == "succeeded"
    assert run.answer is not None and run.answer["answer_text"] == answer["answer_text"]
    assert run.usage is not None and run.usage["prompt_cache_hit_tokens"] == 20
    assert run.tokens_in == 100 and run.tokens_out == 50
    assert run.latency_ms is not None and run.latency_ms >= 0


async def test_fake_evidence_id_removed_by_l0(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM(["行锁保证安全 [E1]。另见虚构文档 [E99]。"])

    answer = await answer_question(
        session, project_id, "并发控制？", embedder=FakeEmbedder(), llm=llm, top_k=4
    )

    assert "[E99]" not in answer["answer_text"]
    assert "[E1]" in answer["answer_text"]
    assert [c["evidence_id"] for c in answer["citations"]] == ["E1"]
    assert len(answer["warnings"]) == 1 and "E99" in answer["warnings"][0]
    # warnings 随 Answer 落库
    run = await RunRepo(session, project_id).get(uuid.UUID(answer["run_id"]))
    assert run is not None and run.answer is not None
    assert len(run.answer["warnings"]) == 1


async def test_llm_timeout_retried_once(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM([LLMTimeoutError("模拟超时"), "重试后成功 [E1]。"])

    answer = await answer_question(
        session, project_id, "问题", embedder=FakeEmbedder(), llm=llm, top_k=2
    )

    assert len(llm.prompts) == 2  # 首次超时 + 重问 1 次
    assert answer["citations"][0]["evidence_id"] == "E1"


async def test_llm_failure_marks_run_failed(session: AsyncSession) -> None:
    project_id = await _seeded_project(session)
    llm = FakeLLM([LLMError("故障一"), LLMError("故障二")])  # 重问 1 次后仍失败

    with pytest.raises(LLMError):
        await answer_question(
            session, project_id, "问题", embedder=FakeEmbedder(), llm=llm, top_k=2
        )

    runs = await RunRepo(session, project_id).list_recent(5)
    assert runs and runs[0].status == "failed"
    assert runs[0].answer is not None and "error" in runs[0].answer
