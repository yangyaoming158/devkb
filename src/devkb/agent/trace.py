"""T18 轨迹记录（规格 §5.2/§5.3/§11）。

图执行期间在内存收集节点 step、LLM 请求与工具调用明细，run 结束后由
service 统一写入 agent_steps / tool_invocations。摘要一律有界：只存结构化
信号、计数与截断后的短文本——不记录 chain-of-thought、secret、完整 Prompt
或文档/回答正文（正文只以字符数出现）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Literal

from devkb.agent.aspects import MAX_ELIMINATION_RECORDS
from devkb.retrieval import MAX_FINAL_TOP_K, MAX_SUBQUERIES

MAX_SUMMARY_ITEMS = 8
MAX_SUMMARY_CHARS = 200

# T30.1（RT-07/m15）候选账本的冻结上限：单条子查询不超过融合前单通道候选数
# （在线检索器的通道 N 恒等于 final top_k），单轮总数按子查询上限推导。
# 两者都从 devkb.retrieval 的既有上限推出，不可能与检索侧漂移。
MAX_CANDIDATES_PER_QUERY = MAX_FINAL_TOP_K
MAX_CANDIDATE_RECORDS = MAX_SUBQUERIES * MAX_CANDIDATES_PER_QUERY
SCORE_DIGITS = 6

StepStatus = Literal["ok", "degraded", "failed"]
# selected = 落在检索器 final top_k 内；truncated_after_fusion = 进了通道候选但
# RRF 完整融合序名次在 top_k 之外。**通道 top-N 之外的内容根本没有被观测**，
# 因此"没有记录"只说明未观测，不构成"未召回/不存在"的断言（判据可证边界 J1）。
CandidateOutcome = Literal["selected", "truncated_after_fusion"]
# refine term 的**字面**来源，非因果：user_question/retrieved_evidence 只证明规范化
# 子串包含，unattributed 表示无法逐字追溯（模型重写的正常结果，不是失败）。
RefineTermSource = Literal["default", "user_question", "retrieved_evidence", "unattributed"]

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
class CandidateRecord:
    """一条 (子查询, chunk) 候选的检索侧元数据（T30.1/RT-07）。

    只存定位与名次信息——**没有 content 字段**；`query_index` 指回该轮 step
    `input_summary["queries"]` / tool `arguments["queries"]` 里已经落盘的查询文本，
    因此本记录不引入任何新的自由文本。
    """

    query_index: int
    chunk_id: str
    rel_path: str
    start_line: int
    end_line: int
    vector_rank: int  # 1-based，该子查询 vector 通道内名次
    vector_score: float  # 该子查询的余弦相似度（量纲与 fused_score 不同，不可混读）
    fused_score: float  # RRF 融合分：Σ 1/(k + vector_rank)，可由本记录集合复算
    fused_rank: int  # 1-based，**完整**融合序名次（不是截断后的）
    outcome: CandidateOutcome


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
    # T30.1：节点/检索器产生、但不进 AgentState 的诊断事实。与上面两个缓冲同一模式
    # （生产者 push、当前 step 的 finish_step 一次性 drain），依赖同一条拓扑事实：图顺序执行。
    _pending_output: dict[str, Any] = field(default_factory=dict)

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

    def record_retrieval_candidates(
        self, records: Sequence[CandidateRecord], *, truncated: bool
    ) -> None:
        """逐候选检索元数据（T30.1）；只在融合完成后调用，检索失败时一条都不写。"""
        self._pending_output["candidates"] = [asdict(record) for record in records]
        self._pending_output["candidates_truncated"] = truncated

    def record_retention_input(self, chunk_ids: Sequence[uuid.UUID], *, limit: int) -> None:
        """跨轮保留裁剪的**排名输入序**（T30.1）。

        有了它，"低排名被截断"才能由位次关系判定——`eliminated[].reason` 只区分
        "丢了方面锚点"与"纯容量"，两者都可能发生在高排名非锚点被低排名锚点挤出时。
        """
        self._pending_output["retention_input"] = [str(chunk_id) for chunk_id in chunk_ids]
        self._pending_output["retention_limit"] = limit

    def record_refine_term_sources(self, sources: Sequence[RefineTermSource]) -> None:
        """逐条 refine query 的字面来源标签，按位与同一摘要的 `queries` 对齐。"""
        self._pending_output["refine_term_sources"] = list(sources)

    def record_evidence_usage(
        self,
        *,
        generate_executed: bool,
        given: Sequence[str],
        cited: Sequence[str],
        unused: Sequence[str],
    ) -> None:
        """交给模型的证据与交付引用的差集。generate 未执行时四项全空——
        "没交给模型"不得被写成"已给模型但未采用"。"""
        self._pending_output["evidence_usage"] = {
            "generate_executed": generate_executed,
            "given": list(given),
            "cited": list(cited),
            "unused": list(unused),
        }

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
        if self._pending_output:
            output_summary = {**(output_summary or {}), **self._pending_output}
            self._pending_output.clear()
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
            # T30.1：节点入参本身就是"到底把哪几条证据给了模型"的**权威账本**。
            # finalize 的 evidence_usage 只有在本键存在（即 generate 真的执行过）时才成立。
            "evidence_ids": [evidence.evidence_id for evidence in state["evidences"]],
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
    if node == "policy_refuse":
        # T27.1：触发层是本终态唯一需要回放的结构化信号（规格 §9"记录触发层 rule/plan"）。
        # 不记问题文本——攻击串没有进入持久化摘要的理由。
        return {"trigger_layer": state["policy_trigger"]}
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
            # T30.1：候选账本按 chunk_id 索引，rel_paths 去重后又无法回指——没有这份
            # E# ↔ chunk_id ↔ 行号的对照，"哪些候选最终到了模型手里"接不上（有界：
            # 条数受 AgentRuntime.max_evidences ≤ MAX_FINAL_TOP_K 约束）
            summary["evidences"] = [
                {
                    "evidence_id": evidence.evidence_id,
                    "chunk_id": str(evidence.chunk_id),
                    "rel_path": evidence.rel_path,
                    "start_line": evidence.start_line,
                    "end_line": evidence.end_line,
                }
                for evidence in evidences
            ]
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
                # 按账本硬上限落盘（不是 MAX_SUMMARY_ITEMS）：step summary 是唯一持久化
                # 载体，截成 8 条会让"逐条回放淘汰原因"不成立（T24 二审发现4）
                for record in eliminated[:MAX_ELIMINATION_RECORDS]
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
            # T30.1：逐条缺口的来源与事实校验依据（m15 断言 3）。**不转载 text/original_text**
            # ——它们已在同一摘要的 not_found 与 Answer JSON 里，重复落盘只会多一份自由文本。
            # 本摘要按 MAX_SUMMARY_ITEMS 截断，与 not_found 逐位对齐；**权威全量在 Answer JSON**。
            "not_found_details": [
                {
                    "category": detail.category,
                    "source": detail.source,
                    "basis": detail.basis,
                    "refs": list(detail.refs),
                }
                for detail in (updates.get("final_not_found_details") or ())[:MAX_SUMMARY_ITEMS]
            ],
            "answer_chars": len(updates.get("final_answer") or ""),
            "warnings": clip_list(updates.get("warnings") or []),
        }
    if node == "policy_refuse":
        # tool_count 恒 0 由拓扑保证（retrieve 不可达）；显式落盘使"零工具调用"
        # 在持久化轨迹里可直接核对，而不必反推 tool_invocations 的缺席。
        return {
            "final_mode": updates.get("final_mode"),
            "trigger_layer": updates.get("policy_trigger"),
            "tool_count": 0,
        }
    return {}


class StepTimer:
    """节点计时；graph 包装器成功/异常两条路径共用。"""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)
