"""T18 轨迹记录（规格 §5.2/§5.3/§11）。

图执行期间在内存收集节点 step、LLM 请求与工具调用明细，run 结束后由
service 统一写入 agent_steps / tool_invocations。摘要一律有界：只存结构化
信号、计数与截断后的短文本——不记录 chain-of-thought、secret、完整 Prompt
或文档/回答正文（正文只以字符数出现）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

MAX_SUMMARY_ITEMS = 8
MAX_SUMMARY_CHARS = 200

StepStatus = Literal["ok", "degraded", "failed"]

# 节点自身降级（冻结默认值/预算耗尽/上限触顶）的 warning 标记；
# verify:l0_l1_failed 是业务信号（触发重生成）而非节点降级，不在此列。
_DEGRADED_MARKERS = (":default_applied", ":budget_exhausted", "limit_reached")


def clip_text(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clip_list(items: list[str], limit: int = MAX_SUMMARY_ITEMS) -> list[str]:
    return [clip_text(item) for item in items[:limit]]


@dataclass(frozen=True)
class LLMRequestRecord:
    """单次发往供应商的请求（含重问），usage/cost 为该次调用的分档明细。"""

    call_key: str
    attempt: int
    status: Literal["ok", "request_failed", "invalid_output"]
    model: str | None
    usage: dict[str, Any]
    cost: Decimal | None
    latency_ms: int
    error: str | None


@dataclass(frozen=True)
class ToolRecord:
    tool_name: str
    status: Literal["ok", "failed"]
    arguments: dict[str, Any]
    result_summary: dict[str, Any] | None
    latency_ms: int
    error: str | None


@dataclass(frozen=True)
class StepRecord:
    seq: int
    node: str
    attempt: int
    status: StepStatus
    input_summary: dict[str, Any]
    output_summary: dict[str, Any] | None
    latency_ms: int
    error: str | None
    llm_requests: tuple[LLMRequestRecord, ...]
    tools: tuple[ToolRecord, ...]


@dataclass
class TraceRecorder:
    """单 run 内存轨迹。图为顺序执行，节点内产生的 LLM/工具记录挂到当前 step。"""

    steps: list[StepRecord] = field(default_factory=list)
    _node_runs: dict[str, int] = field(default_factory=dict)
    _pending_llm: list[LLMRequestRecord] = field(default_factory=list)
    _pending_tools: list[ToolRecord] = field(default_factory=list)

    def record_llm_request(
        self,
        *,
        call_key: str,
        attempt: int,
        status: Literal["ok", "request_failed", "invalid_output"],
        model: str | None,
        usage: dict[str, Any],
        cost: Decimal | None,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        self._pending_llm.append(
            LLMRequestRecord(
                call_key=call_key,
                attempt=attempt,
                status=status,
                model=model,
                usage=usage,
                cost=cost,
                latency_ms=latency_ms,
                error=clip_text(error) if error else None,
            )
        )

    def record_tool(
        self,
        *,
        tool_name: str,
        status: Literal["ok", "failed"],
        arguments: dict[str, Any],
        result_summary: dict[str, Any] | None,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        self._pending_tools.append(
            ToolRecord(
                tool_name=tool_name,
                status=status,
                arguments=arguments,
                result_summary=result_summary,
                latency_ms=latency_ms,
                error=clip_text(error) if error else None,
            )
        )

    def finish_step(
        self,
        *,
        node: str,
        status: StepStatus,
        input_summary: dict[str, Any],
        output_summary: dict[str, Any] | None,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        attempt = self._node_runs.get(node, 0) + 1
        self._node_runs[node] = attempt
        self.steps.append(
            StepRecord(
                seq=len(self.steps) + 1,
                node=node,
                attempt=attempt,
                status=status,
                input_summary=input_summary,
                output_summary=output_summary,
                latency_ms=latency_ms,
                error=clip_text(error) if error else None,
                llm_requests=tuple(self._pending_llm),
                tools=tuple(self._pending_tools),
            )
        )
        self._pending_llm.clear()
        self._pending_tools.clear()


def derive_step_status(updates: dict[str, Any]) -> tuple[StepStatus, str | None]:
    """由节点返回的结构化信号确定性判定 step 终态。

    errors（节点吸收的工具/DB 异常）→ degraded 并记录错误类型；
    降级 warning（冻结默认/预算耗尽/上限触顶）→ degraded；其余 ok。
    未捕获异常的 failed 由 graph 包装器直接判定，不经过本函数。
    """
    errors = updates.get("errors") or []
    if errors:
        return "degraded", str(errors[0])
    warnings = updates.get("warnings") or []
    for warning in warnings:
        if any(marker in warning for marker in _DEGRADED_MARKERS):
            return "degraded", None
    return "ok", None


def summarize_input(node: str, state: dict[str, Any]) -> dict[str, Any]:
    """节点入参有界摘要；异常路径也可从 state 构造（不依赖 updates）。"""
    if node == "plan":
        return {"question_chars": len(state["question"])}
    if node == "retrieve":
        return {"queries": clip_list(state["queries"]), "round": state["retrieval_round"]}
    if node == "evaluate":
        return {"evidence_count": len(state["evidences"]), "round": state["retrieval_round"]}
    if node == "refine":
        evaluation = state["evaluation"]
        return {
            "queries": clip_list(state["queries"]),
            "missing_aspects": clip_list(list(evaluation.missing_aspects) if evaluation else []),
        }
    if node == "generate":
        return {
            "evidence_count": len(state["evidences"]),
            "feedback_errors": clip_list(state["verification_feedback"]),
        }
    if node == "verify":
        draft = state["answer_draft"]
        return {"claim_count": len(draft.claims) if draft else 0}
    if node == "finalize":
        return {
            "has_draft": state["answer_draft"] is not None,
            "generate_failed": state["generate_failed"],
        }
    return {}


def summarize_output(node: str, updates: dict[str, Any]) -> dict[str, Any]:
    """节点返回值有界摘要：只保留回放/诊断所需的结构化信号与计数。"""
    if node == "plan":
        plan = updates.get("plan")
        return {
            "intent": plan.intent if plan else None,
            "queries": clip_list(updates.get("queries") or []),
        }
    if node == "retrieve":
        evidences = updates.get("evidences")
        summary: dict[str, Any] = {"retrieval_round": updates.get("retrieval_round")}
        if evidences is not None:
            summary["evidence_count"] = len(evidences)
            summary["rel_paths"] = clip_list(
                list(dict.fromkeys(evidence.rel_path for evidence in evidences))
            )
        eliminated = updates.get("evidence_eliminations") or []
        if eliminated:
            # T24：逐条淘汰原因（有界）——"未召回 / 被容量截断 / 方面失去锚点"可事后区分
            summary["eliminated"] = [
                {
                    "chunk_id": str(record.chunk_id),
                    "rel_path": clip_text(record.rel_path),
                    "aspect_ids": list(record.aspect_ids)[:MAX_SUMMARY_ITEMS],
                    "reason": record.reason,
                }
                for record in eliminated[:MAX_SUMMARY_ITEMS]
            ]
        return summary
    if node == "evaluate":
        evaluation = updates.get("evaluation")
        if evaluation is None:
            return {}
        return {
            "sufficiency": evaluation.sufficiency,
            "supported_count": len(evaluation.supported_aspects),
            "missing_aspects": clip_list(list(evaluation.missing_aspects)),
        }
    if node == "refine":
        return {
            "queries": clip_list(updates.get("queries") or []),
            "refine_failed": updates.get("refine_failed"),
        }
    if node == "generate":
        draft = updates.get("answer_draft")
        return {
            "generate_calls": updates.get("generate_calls"),
            "generate_failed": updates.get("generate_failed"),
            "answer_chars": len(draft.answer_text) if draft else None,
            "claim_count": len(draft.claims) if draft else None,
            "not_found_count": len(draft.not_found) if draft else None,
        }
    if node == "verify":
        verification = updates.get("verification")
        if verification is None:
            return {}
        return {
            "passed": verification.passed,
            "l0_passed": verification.l0_passed,
            "l1_passed": verification.l1_passed,
            "errors": clip_list(list(verification.errors)),
            "failed_claims": list(verification.failed_claims)[:MAX_SUMMARY_ITEMS],
        }
    if node == "finalize":
        return {
            "final_mode": updates.get("final_mode"),
            "claim_count": len(updates.get("final_claims") or []),
            "not_found": clip_list(updates.get("final_not_found") or []),
            "answer_chars": len(updates.get("final_answer") or ""),
            "warnings": clip_list(updates.get("warnings") or []),
        }
    return {}


class StepTimer:
    """节点计时；graph 包装器成功/异常两条路径共用。"""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)
