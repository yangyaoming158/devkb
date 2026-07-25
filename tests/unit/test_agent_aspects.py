"""P1.5 T24：跨轮覆盖单调性与 partial 保留（RT-16/20，规格 §6）。

单元层锁定方面分解/保留/单调矩阵三件事，图级 Fake 矩阵精确复现案例九：
第一轮 support=2，补检 CitationParser 时不得挤出 RagService；即使仍缺 RagSupport，
也必须交付范围准确的 partial，而不是 claims/citations 全空的整体 refusal。
"""

from __future__ import annotations

import uuid

import pytest

from devkb.agent.answer import build_answer
from devkb.agent.aspects import (
    AspectStatus,
    build_matrix,
    displaced_aspects,
    has_deliverable_aspect,
    is_monotonically_sufficient,
    label_tokens,
    normalize_aspect_label,
    observe_round,
    outstanding_missing,
    plan_retention,
    retention_warnings,
    supported_labels,
)
from devkb.agent.evidence_types import (
    MAX_REQUIRED_ITEMS,
    CoverageEntry,
    compute_coverage,
    matching_item_ids,
    parse_required_evidence,
)
from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime
from devkb.agent.state import AgentInput, AgentState, Evidence
from devkb.agent.trace import derive_step_status
from devkb.llm import FakeLLM

RAG_SERVICE = "backend/src/main/java/com/ragdocs/service/RagService.java"
RAG_SUPPORT = "backend/src/main/java/com/ragdocs/service/RagSupport.java"
CITATION_PARSER = "backend/src/main/java/com/ragdocs/service/CitationParser.java"
RAG_CONSTANTS = "backend/src/main/java/com/ragdocs/service/RagConstants.java"
PARSE_RESULT = "backend/src/main/java/com/ragdocs/service/CitationParseResult.java"
PARSER_TEST = "backend/src/test/java/com/ragdocs/service/CitationParserTest.java"
ARCH_DOC = "docs/RAG规划-02-架构设计.md"

CASE9_QUESTION = (
    "只依据生产 Java 源码解释 NO_ANSWER 和 UNGROUNDED 分别在什么条件下产生，"
    "非法引用编号如何处理。请引用 RagService、RagSupport、CitationParser 和 "
    "RagConstants，不要用 README、架构文档或测试代替实现。"
)


def _uuid(seed: int) -> uuid.UUID:
    return uuid.UUID(int=seed)


def _pairs(*items: tuple[int, str]) -> list[tuple[uuid.UUID, str]]:
    return [(_uuid(seed), path) for seed, path in items]


# ---- 方面分解与观察 --------------------------------------------------------


def test_required_items_and_evaluator_aspects_form_two_tracks() -> None:
    required = parse_required_evidence("请引用 RagService 与 CitationParser 的生产实现。")
    cited = [("E1", RAG_SERVICE)]
    coverage = compute_coverage(required, cited)

    observations = observe_round(
        required, coverage, cited, ["NO_ANSWER 条件", "NO_ANSWER 条件。"], round_index=1
    )

    determinstic = [obs for obs in observations if obs.origin == "required_evidence"]
    assert [obs.label for obs in determinstic] == ["RagService"]  # 未覆盖项不产生观察
    assert determinstic[0].rel_paths == (RAG_SERVICE,)
    # 归一化后同一方面只记一次（标点/空白不构成新方面）
    assert [obs.label for obs in observations if obs.origin == "evaluator"] == ["NO_ANSWER 条件"]


def test_matrix_keeps_support_when_later_round_loses_the_evidence() -> None:
    required = parse_required_evidence("请引用 RagService 与 CitationParser 的生产实现。")
    round1 = observe_round(
        required,
        compute_coverage(required, [("E1", RAG_SERVICE)]),
        [("E1", RAG_SERVICE)],
        ["NO_ANSWER 条件"],
        round_index=1,
    )
    round2 = observe_round(
        required,
        compute_coverage(required, [("E1", CITATION_PARSER)]),
        [("E1", CITATION_PARSER)],
        ["非法引用编号处理"],
        round_index=2,
    )
    matrix = build_matrix(
        required,
        [*round1, *round2],
        compute_coverage(required, [("E1", CITATION_PARSER)]),
        current_round=2,
    )

    by_label = {row.label: row for row in matrix}
    # 仅被新轮挤出 → 仍是"已支持"，只是 present_now=False（判据："被证伪才可退化"）
    assert by_label["RagService"].supported is True
    assert by_label["RagService"].present_now is False
    assert by_label["RagService"].first_supported_round == 1
    assert by_label["CitationParser"].supported and by_label["CitationParser"].present_now
    assert {row.label for row in displaced_aspects(matrix)} == {"RagService", "NO_ANSWER 条件"}


def test_never_supported_aspect_stays_unsupported() -> None:
    required = parse_required_evidence("请引用 RagService 与 RagSupport 的生产实现。")
    coverage = compute_coverage(required, [("E1", RAG_SERVICE)])
    matrix = build_matrix(
        required,
        observe_round(required, coverage, [("E1", RAG_SERVICE)], [], round_index=1),
        coverage,
        current_round=1,
    )

    assert [(row.label, row.supported) for row in matrix] == [
        ("RagService", True),
        ("RagSupport", False),
    ]
    assert has_deliverable_aspect(matrix) is True
    assert is_monotonically_sufficient(matrix, outstanding=[]) is False


def test_outstanding_missing_ignores_previously_supported_aspects() -> None:
    matrix = (
        AspectStatus(
            aspect_id="evaluator:NO_ANSWER条件",
            label="NO_ANSWER 条件",
            origin="evaluator",
            supported=True,
            first_supported_round=1,
        ),
    )

    assert outstanding_missing(matrix, ["NO_ANSWER 条件。", "RagSupport 判定"]) == [
        "RagSupport 判定"
    ]
    assert outstanding_missing(matrix, ["缺口", "缺口"]) == ["缺口"]  # 去重


def test_empty_matrix_is_never_sufficient() -> None:
    # "既没有任何方面被支持、也没有任何方面被判缺失"绝不能升格为 full
    assert is_monotonically_sufficient((), outstanding=[]) is False
    assert has_deliverable_aspect(()) is False


def test_normalize_aspect_label_is_shared_with_not_found_calibration() -> None:
    from devkb.agent import not_found

    # T23 的"前轮已支持方面不得报缺失"与 T24 的 outstanding 判定必须同源，
    # 否则会出现"not_found 里剔除、却仍按未满足缺口降级"的自相矛盾终态
    assert not_found.normalize_aspect_label is normalize_aspect_label
    assert normalize_aspect_label(" NO_ANSWER 条件。") == normalize_aspect_label("NO_ANSWER条件")


# ---- 跨轮保留（RT-16 机制） -------------------------------------------------


def test_single_round_keeps_retrieval_order_untouched() -> None:
    # 本轮内不重排：E# 编号即召回排名，重排会改变"模型该引哪条"（属检索层，不在 T24）
    required = parse_required_evidence("请引用 OrderService 的生产实现。")
    fresh = _pairs(
        (1, "backend/src/main/java/other/Other.java"),
        (2, "backend/src/main/java/svc/OrderService.java"),
    )

    plan = plan_retention(required, (), fresh, [], limit=12)

    assert plan.kept == (_uuid(1), _uuid(2))
    assert plan.eliminated == () and plan.retained_anchors == ()


def test_refill_round_reserves_slots_for_previously_anchored_aspects() -> None:
    # 案例九核心：补检 CitationParser 时不得把第一轮的 RagService/RagConstants 挤出
    required = parse_required_evidence(
        "请引用 RagService、RagSupport、CitationParser 和 RagConstants 的生产实现。"
    )
    fresh = _pairs((11, CITATION_PARSER), (12, PARSE_RESULT), (13, ARCH_DOC))
    carried = _pairs((1, RAG_SERVICE), (2, RAG_CONSTANTS), (3, PARSER_TEST))

    plan = plan_retention(required, (), fresh, carried, limit=3)

    assert plan.kept == (_uuid(11), _uuid(1), _uuid(2))
    assert plan.retained_anchors == (_uuid(1), _uuid(2))
    eliminated = {record.rel_path: record.reason for record in plan.eliminated}
    assert eliminated == {
        PARSE_RESULT: "capacity_limit",
        ARCH_DOC: "capacity_limit",
        PARSER_TEST: "capacity_limit",
    }
    assert retention_warnings(plan) == ["retrieve: 证据因容量上限淘汰 3 条（未影响已锚定方面）"]


def test_anchor_capacity_loss_is_recorded_not_silent() -> None:
    required = parse_required_evidence(
        "请引用 RagService、RagSupport、CitationParser 和 RagConstants 的生产实现。"
    )
    fresh = _pairs((11, CITATION_PARSER))
    carried = _pairs((1, RAG_SERVICE), (2, RAG_CONSTANTS), (3, RAG_SUPPORT))

    plan = plan_retention(required, (), fresh, carried, limit=2)

    assert plan.kept == (_uuid(11), _uuid(1))  # 锚点最多占 limit-1，至少给新证据留一位
    lost = [record for record in plan.eliminated if record.reason == "aspect_anchor_capacity"]
    assert {record.rel_path for record in lost} == {RAG_CONSTANTS, RAG_SUPPORT}
    # 带 limit_reached 机器标记 → 轨迹层记 degraded（与预算/轮次触顶同口径）
    assert (
        retention_warnings(plan)[0]
        == "retrieve:aspect_anchor_limit_reached: 容量上限使方面失去直接证据（R2,R4）"
    )
    assert derive_step_status({"warnings": retention_warnings(plan)}) == ("degraded", None)


def test_retention_without_requirements_matches_fresh_then_carried() -> None:
    required = parse_required_evidence("订单校验是怎么做的？")
    fresh = _pairs((11, "docs/a.md"), (12, "docs/b.md"))
    carried = _pairs((1, "docs/c.md"), (11, "docs/a.md"))

    plan = plan_retention(required, (), fresh, carried, limit=12)

    assert plan.kept == (_uuid(11), _uuid(12), _uuid(1))  # 同 chunk 去重，新证据在前


def test_evaluator_aspect_is_anchored_through_label_tokens() -> None:
    required = parse_required_evidence("非法引用编号是怎么处理的？")
    protected = (
        AspectStatus(
            aspect_id="evaluator:RagService的NO_ANSWER分支",
            label="RagService 的 NO_ANSWER 分支",
            origin="evaluator",
            supported=True,
            first_supported_round=1,
        ),
    )
    plan = plan_retention(
        required, protected, _pairs((11, CITATION_PARSER)), _pairs((1, RAG_SERVICE)), limit=1
    )

    assert label_tokens("RagService 的 NO_ANSWER 分支") == ("RagService",)
    assert label_tokens("订单校验") == ()  # 纯自然语言方面绑定不到文件 → 不做位次保护
    # limit=1 时锚点占不到槽位（至少留一位给新证据），但失锚必须显式记录
    assert plan.kept == (_uuid(11),)
    assert [record.reason for record in plan.eliminated] == ["aspect_anchor_capacity"]


def test_retention_never_exceeds_limit_and_is_deterministic() -> None:
    required = parse_required_evidence(
        "请引用 RagService、RagSupport、CitationParser 和 RagConstants 的生产实现。"
    )
    assert len(required.items) <= MAX_REQUIRED_ITEMS
    fresh = _pairs(*[(20 + i, f"docs/f{i}.md") for i in range(6)])
    carried = _pairs((1, RAG_SERVICE), (2, RAG_SUPPORT), (3, CITATION_PARSER), (4, RAG_CONSTANTS))

    for limit in range(1, 13):
        plan = plan_retention(required, (), fresh, carried, limit=limit)
        assert len(plan.kept) == min(limit, len(fresh) + len(carried))
        assert len(set(plan.kept)) == len(plan.kept)
        assert len(plan.retained_anchors) <= max(limit - 1, 0)
        assert set(plan.kept) | {record.chunk_id for record in plan.eliminated} == {
            *[chunk_id for chunk_id, _ in fresh],
            *[chunk_id for chunk_id, _ in carried],
        }
        assert plan == plan_retention(required, (), fresh, carried, limit=limit)

    with pytest.raises(ValueError):
        plan_retention(required, (), fresh, carried, limit=0)


def test_anchor_matcher_is_the_coverage_matcher() -> None:
    # 防漂移：保留用的身份判定与覆盖矩阵必须同一 matcher，否则"保下来的证据"
    # 与"覆盖矩阵认可的证据"会随修复分叉
    required = parse_required_evidence("请引用 RagService 的生产实现。")
    for path in (RAG_SERVICE, PARSER_TEST, ARCH_DOC, RAG_SUPPORT):
        covered = {
            entry.item_id for entry in compute_coverage(required, [("E1", path)]) if entry.covered
        }
        assert set(matching_item_ids(required, path)) == covered


def test_matrix_present_now_follows_current_coverage_only() -> None:
    required = parse_required_evidence("请引用 RagService 的生产实现。")
    observations = observe_round(
        required,
        compute_coverage(required, [("E1", RAG_SERVICE)]),
        [("E1", RAG_SERVICE)],
        [],
        round_index=1,
    )
    matrix = build_matrix(
        required, observations, (CoverageEntry(item_id="R1", covered=False),), current_round=2
    )

    assert matrix[0].supported is True and matrix[0].present_now is False
    assert supported_labels(matrix) == ["RagService"]
    assert supported_labels(matrix, origin="evaluator") == []


# ---- 图级 Fake 矩阵：案例九复现 ---------------------------------------------

PLAN = '{"intent":"knowledge_qa","queries":["NO_ANSWER 条件"]}'
REFINE = '{"queries":["CitationParser 生产源码"]}'
EVAL_ROUND1 = (
    '{"sufficiency":"partial","supported_aspects":["NO_ANSWER 条件","UNGROUNDED 条件"],'
    '"missing_aspects":["CitationParser 生产源码"]}'
)
# 案例九的倒退：第二轮证据集换掉后 evaluate 退化为 insufficient、supported 2→1，
# 并把第一轮已支持的两个方面重新报成缺失
EVAL_ROUND2_REGRESSED = (
    '{"sufficiency":"insufficient","supported_aspects":["非法引用编号处理"],'
    '"missing_aspects":["NO_ANSWER 条件","UNGROUNDED 条件","RagSupport 生产源码"]}'
)
GEN_PARTIAL = (
    '{"answer_text":"非法引用编号只保留合法 citation [E1]。'
    'NO_ANSWER 由无 grounded hit 触发 [E2]。",'
    '"claims":[{"text":"非法引用编号只保留合法 citation","evidence_ids":["E1"],'
    '"quotes":["CitationParser.java 的生产实现片段。"]},'
    '{"text":"NO_ANSWER 由无 grounded hit 触发","evidence_ids":["E2"],'
    '"quotes":["RagService.java 的生产实现片段。"]}],"not_found":[]}'
)


def _content(rel_path: str) -> str:
    return f"{rel_path.rsplit('/', 1)[-1]} 的生产实现片段。"


def _rounds_runtime(
    script: list[str], rounds: list[list[str]], *, max_evidences: int
) -> AgentRuntime:
    counter = {"round": 0}

    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        paths = rounds[min(counter["round"], len(rounds) - 1)]
        counter["round"] += 1
        return [
            Evidence(
                evidence_id=f"E{index}",
                chunk_id=_uuid(100 * counter["round"] + index),
                rel_path=path,
                title_path="",
                content=_content(path),
                start_line=1,
                end_line=9,
                score=0.9 - 0.1 * index,
            )
            for index, path in enumerate(paths, start=1)
        ]

    return AgentRuntime(llm=FakeLLM(list(script)), retriever=retriever, max_evidences=max_evidences)


def _input(question: str) -> AgentInput:
    return AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question=question)


async def _run_case_nine() -> AgentState:
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, EVAL_ROUND2_REGRESSED, GEN_PARTIAL],
        [
            [RAG_SERVICE, RAG_CONSTANTS, PARSER_TEST],
            [CITATION_PARSER, PARSE_RESULT, ARCH_DOC],
        ],
        max_evidences=3,
    )
    return await run_agent(runtime, _input(CASE9_QUESTION))


async def test_case_nine_refill_does_not_drop_first_round_evidence() -> None:
    result = await _run_case_nine()

    paths = [evidence.rel_path for evidence in result["evidences"]]
    assert CITATION_PARSER in paths  # 补检结果确实进来了
    assert RAG_SERVICE in paths and RAG_CONSTANTS in paths  # 第一轮已锚定的方面没被挤出
    assert result["retrieval_round"] == 2 and result["node_history"].count("retrieve") == 2


async def test_case_nine_delivers_scoped_partial_instead_of_full_refusal() -> None:
    result = await _run_case_nine()

    # RT-20：仍缺 RagSupport，但已有 CitationParser/RagService 直接证据 → 范围准确的 partial
    assert result["final_mode"] == "partial"
    assert [claim.text for claim in result["final_claims"]] == [
        "非法引用编号只保留合法 citation",
        "NO_ANSWER 由无 grounded hit 触发",
    ]
    answer = build_answer(result)
    assert {citation["rel_path"] for citation in answer["citations"]} == {
        CITATION_PARSER,
        RAG_SERVICE,
    }
    assert result["llm_calls"] == 5  # plan+evaluate+refine+evaluate+generate，D6 预算内
    assert result["node_history"][-3:] == ["generate", "verify", "finalize"]


async def test_case_nine_previously_supported_aspects_are_not_reported_missing() -> None:
    result = await _run_case_nine()

    # 第一轮已支持的 NO_ANSWER/UNGROUNDED 不得因末轮倒退而变成用户可见缺口
    assert not any("NO_ANSWER" in item for item in result["final_not_found"])
    assert not any("UNGROUNDED" in item for item in result["final_not_found"])
    # 真正从未取得证据的 RagSupport 仍要如实披露
    assert any("RagSupport" in item for item in result["final_not_found"])
    assert any("必需证据未覆盖" in warning for warning in result["warnings"])


async def test_gap_wording_separates_absent_from_retrieved_but_uncited() -> None:
    """跨轮保留后"已召回但没被引用"变多：仍写成"未取得"会与矩阵 present_now 矛盾。"""
    result = await _run_case_nine()

    # RagSupport 两轮都没召回 → 确实未取得
    assert any(
        "未取得用户要求的必需证据" in item and "RagSupport" in item
        for item in result["final_not_found"]
    )
    # RagConstants 被跨轮保留在证据集里，只是没有 claim 引用它 → 措辞必须区分
    assert any(
        "已在本次证据集中，但未被任何断言直接引用" in item and "RagConstants" in item
        for item in result["final_not_found"]
    )
    assert not any(
        "未取得用户要求的必需证据" in item and "RagConstants" in item
        for item in result["final_not_found"]
    )


async def test_zero_deliverable_evidence_still_refuses() -> None:
    # refusal 仅用于零可交付证据：没有任何方面取得过直接证据时不得假 partial
    eval_none = (
        '{"sufficiency":"insufficient","supported_aspects":[],"missing_aspects":["生产实现"]}'
    )
    runtime = _rounds_runtime(
        [PLAN, eval_none, REFINE, eval_none],
        [[ARCH_DOC], [PARSER_TEST]],
        max_evidences=3,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION))

    assert result["final_mode"] == "refusal"
    assert result["final_claims"] == [] and result["generate_calls"] == 0


async def test_refine_failure_still_delivers_supported_aspects_as_partial() -> None:
    # 同型缺陷换一条分支：refine 解析失败此前直奔 finalize→refusal，把第一轮
    # 已有直接证据的方面一并丢弃（判据："refusal 仅用于零可交付证据"）
    gen_round1 = (
        '{"answer_text":"NO_ANSWER 由无 grounded hit 触发 [E1]。",'
        '"claims":[{"text":"NO_ANSWER 由无 grounded hit 触发","evidence_ids":["E1"],'
        '"quotes":["RagService.java 的生产实现片段。"]}],"not_found":[]}'
    )
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, "{}", "{}", gen_round1],
        [[RAG_SERVICE, RAG_CONSTANTS, PARSER_TEST]],
        max_evidences=3,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION))

    assert result["refine_failed"] is True and result["retrieval_round"] == 1
    assert result["node_history"][-4:] == ["refine", "generate", "verify", "finalize"]
    assert result["final_mode"] == "partial"
    assert [claim.text for claim in result["final_claims"]] == ["NO_ANSWER 由无 grounded hit 触发"]
    assert result["llm_calls"] == 5  # plan+evaluate+refine×2(重问)+generate，仍在 D6 预算内


async def test_monotonic_support_cannot_manufacture_full_without_citations() -> None:
    """单调矩阵只能阻止不当降级，绝不能反过来放宽 full 门（T22 硬门仍是最终防线）。

    构造：两个点名类跨轮都取得过直接证据（矩阵全支持），但第一轮的 RagService 因
    容量被挤出、终稿只引用了 CitationParser → 必须 partial + 未覆盖告警，不得 full。
    """
    eval2 = (
        '{"sufficiency":"sufficient","supported_aspects":["非法引用编号处理"],"missing_aspects":[]}'
    )
    gen_one_citation = (
        '{"answer_text":"非法引用编号只保留合法 citation [E1]。",'
        '"claims":[{"text":"非法引用编号只保留合法 citation","evidence_ids":["E1"],'
        '"quotes":["CitationParser.java 的生产实现片段。"]}],"not_found":[]}'
    )
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, eval2, gen_one_citation],
        [[RAG_SERVICE], [CITATION_PARSER]],
        max_evidences=1,
    )
    result = await run_agent(
        runtime, _input("请引用 RagService 和 CitationParser 的生产实现说明非法引用编号处理。")
    )

    assert [evidence.rel_path for evidence in result["evidences"]] == [CITATION_PARSER]
    assert result["final_mode"] == "partial"
    assert any("必需证据未覆盖" in warning for warning in result["warnings"])
    assert any("被挤出，非证伪" in warning for warning in result["warnings"])


async def test_all_aspects_cited_still_reaches_full() -> None:
    # 反向锁：跨轮取得且逐项被引用时，末轮 evaluate 的扁平 insufficient 不得阻止 full
    question = "请引用 RagService 和 CitationParser 的生产实现说明非法引用编号处理。"
    eval2 = (
        '{"sufficiency":"insufficient","supported_aspects":["非法引用编号处理"],'
        '"missing_aspects":[]}'
    )
    gen_both = (
        '{"answer_text":"非法引用编号只保留合法 citation [E1]。NO_ANSWER 由无 hit 触发 [E2]。",'
        '"claims":[{"text":"非法引用编号只保留合法 citation","evidence_ids":["E1"],'
        '"quotes":["CitationParser.java 的生产实现片段。"]},'
        '{"text":"NO_ANSWER 由无 hit 触发","evidence_ids":["E2"],'
        '"quotes":["RagService.java 的生产实现片段。"]}],"not_found":[]}'
    )
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, eval2, gen_both],
        [[RAG_SERVICE], [CITATION_PARSER]],
        max_evidences=3,
    )
    result = await run_agent(runtime, _input(question))

    assert {evidence.rel_path for evidence in result["evidences"]} == {
        CITATION_PARSER,
        RAG_SERVICE,
    }
    assert result["final_mode"] == "full" and result["final_not_found"] == []


async def test_displaced_aspect_is_reported_as_squeezed_out_not_refuted() -> None:
    # 容量不足以保住锚点时（limit=1）覆盖仍单调：方面保持已支持，但必须显式告警
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, EVAL_ROUND2_REGRESSED, GEN_PARTIAL],
        [[RAG_SERVICE], [CITATION_PARSER]],
        max_evidences=1,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION))

    assert [evidence.rel_path for evidence in result["evidences"]] == [CITATION_PARSER]
    assert any("被挤出，非证伪" in warning for warning in result["warnings"])
    assert result["final_mode"] == "partial"
