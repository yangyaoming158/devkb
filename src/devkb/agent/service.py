"""Agentic 问答服务（T17.4/T18）：跑有界图 + Answer v1 组装 + run 与轨迹落库。

CLI `ask --pipeline agentic` 使用；T19 API 复用同一入口。
轨迹在图执行期由 TraceRecorder 内存收集，run 结束（含失败）统一写入
agent_steps / tool_invocations，与 run 终态同一事务提交。
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
from devkb.agent.trace import StepRecord, TraceRecorder
from devkb.embedding import Embedder
from devkb.errors import InvalidInputError, NotFoundError
from devkb.llm import LLMClient
from devkb.repositories import AgentStepRepo, RunRepo, ToolInvocationRepo

logger = structlog.get_logger(__name__)


def _step_output_summary(record: StepRecord) -> dict[str, Any] | None:
    """step 落库摘要：节点输出摘要 + 逐次 LLM 请求明细（cost 序列化为字符串）。"""
    summary = dict(record.output_summary) if record.output_summary is not None else {}
    if record.llm_requests:
        summary["llm_requests"] = [
            {
                "call_key": request.call_key,
                "attempt": request.attempt,
                "status": request.status,
                "model": request.model,
                "usage": request.usage,
                "cost": str(request.cost) if request.cost is not None else None,
                "latency_ms": request.latency_ms,
                "error": request.error,
            }
            for request in record.llm_requests
        ]
    return summary or None


async def _persist_trace(
    session: AsyncSession,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    recorder: TraceRecorder,
) -> None:
    step_repo = AgentStepRepo(session, project_id)
    tool_repo = ToolInvocationRepo(session, project_id)
    for record in recorder.steps:
        step = await step_repo.add(
            run_id,
            seq=record.seq,
            node=record.node,
            status=record.status,
            attempt=record.attempt,
            input_summary=record.input_summary or None,
            output_summary=_step_output_summary(record),
            latency_ms=record.latency_ms,
            error=record.error,
        )
        for tool in record.tools:
            await tool_repo.add(
                run_id,
                step.id,
                tool_name=tool.tool_name,
                status=tool.status,
                arguments=tool.arguments or None,
                result_summary=tool.result_summary,
                latency_ms=tool.latency_ms,
                error=tool.error,
            )


async def get_run_trace(
    session: AsyncSession,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    """T18.3 只读轨迹装配：run + steps + tools + Answer，一次都不执行节点。

    CLI `runs show/replay` 与 T19 `GET /runs/{run_id}` 共用。Repository 以
    project_id 实例化（D7）：其他项目的 run_id 在此天然不可见，统一 NotFound。
    """
    run = await RunRepo(session, project_id).get(run_id)
    if run is None:
        raise NotFoundError(f"run {run_id} 不存在于该项目")
    steps = await AgentStepRepo(session, project_id).list_for_run(run_id)
    tools = await ToolInvocationRepo(session, project_id).list_for_run(run_id)
    tools_by_step: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for tool in tools:
        tools_by_step.setdefault(tool.step_id, []).append(
            {
                "tool_name": tool.tool_name,
                "status": tool.status,
                "arguments": tool.arguments,
                "result_summary": tool.result_summary,
                "latency_ms": tool.latency_ms,
                "error": tool.error,
            }
        )
    return {
        "run": {
            "run_id": str(run.id),
            "question": run.question,
            "status": run.status,
            "model": run.model,
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
            "usage": run.usage,
            "cost": str(run.cost) if run.cost is not None else None,
            "latency_ms": run.latency_ms,
            "created_at": run.created_at.isoformat(),
            "answer": run.answer,
        },
        "steps": [
            {
                "seq": step.seq,
                "node": step.node,
                "attempt": step.attempt,
                "status": step.status,
                "latency_ms": step.latency_ms,
                "error": step.error,
                "input_summary": step.input_summary,
                "output_summary": step.output_summary,
                "tools": tools_by_step.get(step.id, []),
            }
            for step in steps
        ],
    }


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
    recorder = TraceRecorder()

    started = time.perf_counter()
    try:
        state = await run_agent(runtime, agent_input, recorder)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        # 失败路径尽力保留轨迹（最后一个 step 为 failed）；轨迹写入自身失败时
        # 回滚以保住 session，run 终态 failed 优先于轨迹完整性
        try:
            await _persist_trace(session, project_id, run.id, recorder)
        except Exception:
            await session.rollback()
            logger.warning("trace_persist_failed", run_id=str(run.id))
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
    await _persist_trace(session, project_id, run.id, recorder)
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
