"""T16 Agent 节点：结构化调用、预算计数与冻结默认路径。"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent import prompts
from devkb.agent.evidence_types import (
    TYPE_LABELS,
    all_required_covered,
    compute_coverage,
    uncovered_items,
)
from devkb.agent.state import (
    MAX_GENERATE_CALLS,
    MAX_LLM_REQUESTS,
    MAX_REASKS_PER_CALL,
    MAX_RETRIEVAL_ROUNDS,
    AgentState,
    ClaimOutput,
    EvaluateOutput,
    Evidence,
    FinalMode,
    GenerateOutput,
    PlanOutput,
    RefineOutput,
    StrictModel,
    VerificationOutput,
)
from devkb.agent.trace import TraceRecorder, clip_list
from devkb.agent.verification import verify_draft
from devkb.answer import apply_l0
from devkb.embedding import Embedder
from devkb.llm import LLMClient, compute_cost
from devkb.retrieval import (
    HNSW_EF_SEARCH,
    MAX_FINAL_TOP_K,
    ChannelRanking,
    RetrievedChunk,
    retrieve,
    rrf_fuse,
)

Retriever = Callable[[uuid.UUID, tuple[str, ...]], Awaitable[list[Evidence]]]


def _refusal_text(missing: list[str]) -> str:
    """确定性拒答模板（说明缺什么），不消耗 LLM 预算。"""
    if missing:
        return f"现有资料不足以回答该问题。缺少：{'；'.join(missing)}。"
    return "现有资料不足以回答该问题。"


def _merge_unique(*groups: list[str]) -> list[str]:
    return list(dict.fromkeys(item for group in groups for item in group))


_EVIDENCE_MARK = re.compile(r"\[E\d+\]")


def _rebuild_answer_from_claims(kept: list[ClaimOutput]) -> str:
    """二次验证失败后由保留 claim 确定性重建正文，杜绝不可信句子/标记残留。

    claim.text 内嵌的 [E#] 未经 L0 检查，一律剔除；标记只由已通过 L0 的
    claim.evidence_ids 规范生成。
    """
    sentences = []
    for claim in kept:
        text = " ".join(_EVIDENCE_MARK.sub("", claim.text).split())
        marks = "".join(f"[{evidence_id}]" for evidence_id in claim.evidence_ids)
        sentences.append(f"{text.rstrip('。.')} {marks}。")
    return "".join(sentences)


@dataclass(frozen=True)
class AgentRuntime:
    llm: LLMClient
    retriever: Retriever
    max_evidences: int = MAX_FINAL_TOP_K

    def __post_init__(self) -> None:
        if not 1 <= self.max_evidences <= MAX_FINAL_TOP_K:
            raise ValueError(f"max_evidences 必须在 1..{MAX_FINAL_TOP_K}")


@dataclass(frozen=True)
class _CallResult[StructuredT: StrictModel]:
    value: StructuredT
    used_default: bool
    updates: dict[str, Any]
    warnings: list[str]


async def _structured_call[StructuredT: StrictModel](
    state: AgentState,
    *,
    llm: LLMClient,
    call_key: str,
    system: str,
    user: str,
    schema: type[StructuredT],
    default: StructuredT,
    recorder: TraceRecorder | None = None,
) -> _CallResult[StructuredT]:
    """单逻辑调用最多重问一次；每次请求先占用全局预算再调用供应商。

    每次实际发出的请求（含重问）逐条记入轨迹：分档 usage、单次 cost、时延与
    终态，run 汇总值可由这些明细复算（T18.2）。预算耗尽未发出的请求不记录。
    """
    calls = state["llm_calls"]
    retries = state["llm_retries"]
    retry_counts = dict(state["retry_counts"])
    tokens_in = state["tokens_in"]
    tokens_out = state["tokens_out"]
    cost = state["cost"]
    latency_ms = state["latency_ms"]
    model = state["model"]
    warnings: list[str] = []

    for attempt in range(MAX_REASKS_PER_CALL + 1):
        if calls >= MAX_LLM_REQUESTS:
            warnings.append(f"{call_key}:budget_exhausted")
            break
        calls += 1
        if attempt:
            retries += 1
            retry_counts[call_key] = retry_counts.get(call_key, 0) + 1

        started = time.perf_counter()
        try:
            result = await llm.complete(system=system, user=user)
        except Exception as exc:
            call_latency_ms = int((time.perf_counter() - started) * 1000)
            latency_ms += call_latency_ms
            warnings.append(f"{call_key}:request_failed:{type(exc).__name__}")
            if recorder is not None:
                recorder.record_llm_request(
                    call_key=call_key,
                    attempt=attempt + 1,
                    status="request_failed",
                    model=None,
                    usage={},
                    cost=None,
                    latency_ms=call_latency_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )
            continue
        call_latency_ms = int((time.perf_counter() - started) * 1000)
        latency_ms += call_latency_ms
        tokens_in += int(result.usage.get("prompt_tokens", 0))
        tokens_out += int(result.usage.get("completion_tokens", 0))
        call_cost = compute_cost(result.model, result.usage)
        cost = None if cost is None or call_cost is None else cost + call_cost
        model = result.model
        try:
            value = schema.model_validate_json(result.text)
        except ValidationError:
            warnings.append(f"{call_key}:invalid_structured_output")
            if recorder is not None:
                recorder.record_llm_request(
                    call_key=call_key,
                    attempt=attempt + 1,
                    status="invalid_output",
                    model=result.model,
                    usage=dict(result.usage),
                    cost=call_cost,
                    latency_ms=call_latency_ms,
                    error="invalid_structured_output",
                )
            continue
        if recorder is not None:
            recorder.record_llm_request(
                call_key=call_key,
                attempt=attempt + 1,
                status="ok",
                model=result.model,
                usage=dict(result.usage),
                cost=call_cost,
                latency_ms=call_latency_ms,
            )
        return _CallResult(
            value=value,
            used_default=False,
            updates={
                "llm_calls": calls,
                "llm_retries": retries,
                "retry_counts": retry_counts,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost": cost,
                "latency_ms": latency_ms,
                "model": model,
            },
            warnings=warnings,
        )

    return _CallResult(
        value=default,
        used_default=True,
        updates={
            "llm_calls": calls,
            "llm_retries": retries,
            "retry_counts": retry_counts,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": cost,
            "latency_ms": latency_ms,
            "model": model,
        },
        warnings=[*warnings, f"{call_key}:default_applied"],
    )


def make_pg_retriever(
    session: AsyncSession,
    embedder: Embedder,
    *,
    top_k: int = 8,
) -> Retriever:
    """创建在线默认 vector-HNSW 多查询检索器；不启用 Hybrid lexical channel。"""
    if not 1 <= top_k <= MAX_FINAL_TOP_K:
        raise ValueError(f"top_k 必须在 1..{MAX_FINAL_TOP_K}")

    async def _retrieve(project_id: uuid.UUID, queries: tuple[str, ...]) -> list[Evidence]:
        rankings: list[ChannelRanking] = []
        catalog: dict[uuid.UUID, RetrievedChunk] = {}
        for query in dict.fromkeys(queries):
            try:
                hits = await retrieve(
                    session,
                    project_id,
                    query,
                    embedder=embedder,
                    top_k=top_k,
                    mode="hnsw",
                    ef_search=HNSW_EF_SEARCH,
                )
            except SQLAlchemyError:
                # DB 异常会使事务 aborted：先回滚恢复 session 再上抛，
                # 否则 retrieve 节点降级后轨迹/run 终态无法落库（T18.4 一致性）
                await session.rollback()
                raise
            rankings.append(ChannelRanking(query, "vector", tuple(hit.chunk_id for hit in hits)))
            for hit in hits:
                catalog.setdefault(hit.chunk_id, hit)
        fused = rrf_fuse(rankings)[:top_k]
        return [
            Evidence(
                evidence_id=f"E{index}",
                chunk_id=item.chunk_id,
                rel_path=catalog[item.chunk_id].rel_path,
                title_path=catalog[item.chunk_id].title_path,
                content=catalog[item.chunk_id].content,
                start_line=catalog[item.chunk_id].start_line,
                end_line=catalog[item.chunk_id].end_line,
                score=item.fused_score,
            )
            for index, item in enumerate(fused, start=1)
        ]

    return _retrieve


class AgentNodes:
    def __init__(self, runtime: AgentRuntime, recorder: TraceRecorder | None = None) -> None:
        self._runtime = runtime
        self._recorder = recorder

    async def plan(self, state: AgentState) -> dict[str, Any]:
        default = PlanOutput(intent="knowledge_qa", queries=[state["question"]])
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key="plan",
            system=prompts.PLAN_SYSTEM,
            user=prompts.build_plan_user(state["question"], state["required_evidence"]),
            schema=PlanOutput,
            default=default,
            recorder=self._recorder,
        )
        # plan 回显确认 required_evidence 的 item_id：须与权威集合完整一致；遗漏或
        # 出现非权威 id 都记诊断警告并忽略。权威来源是 state["required_evidence"]
        # （确定性解析），LLM 不得新增/伪造/漏报，也不影响确定性裁决。
        warnings = list(call.warnings)
        authoritative_ids = state["required_evidence"].item_ids
        reported_ids = set(call.value.required_evidence)
        if (authoritative_ids and reported_ids != authoritative_ids) or (
            reported_ids - authoritative_ids
        ):
            warnings.append("plan:required_evidence_mismatch")
        return {
            **call.updates,
            "plan": call.value,
            "queries": call.value.queries,
            "node_history": ["plan"],
            "warnings": warnings,
        }

    async def retrieve(self, state: AgentState) -> dict[str, Any]:
        if state["retrieval_round"] >= MAX_RETRIEVAL_ROUNDS:
            return {
                "node_history": ["retrieve"],
                "warnings": ["retrieve:round_limit_reached"],
            }
        tool_arguments = {"queries": clip_list(state["queries"])}
        started = time.perf_counter()
        try:
            fresh = await self._runtime.retriever(state["project_id"], tuple(state["queries"]))
        except Exception as exc:
            if self._recorder is not None:
                self._recorder.record_tool(
                    tool_name="retrieve",
                    status="failed",
                    arguments=tool_arguments,
                    result_summary=None,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return {
                "retrieval_round": state["retrieval_round"] + 1,
                "evidences": state["evidences"],
                "node_history": ["retrieve"],
                "errors": [f"retrieve:{type(exc).__name__}"],
            }
        if self._recorder is not None:
            self._recorder.record_tool(
                tool_name="retrieve",
                status="ok",
                arguments=tool_arguments,
                result_summary={"result_count": len(fresh)},
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        # 补检结果优先，旧证据只用于填充剩余槽位；否则首轮 top-k 已满时
        # 第二轮新证据会被截断为零，形成“走了 refine 但证据没变”的假补检。
        merged: dict[uuid.UUID, Evidence] = {}
        for evidence in [*fresh, *state["evidences"]]:
            merged.setdefault(evidence.chunk_id, evidence)
        evidences = [
            evidence.model_copy(update={"evidence_id": f"E{index}"})
            for index, evidence in enumerate(
                list(merged.values())[: self._runtime.max_evidences], start=1
            )
        ]
        return {
            "retrieval_round": state["retrieval_round"] + 1,
            "evidences": evidences,
            "node_history": ["retrieve"],
        }

    async def evaluate(self, state: AgentState) -> dict[str, Any]:
        default = EvaluateOutput(
            sufficiency="insufficient",
            supported_aspects=[],
            missing_aspects=["证据充分性无法确认"],
        )
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key=f"evaluate:{state['retrieval_round']}",
            system=prompts.EVALUATE_SYSTEM,
            user=prompts.build_evaluate_user(
                state["question"], state["evidences"], state["required_evidence"]
            ),
            schema=EvaluateOutput,
            default=default,
            recorder=self._recorder,
        )
        # 确定性覆盖矩阵（按当前证据可用性）：权威，驱动 route/refine；LLM 覆盖报告
        # 只作诊断。报告须逐项完整、covered 与命中 evidence 都要与确定性矩阵对齐；
        # 遗漏/翻转/乱报 evidence 都记 mismatch 警告，但绝不改判确定性覆盖。
        cited = [(ev.evidence_id, ev.rel_path) for ev in state["evidences"]]
        coverage = compute_coverage(state["required_evidence"], cited)
        deterministic = {entry.item_id: entry for entry in coverage}
        authoritative_ids = state["required_evidence"].item_ids
        reported_ids = {report.item_id for report in call.value.coverage}
        warnings = list(call.warnings)
        mismatch = bool(authoritative_ids) and reported_ids != authoritative_ids
        for report in call.value.coverage:
            entry = deterministic.get(report.item_id)
            if entry is None or report.covered != entry.covered:
                mismatch = True
            elif set(report.evidence_ids) != set(entry.matched_evidence_ids):
                mismatch = True  # 自报命中的 evidence 与确定性矩阵不符（含遗漏/多报）
        if mismatch:
            warnings.append("evaluate:coverage_mismatch")
        return {
            **call.updates,
            "evaluation": call.value,
            "coverage": coverage,
            "node_history": ["evaluate"],
            "warnings": warnings,
        }

    async def refine(self, state: AgentState) -> dict[str, Any]:
        evaluation = state["evaluation"]
        if evaluation is None:
            raise RuntimeError("refine 节点缺少 evaluation")
        default = RefineOutput(queries=state["queries"])
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key="refine",
            system=prompts.REFINE_SYSTEM,
            user=prompts.build_refine_user(
                state["question"],
                state["queries"],
                evaluation,
                uncovered_hints=[
                    item.symbol or item.path or item.anchor
                    for item in uncovered_items(state["required_evidence"], state["coverage"])
                ],
            ),
            schema=RefineOutput,
            default=default,
            recorder=self._recorder,
        )
        return {
            **call.updates,
            "queries": call.value.queries,
            "refine_calls": state["refine_calls"] + 1,
            "refine_failed": call.used_default,
            "node_history": ["refine"],
            "warnings": call.warnings,
        }

    async def generate(self, state: AgentState) -> dict[str, Any]:
        evaluation = state["evaluation"]
        if evaluation is None:
            raise RuntimeError("generate 节点缺少 evaluation")
        if state["generate_calls"] >= MAX_GENERATE_CALLS:
            return {
                "generate_failed": True,
                "node_history": ["generate"],
                "warnings": ["generate:call_limit_reached"],
            }
        default = GenerateOutput(answer_text="现有证据不足以可靠生成回答。")
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key=f"generate:{state['generate_calls'] + 1}",
            system=prompts.GENERATE_SYSTEM,
            user=prompts.build_generate_user(
                state["question"],
                state["evidences"],
                evaluation,
                verification_errors=state["verification_feedback"] or None,
                required_hints=[
                    f"{TYPE_LABELS[item.type]}:{item.symbol or item.path or item.anchor}"
                    for item in state["required_evidence"].items
                ]
                or None,
            ),
            schema=GenerateOutput,
            default=default,
            recorder=self._recorder,
        )
        return {
            **call.updates,
            "answer_draft": call.value,
            "generate_calls": state["generate_calls"] + 1,
            "generate_failed": call.used_default,
            "node_history": ["generate"],
            "warnings": call.warnings,
        }

    async def verify(self, state: AgentState) -> dict[str, Any]:
        """确定性 L0/L1 验证（T17.4）；不通过时 errors 作为重生成反馈。"""
        draft = state["answer_draft"]
        if draft is None or state["generate_failed"]:
            verification = VerificationOutput(
                passed=False,
                l0_passed=False,
                l1_passed=False,
                errors=["verify:no_valid_draft"],
            )
            return {
                "verification": verification,
                "verification_feedback": [],
                "node_history": ["verify"],
            }
        verification = verify_draft(draft, state["evidences"])
        return {
            "verification": verification,
            "verification_feedback": [] if verification.passed else list(verification.errors),
            "node_history": ["verify"],
            "warnings": [] if verification.passed else ["verify:l0_l1_failed"],
        }

    async def finalize(self, state: AgentState) -> dict[str, Any]:
        """三态确定性收尾（规格 §10）：只读结构化状态，不发起任何 LLM 调用。"""
        draft = state["answer_draft"]
        evaluation = state["evaluation"]
        verification = state["verification"]
        missing = list(evaluation.missing_aspects) if evaluation else []
        warnings: list[str] = []

        if draft is None:
            return {
                "final_answer": _refusal_text(missing),
                "final_mode": "refusal",
                "final_claims": [],
                "final_not_found": missing,
                "status": "succeeded",
                "node_history": ["finalize"],
            }

        if state["generate_failed"]:
            mode: FinalMode = "partial" if state["evidences"] else "refusal"
            answer = draft.answer_text if mode == "partial" else _refusal_text(missing)
            return {
                "final_answer": answer,
                "final_mode": mode,
                "final_claims": [],
                "final_not_found": _merge_unique(draft.not_found, missing),
                "status": "succeeded",
                "node_history": ["finalize"],
            }

        failed = set(verification.failed_claims) if verification else set()
        kept = [claim for index, claim in enumerate(draft.claims) if index not in failed]
        removed = len(draft.claims) - len(kept)

        not_found = _merge_unique(draft.not_found, missing)
        verification_ok = verification is None or verification.passed
        if removed:
            # 不可信 claim 的正文与其 [E#] 标记不得残留：正文按保留 claim 确定性重建
            warnings.append(
                f"finalize: 已移除 {removed} 个未通过验证的 claim，正文按保留 claim 重建"
            )
            # 重建后再过一次确定性 L0，作为越界标记的最终防线
            answer, _, rebuild_l0_warnings = apply_l0(
                _rebuild_answer_from_claims(kept), len(state["evidences"])
            )
            warnings.extend(rebuild_l0_warnings)
            mode: FinalMode = "partial" if kept else "refusal"
        else:
            answer, _, l0_warnings = apply_l0(draft.answer_text, len(state["evidences"]))
            warnings.extend(l0_warnings)
            # T22.2 full 硬约束：逐项确定性覆盖——每条必需证据（含点名类/路径）都须有
            # 匹配的直接引用；测试/设计/历史材料因类型不同无法替代必需生产证据。
            evidence_by_id = {ev.evidence_id: ev for ev in state["evidences"]}
            cited = [
                (evidence_id, evidence_by_id[evidence_id].rel_path)
                for claim in kept
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence_by_id
            ]
            final_coverage = compute_coverage(state["required_evidence"], cited)
            uncovered = uncovered_items(state["required_evidence"], final_coverage)
            if (
                evaluation is not None
                and evaluation.sufficiency == "sufficient"
                and kept
                and not draft.not_found
                and verification_ok
                and all_required_covered(final_coverage)
            ):
                mode = "full"
            else:
                mode = "partial"
                if evaluation is not None and evaluation.sufficiency == "sufficient" and not kept:
                    warnings.append("finalize: 充分判定但无结构化 claim，降级 partial")
                if uncovered:
                    labels = "、".join(
                        f"{TYPE_LABELS[item.type]}:{item.symbol or item.path or item.anchor}"
                        for item in uncovered
                    )
                    note = f"未取得用户要求的必需证据（{labels}）；测试/设计/历史材料不能替代"
                    if note not in not_found:
                        not_found = [*not_found, note]
                    warnings.append(
                        "finalize: 必需证据未覆盖，降级 partial（"
                        + ",".join(item.item_id for item in uncovered)
                        + "）"
                    )
        if mode == "refusal":
            answer = _refusal_text(not_found)
            kept = []
        return {
            "final_answer": answer,
            "final_mode": mode,
            "final_claims": kept,
            "final_not_found": not_found if mode != "full" else [],
            "status": "succeeded",
            "node_history": ["finalize"],
            "warnings": warnings,
        }
