"""P1.5 T24：跨轮覆盖单调性与 partial 保留（RT-16/20，规格 §6）。

单元层锁定方面分解/保留/单调矩阵三件事，图级 Fake 矩阵精确复现案例九：
第一轮 support=2，补检 CitationParser 时不得挤出 RagService；即使仍缺 RagSupport，
也必须交付范围准确的 partial，而不是 claims/citations 全空的整体 refusal。
"""

from __future__ import annotations

import uuid
from typing import get_args

import pytest

from devkb.agent.answer import build_answer
from devkb.agent.aspects import (
    AspectStatus,
    EvidenceRef,
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
    EvidenceType,
    compute_coverage,
    iter_target_spans,
    matching_item_ids,
    parse_required_evidence,
    target_tokens_all_match,
)
from devkb.agent.graph import has_deliverable_content, route_after_evaluate, run_agent
from devkb.agent.nodes import (
    _TYPE_QUALIFIERS,
    AgentRuntime,
    strip_items_contradicting_displaced_notes,
)
from devkb.agent.not_found import NotFoundInput
from devkb.agent.state import (
    AgentInput,
    AgentState,
    EvaluateOutput,
    Evidence,
    initial_agent_state,
)
from devkb.agent.trace import TraceRecorder, derive_step_status
from devkb.llm import FakeLLM

RAG_SERVICE = "backend/src/main/java/com/ragdocs/service/RagService.java"
RAG_SUPPORT = "backend/src/main/java/com/ragdocs/service/RagSupport.java"
CITATION_PARSER = "backend/src/main/java/com/ragdocs/service/CitationParser.java"
RAG_CONSTANTS = "backend/src/main/java/com/ragdocs/service/RagConstants.java"
PARSE_RESULT = "backend/src/main/java/com/ragdocs/service/CitationParseResult.java"
PARSER_TEST = "backend/src/test/java/com/ragdocs/service/CitationParserTest.java"
ARCH_DOC = "docs/RAG规划-02-架构设计.md"
OTHER_ONE = "backend/src/main/java/com/ragdocs/service/OtherOne.java"
OTHER_TWO = "backend/src/main/java/com/ragdocs/service/OtherTwo.java"

CASE9_QUESTION = (
    "只依据生产 Java 源码解释 NO_ANSWER 和 UNGROUNDED 分别在什么条件下产生，"
    "非法引用编号如何处理。请引用 RagService、RagSupport、CitationParser 和 "
    "RagConstants，不要用 README、架构文档或测试代替实现。"
)


def _uuid(seed: int) -> uuid.UUID:
    return uuid.UUID(int=seed)


def _pairs(*items: tuple[int, str]) -> list[tuple[uuid.UUID, str]]:
    return [(_uuid(seed), path) for seed, path in items]


def _refs(*items: tuple[int, str]) -> list[EvidenceRef]:
    """(chunk 序号, 路径) → 证据引用；evidence_id 按顺序编号（轮内编号，跨轮无意义）。"""
    return [
        EvidenceRef(evidence_id=f"E{index}", chunk_id=_uuid(seed), rel_path=path)
        for index, (seed, path) in enumerate(items, start=1)
    ]


# ---- 方面分解与观察 --------------------------------------------------------


def test_required_items_and_evaluator_aspects_form_two_tracks() -> None:
    required = parse_required_evidence("请引用 RagService 与 CitationParser 的生产实现。")
    coverage = compute_coverage(required, [("E1", RAG_SERVICE)])

    observations = observe_round(
        required,
        coverage,
        _refs((1, RAG_SERVICE)),
        ["NO_ANSWER 条件", "NO_ANSWER 条件。"],
        round_index=1,
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
        _refs((1, RAG_SERVICE)),
        ["NO_ANSWER 条件"],
        round_index=1,
    )
    round2 = observe_round(
        required,
        compute_coverage(required, [("E1", CITATION_PARSER)]),
        _refs((2, CITATION_PARSER)),
        ["非法引用编号处理"],
        round_index=2,
    )
    matrix = build_matrix(
        required,
        [*round1, *round2],
        compute_coverage(required, [("E1", CITATION_PARSER)]),
        present_chunk_ids=[_uuid(2)],
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
        observe_round(required, coverage, _refs((1, RAG_SERVICE)), [], round_index=1),
        coverage,
        present_chunk_ids=[_uuid(1)],
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


def test_diagnostic_track_alone_can_never_grant_sufficiency() -> None:
    """用户裁决（2026-07-25）：诊断轨只给 partial/refusal 的单调下界，不得授予 full。

    复审给的反例：无 required item 的双方面题，两轮各自报支持一个方面，终稿只引用
    其中一个——矩阵会把两个方面都算 supported，若据此判充分，空 required 硬门会真空
    通过，本该 partial 的终态会升成错误 full。
    """
    required = parse_required_evidence("订单校验和库存扣减分别是怎么做的？")
    assert required.items == ()
    observations = [
        *observe_round(required, (), _refs((1, ARCH_DOC)), ["订单校验"], round_index=1),
        *observe_round(required, (), _refs((1, ARCH_DOC)), ["库存扣减"], round_index=2),
    ]
    matrix = build_matrix(required, observations, (), present_chunk_ids=[_uuid(1)])

    assert [row.origin for row in matrix] == ["evaluator", "evaluator"]
    assert all(row.supported for row in matrix)  # 单调下界仍然成立
    assert is_monotonically_sufficient(matrix, outstanding=[]) is False  # 但不得授予 full


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
        # 确定性轨代表可以占满全部槽位（用户点名的证据优先于无关的新召回）；
        # 只有诊断轨代表要给新证据让位，见
        # test_diagnostic_anchor_never_evicts_the_whole_refill_round
        assert len(plan.retained_anchors) <= limit
        assert set(plan.kept) | {record.chunk_id for record in plan.eliminated} == {
            *[chunk_id for chunk_id, _ in fresh],
            *[chunk_id for chunk_id, _ in carried],
        }
        assert plan == plan_retention(required, (), fresh, carried, limit=limit)

    with pytest.raises(ValueError):
        plan_retention(required, (), fresh, carried, limit=0)


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5, 6])
def test_deterministic_aspects_are_never_starved_while_capacity_allows(limit: int) -> None:
    """不变量（复审发现1 的一般化）：只要"有候选证据的确定性方面数 ≤ limit"，
    这些方面在保留集中就必须各有一条直接证据——无论它的候选来自本轮还是上一轮。"""
    required = parse_required_evidence(
        "请引用 RagService、RagSupport、CitationParser 和 RagConstants 的生产实现。"
    )
    fresh = _pairs((11, OTHER_ONE), (12, RAG_SERVICE), (13, OTHER_TWO), (14, RAG_SUPPORT))
    carried = _pairs((1, CITATION_PARSER), (2, ARCH_DOC), (3, RAG_CONSTANTS))
    candidates = {RAG_SERVICE, RAG_SUPPORT, CITATION_PARSER, RAG_CONSTANTS}
    path_by_id = dict([*fresh, *carried])

    plan = plan_retention(required, (), fresh, carried, limit=limit)
    kept_paths = {path_by_id[chunk_id] for chunk_id in plan.kept}

    if limit >= len(candidates):
        assert candidates <= kept_paths
        assert not [r for r in plan.eliminated if r.reason == "aspect_anchor_capacity"]
    else:  # 容量真的不够时才允许失锚，且必须逐条记录
        assert len(kept_paths & candidates) == limit
        assert [r for r in plan.eliminated if r.reason == "aspect_anchor_capacity"]
    # 无论容量如何，两个来源各自的相对次序都不变
    for source in (fresh, carried):
        order = [chunk_id for chunk_id, _ in source if chunk_id in set(plan.kept)]
        assert [c for c in plan.kept if c in set(order)] == order


def test_required_representative_outranks_diagnostic_representative() -> None:
    """二审发现1：两轨的新证据代表被合并进同一个桶后按召回排名截断，诊断轨代表会把
    确定性轨代表挤掉——保留资格必须先按"确定性轨 → 诊断轨"分配，再按各来源原序输出。"""
    required = parse_required_evidence("请引用 RagService 的生产实现。")
    protected = (
        AspectStatus(
            aspect_id="evaluator:CitationParser处理",
            label="CitationParser 处理",
            origin="evaluator",
            supported=True,
            first_supported_round=1,
        ),
    )

    # ① 两轨代表都在本轮新证据里，诊断轨排名更高
    plan = plan_retention(
        required, protected, _pairs((11, CITATION_PARSER), (12, RAG_SERVICE)), [], limit=1
    )
    assert plan.kept == (_uuid(12),)

    # ② 诊断轨代表是新证据、确定性轨代表是旧证据：旧的点名类同样不得被挤掉
    plan = plan_retention(
        required, protected, _pairs((11, CITATION_PARSER)), _pairs((1, RAG_SERVICE)), limit=1
    )
    assert plan.kept == (_uuid(1),)


def test_partial_binding_overlap_is_not_current_support() -> None:
    """三审发现1：绑定集合只剩"任意一条"不能证明直接支撑仍在。

    绑定 = [A.java（真正的支撑）, unrelated.md]，终态只剩 unrelated.md 时，
    present_now 必须为 False；同一文件的**不同 chunk** 也不算同一条证据。
    """
    required = parse_required_evidence("订单校验是怎么做的？")
    service = "backend/src/main/java/svc/OrderService.java"
    observations = observe_round(
        required,
        (),
        _refs((1, service), (2, ARCH_DOC)),
        ["订单校验"],
        round_index=1,
    )

    kept_all = build_matrix(required, observations, (), present_chunk_ids=[_uuid(1), _uuid(2)])
    assert kept_all[0].present_now is True  # 绑定集合完整保留 → 仍可交付

    leftover_only = build_matrix(required, observations, (), present_chunk_ids=[_uuid(2)])
    assert leftover_only[0].supported is True  # 单调下界不变
    assert leftover_only[0].present_now is False  # 但"还剩一条无关证据"不等于仍被支撑

    other_chunk = build_matrix(required, observations, (), present_chunk_ids=[_uuid(9), _uuid(2)])
    assert other_chunk[0].present_now is False  # 同文件不同 chunk 不是同一条证据


def test_diagnostic_support_survives_evaluator_flip_when_evidence_unchanged() -> None:
    """二审发现2：evaluator 方面的 present_now 不能用"本轮是否又说了一遍"代理——
    证据集没变时 LLM 不该能撤销历史支持（已裁决的诊断轨单调下界）。"""
    required = parse_required_evidence("订单校验是怎么做的？")
    service = "backend/src/main/java/svc/OrderService.java"
    observations = observe_round(
        required, (), _refs((1, service), (2, ARCH_DOC)), ["订单校验"], round_index=1
    )

    unchanged = build_matrix(required, observations, (), present_chunk_ids=[_uuid(1), _uuid(2)])
    assert unchanged[0].supported and unchanged[0].present_now  # 证据未变 → 仍可交付

    replaced = build_matrix(required, observations, (), present_chunk_ids=[_uuid(3)])
    assert replaced[0].supported and not replaced[0].present_now  # 支撑证据没了才算被挤出


def test_diagnostic_anchor_never_evicts_the_whole_refill_round() -> None:
    """诊断轨代表要给新证据让位：一条 LLM 自报标签不得把整轮补检结果清空。"""
    required = parse_required_evidence("非法引用编号是怎么处理的？")
    protected = (
        AspectStatus(
            aspect_id="evaluator:RagService分支",
            label="RagService 分支",
            origin="evaluator",
            supported=True,
            first_supported_round=1,
        ),
    )

    plan = plan_retention(
        required, protected, _pairs((11, CITATION_PARSER)), _pairs((1, RAG_SERVICE)), limit=1
    )

    assert plan.kept == (_uuid(11),)
    assert [record.reason for record in plan.eliminated] == ["aspect_anchor_capacity"]


@pytest.mark.parametrize(
    ("text", "absorbed"),
    [
        ("RagService 生产源码", True),  # 单目标 + 封闭通用限定词 → 交给确定性说明
        ("RagService", True),
        ("RagService 的生产实现", True),
        ("当前证据未覆盖 RagService 的生产源码", True),  # 缺失措辞不算语义残余
        ("缺少 RagService 生产源码", True),
        ("RagService 中 NO_ANSWER 的触发条件", False),  # 方法/行为语义必须保留（T23）
        ("RagService 和 UnknownService 生产源码", False),  # 第二目标不得被静默吞掉
        ("RagService.java 与 CitationParser 的实现", False),
        # 四审 P1：T22 从类型词也能解析出目标，它们不进 iter_target_spans，
        # 却恰好落在旧限定词表里 → 整条被吸收，测试/配置/迁移/设计缺口静默消失
        ("RagService 和测试", False),
        ("RagService 与配置", False),
        ("RagService 及迁移文件", False),
        ("RagService、设计文档", False),
        # 四审 P1：无连接词但类型不符——三态说明只覆盖生产源码，替代不了测试缺口
        ("RagService 的测试", False),
        ("RagService 的设计文档", False),
        ("当前证据未覆盖 RagService 的数据库迁移", False),
        # 探针发现：无分隔符拼接时 merge_spans 会把两个符号并成一段跨度，
        # 只数跨度看不到第二个目标 → 必须逐 token 核对身份
        ("RagService.javaOrderService 的实现", False),
    ],
)
def test_absorption_only_swallows_pure_identity_gaps(text: str, absorbed: bool) -> None:
    """三审发现2：吸收判据只数"匹配到几个 required id"，会把方法级缺口与未绑定的
    第二目标一起删掉——UnknownService 不是 required item，`len(bound)==1` 仍成立。"""
    required = parse_required_evidence("请引用 RagService 的生产实现。")
    items = [NotFoundInput(text=text, source="evaluator_missing")]

    kept, dropped = strip_items_contradicting_displaced_notes(items, required, frozenset({"R1"}))

    assert bool(dropped) is absorbed
    assert [item.text for item in kept] == ([] if absorbed else [text])


def test_absorption_accepts_qualifiers_of_the_bound_items_own_type() -> None:
    """类型相容是双向的：绑到测试项上的"测试"限定词仍该被三态说明吸收，
    否则修复会退化成"任何类型词都不吸收"，把 T24 二审发现3 的自相矛盾放回来。"""
    required = parse_required_evidence("请引用 CitationParserTest 的测试代码。")
    assert [item.type for item in required.items] == ["test"]
    items = [NotFoundInput(text="CitationParserTest 的测试", source="evaluator_missing")]

    kept, dropped = strip_items_contradicting_displaced_notes(items, required, frozenset({"R1"}))

    assert dropped == ["CitationParserTest 的测试"] and kept == []


def test_absorption_reads_the_full_gap_text_not_a_truncated_prefix() -> None:
    """四审 P1：安全判据不得读被截短的文本。缺口条目允许到 500 字，把方法级语义
    放在第 60 字之后就能让判据只看见开头的"生产源码"并错误吸收整条。"""
    padding = "（中之内里）" * 12  # 60+ 字纯结构噪音，本身不携带语义
    text = f"当前证据未覆盖 RagService 的生产源码{padding}触发 NO_ANSWER 的条件"
    assert len(text) > 60
    required = parse_required_evidence("请引用 RagService 的生产实现。")

    kept, dropped = strip_items_contradicting_displaced_notes(
        [NotFoundInput(text=text, source="evaluator_missing")], required, frozenset({"R1"})
    )

    assert dropped == [] and [item.text for item in kept] == [text]


def test_target_tokens_all_match_sees_through_merged_spans() -> None:
    """反向条件（没有别的目标）不能靠数跨度：相接跨度会被 merge_spans 合成一段。"""
    required = parse_required_evidence("请引用 RagService 的生产实现。")
    item = required.items[0]

    assert len(iter_target_spans("RagService.javaOrderService")) == 1  # 两个符号只剩一段跨度
    assert target_tokens_all_match(item, "RagService.javaOrderService") is False
    assert target_tokens_all_match(item, "RagService.java 的生产源码") is True
    assert target_tokens_all_match(item, "生产源码") is False  # 无目标 token → 无从绑定


def test_identity_qualifier_table_covers_every_evidence_type() -> None:
    # 防漂移：新增证据类型必须同时给出限定词表，否则吸收判据会 KeyError
    assert set(_TYPE_QUALIFIERS) == set(get_args(EvidenceType))


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
        _refs((1, RAG_SERVICE)),
        [],
        round_index=1,
    )
    matrix = build_matrix(
        required,
        observations,
        (CoverageEntry(item_id="R1", covered=False),),
        present_chunk_ids=[_uuid(9)],
    )

    assert matrix[0].supported is True and matrix[0].present_now is False
    assert supported_labels(matrix) == ["RagService"]
    assert supported_labels(matrix, origin="evaluator") == []


def test_fresh_anchor_survives_when_other_aspects_reserve_slots() -> None:
    """复审发现1：`anchored` 按全部 fresh 计算，但预留旧锚点又会缩短实际入选的 fresh
    前缀——落在前缀外的 fresh 锚点与同方面的旧锚点会被一起截掉，容量明明够。"""
    required = parse_required_evidence("请引用 RagService 和 CitationParser 的生产实现。")
    fresh = _pairs((11, OTHER_ONE), (12, OTHER_TWO), (13, RAG_SERVICE))
    carried = _pairs((1, RAG_SERVICE), (2, CITATION_PARSER))

    plan = plan_retention(required, (), fresh, carried, limit=3)

    # 容量 3 完全够：丢的应该是不锚定任何方面的 OtherTwo，而不是两条 RagService
    assert plan.kept == (_uuid(11), _uuid(13), _uuid(2))
    assert [record.reason for record in plan.eliminated if record.rel_path == RAG_SERVICE] == [
        "capacity_limit"  # 同方面已由本轮 fresh 锚定，旧的那条只是普通截断
    ]
    assert not [record for record in plan.eliminated if record.reason == "aspect_anchor_capacity"]
    # 本轮内相对顺序不变（只丢弃、不重排）
    assert plan.kept.index(_uuid(11)) < plan.kept.index(_uuid(13))


def test_history_alone_is_not_deliverable_when_current_evidence_lost_it() -> None:
    """复审发现2：'历史已支持' ≠ '当前可交付'——当前证据集已不再支撑任何方面时，
    不得因为历史账本非空就路由去 generate。"""
    required = parse_required_evidence("请引用 RagService 的生产实现。")
    state = initial_agent_state(_input("请引用 RagService 的生产实现。"))
    state["required_evidence"] = required
    state["retrieval_round"] = 2
    state["evidences"] = [
        Evidence(
            evidence_id="E1",
            chunk_id=_uuid(9),
            rel_path=ARCH_DOC,
            title_path="",
            content=_content(ARCH_DOC),
            start_line=1,
            end_line=9,
            score=0.4,
        )
    ]
    state["coverage"] = (CoverageEntry(item_id="R1", covered=False),)
    state["aspect_observations"] = observe_round(
        required,
        compute_coverage(required, [("E1", RAG_SERVICE)]),
        _refs((1, RAG_SERVICE)),
        [],
        round_index=1,
    )
    state["evaluation"] = EvaluateOutput(
        sufficiency="insufficient", supported_aspects=[], missing_aspects=["RagService 生产源码"]
    )

    assert has_deliverable_content(state) is False
    assert route_after_evaluate(state) == "finalize"


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


async def test_evaluator_flip_with_unchanged_evidence_still_delivers_partial() -> None:
    """二审发现2 的端到端版：证据集一模一样，末轮 evaluate 却撤回自报支持并判 insufficient。

    诊断轨的单调下界必须扛住——证据没变，LLM 不能靠"这轮不提了"把已支持方面清空、
    把整个 run 推进 refusal。
    """
    order_service = "backend/src/main/java/svc/OrderService.java"
    eval1 = (
        '{"sufficiency":"partial","supported_aspects":["订单校验"],"missing_aspects":["库存扣减"]}'
    )
    eval2 = '{"sufficiency":"insufficient","supported_aspects":[],"missing_aspects":["订单校验"]}'
    gen = (
        '{"answer_text":"订单校验由 owner 过滤保护 [E1]。",'
        '"claims":[{"text":"订单校验由 owner 过滤保护","evidence_ids":["E1"],'
        f'"quotes":["{_content(order_service)}"]}}],"not_found":[]}}'
    )
    runtime = _rounds_runtime([PLAN, eval1, REFINE, eval2, gen], [[order_service]], max_evidences=3)

    result = await run_agent(runtime, _input("订单校验是怎么做的？"))

    assert {evidence.rel_path for evidence in result["evidences"]} == {order_service}
    assert result["final_mode"] == "partial"
    assert [claim.text for claim in result["final_claims"]] == ["订单校验由 owner 过滤保护"]
    assert "订单校验" not in result["final_not_found"]


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


async def test_displaced_aspect_is_never_worded_as_never_obtained() -> None:
    """复审发现2 的第二半：同一方面不得既告警"被挤出、非证伪"、又在 not_found 里
    被写成"未取得"。三态必须分开：从未取得 / 仍在证据集但未引用 / 前轮取得后被挤出。"""
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, EVAL_ROUND2_REGRESSED, GEN_PARTIAL],
        [[RAG_SERVICE], [CITATION_PARSER]],
        max_evidences=1,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION))

    assert any("被挤出，非证伪" in warning for warning in result["warnings"])
    assert not any(
        "未取得用户要求的必需证据" in item and "RagService" in item
        for item in result["final_not_found"]
    )
    # 第三态有自己的措辞，且仍如实说明"本次无法作为引用支撑"
    assert any(
        "RagService" in item and "被挤出" in item and "无法作为引用支撑" in item
        for item in result["final_not_found"]
    )
    # 从未取得的 RagSupport 仍按第一态措辞
    assert any(
        "未取得用户要求的必需证据" in item and "RagSupport" in item
        for item in result["final_not_found"]
    )
    assert result["final_mode"] == "partial"


async def test_evaluator_missing_bound_to_required_item_yields_one_statement() -> None:
    """二审发现3：末轮把已被挤出的 RagService 报成"RagService 生产源码"时，终态会同时
    出现原始缺失项与三态说明——限定词一变，归一化全等比较就失效。能由 T22 matcher
    唯一绑定到某条 required item 的缺失项，交给确定性三态说明，不再保留原文。"""
    eval2_names_ragservice = (
        '{"sufficiency":"insufficient","supported_aspects":["非法引用编号处理"],'
        '"missing_aspects":["RagService 生产源码","RagSupport 生产源码"]}'
    )
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, eval2_names_ragservice, GEN_PARTIAL],
        [[RAG_SERVICE], [CITATION_PARSER]],
        max_evidences=1,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION))

    # 被挤出的 RagService：原始缺失项被吸收，只留"取得过但被挤出"一句
    assert not any(item.strip() == "RagService 生产源码" for item in result["final_not_found"])
    displaced = [item for item in result["final_not_found"] if "RagService" in item]
    assert len(displaced) == 1 and "被挤出" in displaced[0]
    assert any("缺失项已由确定性说明覆盖" in warning for warning in result["warnings"])
    # 从未取得的 RagSupport 不属于矛盾态：LLM 原文继续走 T23 事实校验，不被吞掉
    # （吞掉会连方法级细节与 corpus_index 事实来源一起丢，c10 要求两类缺口可分辨）
    assert any(
        "未取得用户要求的必需证据" in item and "RagSupport" in item
        for item in result["final_not_found"]
    )


async def test_elimination_records_are_replayable_beyond_summary_clip() -> None:
    """二审发现4：单轮账本可达 24 条，step summary 却只落前 8 条 → 回放看不全。"""
    recorder = TraceRecorder()
    extra = [f"docs/filler-{index}.md" for index in range(12)]
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, EVAL_ROUND2_REGRESSED, GEN_PARTIAL],
        [[RAG_SERVICE, *extra], [CITATION_PARSER, *extra]],
        max_evidences=1,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION), recorder)

    first_round = next(step for step in recorder.steps if step.node == "retrieve")
    summary = first_round.output_summary
    assert summary is not None
    # 首轮 13 条候选、容量 1 → 12 条淘汰；落盘摘要按账本上限保留，
    # 不得被 MAX_SUMMARY_ITEMS 截成 8 条（否则回放看不全）
    assert len(summary["eliminated"]) == 12
    assert {item["rel_path"] for item in summary["eliminated"]} == set(extra)
    assert len(result["evidence_eliminations"]) >= 12


async def test_elimination_records_are_auditable_per_candidate() -> None:
    """复审发现3：逐条淘汰原因必须能在运行结束后审计，不能只剩一条汇总 warning。"""
    recorder = TraceRecorder()
    runtime = _rounds_runtime(
        [PLAN, EVAL_ROUND1, REFINE, EVAL_ROUND2_REGRESSED, GEN_PARTIAL],
        [
            [RAG_SERVICE, RAG_CONSTANTS, PARSER_TEST],
            [CITATION_PARSER, PARSE_RESULT, ARCH_DOC],
        ],
        max_evidences=3,
    )
    result = await run_agent(runtime, _input(CASE9_QUESTION), recorder)

    records = {record.rel_path: record for record in result["evidence_eliminations"]}
    assert set(records) == {PARSE_RESULT, ARCH_DOC, PARSER_TEST}
    assert all(record.reason == "capacity_limit" for record in records.values())
    assert all(record.chunk_id for record in records.values())
    # 轨迹里同样可逐条回放（chunk_id + reason），不止汇总条数
    retrieve_steps = [step for step in recorder.steps if step.node == "retrieve"]
    summary = retrieve_steps[-1].output_summary
    assert summary is not None
    eliminated = summary["eliminated"]
    assert {item["rel_path"] for item in eliminated} == {PARSE_RESULT, ARCH_DOC, PARSER_TEST}
    assert all({"chunk_id", "reason", "aspect_ids"} <= set(item) for item in eliminated)
