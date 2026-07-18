"""T16 Agent 节点：结构化调用、预算计数与冻结默认路径。"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent import prompts
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


def _cost_after_result(
    current: Decimal | None,
    model: str,
    usage: dict[str, Any],
) -> Decimal | None:
    call_cost = compute_cost(model, usage)
    if current is None or call_cost is None:
        return None
    return current + call_cost


async def _structured_call[StructuredT: StrictModel](
    state: AgentState,
    *,
    llm: LLMClient,
    call_key: str,
    system: str,
    user: str,
    schema: type[StructuredT],
    default: StructuredT,
) -> _CallResult[StructuredT]:
    """单逻辑调用最多重问一次；每次请求先占用全局预算再调用供应商。"""
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
            latency_ms += int((time.perf_counter() - started) * 1000)
            warnings.append(f"{call_key}:request_failed:{type(exc).__name__}")
            continue
        latency_ms += int((time.perf_counter() - started) * 1000)
        tokens_in += int(result.usage.get("prompt_tokens", 0))
        tokens_out += int(result.usage.get("completion_tokens", 0))
        cost = _cost_after_result(cost, result.model, result.usage)
        model = result.model
        try:
            value = schema.model_validate_json(result.text)
        except ValidationError:
            warnings.append(f"{call_key}:invalid_structured_output")
            continue
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
            hits = await retrieve(
                session,
                project_id,
                query,
                embedder=embedder,
                top_k=top_k,
                mode="hnsw",
                ef_search=HNSW_EF_SEARCH,
            )
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
    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def plan(self, state: AgentState) -> dict[str, Any]:
        default = PlanOutput(intent="knowledge_qa", queries=[state["question"]])
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key="plan",
            system=prompts.PLAN_SYSTEM,
            user=prompts.build_plan_user(state["question"]),
            schema=PlanOutput,
            default=default,
        )
        return {
            **call.updates,
            "plan": call.value,
            "queries": call.value.queries,
            "node_history": ["plan"],
            "warnings": call.warnings,
        }

    async def retrieve(self, state: AgentState) -> dict[str, Any]:
        if state["retrieval_round"] >= MAX_RETRIEVAL_ROUNDS:
            return {
                "node_history": ["retrieve"],
                "warnings": ["retrieve:round_limit_reached"],
            }
        try:
            fresh = await self._runtime.retriever(state["project_id"], tuple(state["queries"]))
        except Exception as exc:
            return {
                "retrieval_round": state["retrieval_round"] + 1,
                "evidences": state["evidences"],
                "node_history": ["retrieve"],
                "errors": [f"retrieve:{type(exc).__name__}"],
            }

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
            user=prompts.build_evaluate_user(state["question"], state["evidences"]),
            schema=EvaluateOutput,
            default=default,
        )
        return {
            **call.updates,
            "evaluation": call.value,
            "node_history": ["evaluate"],
            "warnings": call.warnings,
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
            user=prompts.build_refine_user(state["question"], state["queries"], evaluation),
            schema=RefineOutput,
            default=default,
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
            ),
            schema=GenerateOutput,
            default=default,
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
            if (
                evaluation is not None
                and evaluation.sufficiency == "sufficient"
                and kept
                and not draft.not_found
                and verification_ok
            ):
                mode = "full"
            else:
                mode = "partial"
                if evaluation is not None and evaluation.sufficiency == "sufficient" and not kept:
                    warnings.append("finalize: 充分判定但无结构化 claim，降级 partial")
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
