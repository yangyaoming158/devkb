"""Agentic 问答服务（T17.4）：跑有界图 + Answer v1 组装 + agent_runs 落库。

CLI `ask --pipeline agentic` 使用；T19 API 复用同一入口。
节点/工具轨迹（agent_steps/tool_invocations）落库属 T18，不在此实现。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent.answer import build_answer
from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime, make_pg_retriever
from devkb.agent.state import MAX_QUESTION_CHARS, AgentInput
from devkb.embedding import Embedder
from devkb.errors import InvalidInputError
from devkb.llm import LLMClient
from devkb.repositories import RunRepo

logger = structlog.get_logger(__name__)


async def agentic_answer_question(
    session: AsyncSession,
    project_id: uuid.UUID,
    question: str,
    *,
    embedder: Embedder,
    llm: LLMClient,
    top_k: int = 8,
) -> dict[str, Any]:
    question = question.strip()
    if not question or len(question) > MAX_QUESTION_CHARS:
        raise InvalidInputError(f"问题须为 1..{MAX_QUESTION_CHARS} 字符（当前 {len(question)}）")
    runtime = AgentRuntime(
        llm=llm,
        retriever=make_pg_retriever(session, embedder, top_k=top_k),
    )
    run_repo = RunRepo(session, project_id)
    run = await run_repo.create(question)
    await session.commit()
    agent_input = AgentInput(run_id=run.id, project_id=project_id, question=question)

    started = time.perf_counter()
    try:
        state = await run_agent(runtime, agent_input)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await run_repo.finish(
            run.id,
            answer={"error": f"{type(exc).__name__}: {exc}"},
            model="",
            tokens_in=0,
            tokens_out=0,
            usage=None,
            cost=None,
            latency_ms=latency_ms,
            status="failed",
        )
        await session.commit()
        raise

    # stats.latency_ms 与 P0 口径一致：run 全程墙钟（含检索/验证），非仅 LLM 时延
    state["latency_ms"] = int((time.perf_counter() - started) * 1000)
    answer = build_answer(state)
    usage = {
        "prompt_tokens": state["tokens_in"],
        "completion_tokens": state["tokens_out"],
        "total_tokens": state["tokens_in"] + state["tokens_out"],
        "llm_calls": state["llm_calls"],
        "llm_retries": state["llm_retries"],
    }
    await run_repo.finish(
        run.id,
        answer=answer,
        model=state["model"],
        tokens_in=state["tokens_in"],
        tokens_out=state["tokens_out"],
        usage=usage,
        cost=state["cost"],
        latency_ms=state["latency_ms"],
        status="succeeded",
    )
    await session.commit()
    logger.info(
        "agentic_run_finished",
        run_id=str(run.id),
        mode=answer["mode"],
        llm_calls=state["llm_calls"],
        warnings=len(answer["warnings"]),
        latency_ms=state["latency_ms"],
    )
    return answer
