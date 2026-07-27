"""T16 Agent 状态与 LLM 结构化输出契约。"""

from __future__ import annotations

import operator
import uuid
from decimal import Decimal
from typing import Annotated, Literal, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from devkb.agent.aspects import AspectObservation, EliminationRecord
from devkb.agent.evidence_types import (
    MAX_REQUIRED_ITEMS,
    CoverageEntry,
    RequiredEvidence,
    parse_required_evidence,
)
from devkb.agent.not_found import NotFoundDetail

MAX_QUESTION_CHARS = 4000
MAX_QUERY_CHARS = 4000
MAX_SUBQUERIES = 3
MAX_ANSWER_CHARS = 16_000
MAX_LLM_REQUESTS = 6
MAX_RETRIEVAL_ROUNDS = 2
MAX_REASKS_PER_CALL = 1
MAX_REFINE_CALLS = 1
MAX_GENERATE_CALLS = 2
MAX_CLAIMS = 20
MAX_CLAIM_EVIDENCE_IDS = 12  # 与 retrieval.MAX_FINAL_TOP_K 对齐（有测试锁定）
MAX_QUOTES_PER_CLAIM = 4

Intent = Literal["knowledge_qa"]
Sufficiency = Literal["sufficient", "partial", "insufficient"]
FinalMode = Literal["full", "partial", "refusal"]

QuestionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS),
]
QueryText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS),
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ClaimText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]
QuoteText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
EvidenceId = Annotated[str, StringConstraints(pattern=r"^E[1-9][0-9]*$")]


class StrictModel(BaseModel):
    """LLM 输出一律禁止额外字段和宽松类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class AgentInput(StrictModel):
    """仅由应用服务构造；身份字段不出现在任何 LLM 输出 schema 中。"""

    run_id: uuid.UUID
    project_id: uuid.UUID
    question: QuestionText


class PlanOutput(StrictModel):
    intent: Intent
    queries: list[QueryText] = Field(min_length=1, max_length=MAX_SUBQUERIES)
    # 上限与确定性解析的 items 上限同源（MAX_REQUIRED_ITEMS）：否则 required 可多于
    # 模型能回显的条数，"完整逐项回填"成为不可能满足的契约（五审发现6）
    required_evidence: list[ShortText] = Field(default_factory=list, max_length=MAX_REQUIRED_ITEMS)

    @field_validator("queries")
    @classmethod
    def deduplicate_queries(cls, queries: list[str]) -> list[str]:
        return list(dict.fromkeys(queries))


class CoverageReport(StrictModel):
    """LLM 对某条必需证据的覆盖观察；仅诊断，不作事实来源（裁决权在确定性代码）。"""

    item_id: ShortText
    covered: bool
    evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=MAX_CLAIM_EVIDENCE_IDS)


class EvaluateOutput(StrictModel):
    sufficiency: Sufficiency
    supported_aspects: list[ShortText] = Field(default_factory=list, max_length=10)
    missing_aspects: list[ShortText] = Field(default_factory=list, max_length=10)
    coverage: list[CoverageReport] = Field(default_factory=list, max_length=MAX_REQUIRED_ITEMS)


class RefineOutput(StrictModel):
    queries: list[QueryText] = Field(min_length=1, max_length=MAX_SUBQUERIES)

    @field_validator("queries")
    @classmethod
    def deduplicate_queries(cls, queries: list[str]) -> list[str]:
        return list(dict.fromkeys(queries))


class ClaimOutput(StrictModel):
    """事实性断言：必须绑定至少一个 evidence（规格 §10 full 模式前提）。"""

    text: ClaimText
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=MAX_CLAIM_EVIDENCE_IDS)
    quotes: list[QuoteText] = Field(default_factory=list, max_length=MAX_QUOTES_PER_CLAIM)

    @field_validator("evidence_ids")
    @classmethod
    def deduplicate_evidence_ids(cls, evidence_ids: list[str]) -> list[str]:
        return list(dict.fromkeys(evidence_ids))


class GenerateOutput(StrictModel):
    answer_text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ANSWER_CHARS),
    ]
    claims: list[ClaimOutput] = Field(default_factory=list, max_length=MAX_CLAIMS)
    not_found: list[ShortText] = Field(default_factory=list, max_length=10)


class DraftSnapshot(BaseModel):
    """第一稿的跨稿复检快照（T25.2）：只留判据求值用得到的两项事实。

    不留正文、claim 与引用账本：finalize 在 claim 被移除时按保留 claim **重建**正文并
    丢弃原正文独立 ``[E#]`` 标记（``nodes.py`` 的 removed 分支），两稿引用账本口径无法在
    不改动既有构造的前提下对齐，据此做子集比较只会造出虚假的覆盖收缩（用户裁决 C1）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    # 第一稿 GenerateOutput.not_found 的原始字段（未经 T23 校准）
    not_found: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class Evidence(StrictModel):
    evidence_id: Annotated[str, StringConstraints(pattern=r"^E[1-9][0-9]*$")]
    chunk_id: uuid.UUID
    rel_path: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    title_path: Annotated[str, StringConstraints(max_length=2000)] = ""
    content: Annotated[str, StringConstraints(min_length=1)]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    score: float

    @model_validator(mode="after")
    def line_range_is_ordered(self) -> Evidence:
        if self.end_line < self.start_line:
            raise ValueError("end_line 不得小于 start_line")
        return self


class VerificationOutput(StrictModel):
    """由确定性代码构造（非 LLM 输出）；errors 为机器可读反馈，可回传 generate。"""

    passed: bool
    l0_passed: bool
    l1_passed: bool
    errors: list[ShortText] = Field(default_factory=list, max_length=2 * MAX_CLAIMS)
    failed_claims: list[Annotated[int, Field(ge=0)]] = Field(
        default_factory=list, max_length=MAX_CLAIMS
    )


class AgentState(TypedDict):
    run_id: uuid.UUID
    project_id: uuid.UUID
    question: str
    required_evidence: RequiredEvidence
    coverage: tuple[CoverageEntry, ...]
    plan: PlanOutput | None
    queries: list[str]
    retrieval_round: int
    evidences: list[Evidence]
    # T23.2 事实校验的历史账本：**全部检索轮**出现过的证据路径。
    # 只增不减（reducer=operator.add），使"任一轮出现过的文件不得写成不存在"可判定。
    evidence_path_history: Annotated[list[str], operator.add]
    # T24 跨轮方面账本：每轮 evaluate 后追加"该方面本轮已取得直接证据"的观察。
    # 同为只增账本——覆盖单调性、假拒答判定与 T23 的"前轮已支持方面"都由它折出，
    # 同一事实不存两份（口径漂移是前几轮复审反复踩的坑）。
    aspect_observations: Annotated[list[AspectObservation], operator.add]
    # T24 淘汰账本：跨轮合并里每条被截断的证据（chunk_id/rel_path/所锚定方面/原因），
    # 使"这条证据为什么不在终态证据集"可事后逐条审计，而不是只剩一条汇总 warning。
    # 有界：单轮 ≤ MAX_ELIMINATION_RECORDS，轮次本身受 MAX_RETRIEVAL_ROUNDS 限制。
    evidence_eliminations: Annotated[list[EliminationRecord], operator.add]
    evaluation: EvaluateOutput | None
    answer_draft: GenerateOutput | None
    # T25.2 跨稿复检快照：generate 每次直接覆写 answer_draft，第一稿的自述缺口在状态里
    # 不留痕，finalize 因而只看得到最终那一稿——模型删掉自己的缺口自述即可把终态升成
    # full。首次 generate 写入后只读，最多两稿（MAX_GENERATE_CALLS）故只需一份。
    first_draft: DraftSnapshot | None
    verification: VerificationOutput | None
    verification_feedback: list[str]
    final_answer: str | None
    final_mode: FinalMode | None
    final_claims: list[ClaimOutput]
    final_not_found: list[str]
    final_not_found_details: tuple[NotFoundDetail, ...]
    model: str
    status: Literal["running", "succeeded", "failed"]
    llm_calls: int
    llm_retries: int
    retry_counts: dict[str, int]
    tokens_in: int
    tokens_out: int
    cost: Decimal | None
    latency_ms: int
    refine_calls: int
    generate_calls: int
    refine_failed: bool
    generate_failed: bool
    node_history: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]


def initial_agent_state(agent_input: AgentInput) -> AgentState:
    """构造完整初态；LLM 节点只更新业务字段，身份字段永不来自模型。"""
    return AgentState(
        run_id=agent_input.run_id,
        project_id=agent_input.project_id,
        question=agent_input.question,
        required_evidence=parse_required_evidence(agent_input.question),
        coverage=(),
        plan=None,
        queries=[],
        retrieval_round=0,
        evidences=[],
        evidence_path_history=[],
        aspect_observations=[],
        evidence_eliminations=[],
        evaluation=None,
        answer_draft=None,
        first_draft=None,
        verification=None,
        verification_feedback=[],
        final_answer=None,
        final_mode=None,
        final_claims=[],
        final_not_found=[],
        final_not_found_details=(),
        model="",
        status="running",
        llm_calls=0,
        llm_retries=0,
        retry_counts={},
        tokens_in=0,
        tokens_out=0,
        cost=Decimal("0"),
        latency_ms=0,
        refine_calls=0,
        generate_calls=0,
        refine_failed=False,
        generate_failed=False,
        node_history=[],
        warnings=[],
        errors=[],
    )
