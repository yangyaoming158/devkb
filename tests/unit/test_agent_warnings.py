"""T30.2 warning 归属与时态划分的规则层单测（RT-10 / m14 断言 4）。

合同：`docs/tasks/T30.2-warning-attribution.md`（冻结测试 U1–U11，2026-08-01 `PLAN_APPROVED`）。
本文件只测纯函数与模型，不起图；图级归属见 `test_agent_graph.py` 的 G1–G11。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from devkb.agent.warnings import (
    VERIFY_FAILED_CODE,
    WarningDetail,
    WarningRecord,
    attribute,
    classify_warnings,
)

# G8 实测形状（合同「基线复现」）：finalize 的 warning 逐字插值 LLM 自由文本，
# 内含 ":request_failed:" 却与 _structured_call 无关。
COLLIDING_FINALIZE_CODE = (
    "finalize: 前轮已支持方面被后轮报为缺失，已剔除（库存:request_failed:LLMTimeoutError）"
)
# PG-T302-02-R 实测形状：合法 run 产出的超长 warning（不得被任何长度约束拦下）
OVERLONG_CODE = "finalize: 前轮已支持方面被后轮报为缺失，已剔除（" + "库" * 500 + "）"
HUGE_CODE = "finalize: 前轮已支持方面被后轮报为缺失，已剔除（" + "库" * 5000 + "）"


def _record(code: str, node: str, attempt: int = 1) -> WarningRecord:
    return WarningRecord(code=code, node=node, attempt=attempt)  # type: ignore[arg-type]


def _classify(records: list[WarningRecord], *, verify_runs: int = 0):
    return classify_warnings([record.code for record in records], records, verify_runs=verify_runs)


# ---- U1/U8：R1（verify 取代） -------------------------------------------------


def test_u1_verify_failure_before_the_last_attempt_is_superseded() -> None:
    ledger = _classify([_record(VERIFY_FAILED_CODE, "verify", 1)], verify_runs=2)

    assert ledger.resolved == (VERIFY_FAILED_CODE,)
    assert ledger.active == ()
    (detail,) = ledger.details
    assert detail.status == "resolved"
    assert detail.resolution == "superseded_by_regeneration"
    assert (detail.node, detail.attempt) == ("verify", 1)


def test_u1b_single_verify_run_keeps_the_failure_active() -> None:
    """只跑过一次 verify：没有"后一稿"，该失败描述的就是终态交付物。"""
    ledger = _classify([_record(VERIFY_FAILED_CODE, "verify", 1)], verify_runs=1)

    assert ledger.active == (VERIFY_FAILED_CODE,)
    assert ledger.resolved == ()
    assert ledger.details[0].resolution is None


def test_u8_last_verify_attempt_is_never_resolved() -> None:
    """U8（fail-closed）：attempt == verify_runs 恒 active——两稿都失败时第 2 条不得被藏。

    另断言"存在 finalize 降级说明"不改变该判定（案例五/十三形状）。
    """
    records = [
        _record(VERIFY_FAILED_CODE, "verify", 1),
        _record(VERIFY_FAILED_CODE, "verify", 2),
        _record("finalize: 已移除 1 个未通过验证的 claim，正文按保留 claim 重建", "finalize", 1),
    ]
    ledger = _classify(records, verify_runs=2)

    assert [detail.status for detail in ledger.details] == ["resolved", "active", "active"]
    assert ledger.details[1].attempt == 2 and ledger.details[1].resolution is None
    # 两条 code 逐字节相同，只有 attempt 能区分（RT-10 案例十三的原始缺陷）
    assert ledger.details[0].code == ledger.details[1].code


def test_u1c_verify_rule_does_not_apply_to_other_codes_or_nodes() -> None:
    """R1 三项合取：node、code、attempt<verify_runs 缺一不可。"""
    other_code = _classify([_record("verify:no_valid_draft", "verify", 1)], verify_runs=2)
    other_node = _classify([_record(VERIFY_FAILED_CODE, "finalize", 1)], verify_runs=2)

    assert other_code.active == ("verify:no_valid_draft",)
    assert other_node.active == (VERIFY_FAILED_CODE,)


# ---- U2/U9/U10/U11：R2（重问成功）与其 fail-closed 负例 ------------------------


@pytest.mark.parametrize(
    ("code", "node"),
    [
        ("plan:invalid_structured_output", "plan"),
        ("refine:invalid_structured_output", "refine"),
        ("evaluate:1:invalid_structured_output", "evaluate"),
        ("evaluate:2:invalid_structured_output", "evaluate"),
        ("generate:1:invalid_structured_output", "generate"),
        ("plan:request_failed:LLMTimeoutError", "plan"),
        ("generate:2:request_failed:LLMError", "generate"),
    ],
)
def test_u2_canonical_reask_failure_without_default_applied_is_resolved(
    code: str, node: str
) -> None:
    ledger = _classify([_record(code, node)])

    assert ledger.resolved == (code,)
    assert ledger.details[0].resolution == "reask_succeeded"


def test_u2b_same_call_key_default_applied_keeps_the_whole_group_active() -> None:
    records = [
        _record("plan:invalid_structured_output", "plan"),
        _record("plan:invalid_structured_output", "plan"),
        _record("plan:default_applied", "plan"),
    ]
    ledger = _classify(records)

    assert ledger.resolved == ()
    assert len(ledger.active) == 3
    assert all(detail.resolution is None for detail in ledger.details)


def test_u2c_default_applied_of_a_different_call_key_does_not_shield() -> None:
    """否决按 call_key 精确匹配：generate:2 的降级不能让 generate:1 的失败留 active，
    反之亦然——两次 generate 是两个节点执行，attempt 已不同，这里再锁 call_key 维度。"""
    records = [
        _record("generate:1:invalid_structured_output", "generate", 1),
        _record("generate:2:invalid_structured_output", "generate", 2),
        _record("generate:2:default_applied", "generate", 2),
    ]
    ledger = _classify(records)

    assert ledger.resolved == ("generate:1:invalid_structured_output",)
    assert ledger.active == (
        "generate:2:invalid_structured_output",
        "generate:2:default_applied",
    )


@pytest.mark.parametrize(
    "code",
    [
        COLLIDING_FINALIZE_CODE,  # G8：finalize 插值 LLM 文本导致的碰撞
        "finalize:x:request_failed:LLMError",  # 前缀不是合法 call_key
        "plan-x:invalid_structured_output",  # 前缀被污染
        "plan:invalid_structured_output:extra",  # 尾部有残余
        " plan:invalid_structured_output",  # 前导空格
        "plan:invalid_structured_outputX",  # 尾部粘连
        "generate:0:invalid_structured_output",  # 0 不是合法 call 序号
        "generate:01:invalid_structured_output",  # 前导零不合法
        "evaluate:invalid_structured_output",  # evaluate 必须带轮次
        "plan:request_failed:",  # 异常类名为空
        "plan:request_failed:Foo Bar",  # 异常类名含空格
        "plan:request_failed:类型",  # 异常类名非 ASCII 标识符
    ],
)
def test_u9_non_canonical_code_syntax_stays_active(code: str) -> None:
    """U9：正则 ^…$ 完整锚定——不用 in/endswith，"看起来像"不算数。"""
    node = "finalize" if code.startswith("finalize") else "plan"
    ledger = _classify([_record(code, node)])

    assert ledger.active == (code,)
    assert ledger.resolved == ()
    assert ledger.details[0].resolution is None


@pytest.mark.parametrize(
    ("code", "node"),
    [
        ("generate:1:invalid_structured_output", "finalize"),
        ("generate:1:invalid_structured_output", "verify"),
        ("generate:1:invalid_structured_output", "plan"),
        ("plan:invalid_structured_output", "evaluate"),
        ("plan:request_failed:LLMError", "retrieve"),
        ("evaluate:1:invalid_structured_output", "policy_refuse"),
    ],
)
def test_u10_call_key_prefix_must_match_the_recording_node(code: str, node: str) -> None:
    """U10：语法合法但归属节点对不上 → active（R2 ①③ 否决）。"""
    ledger = _classify([_record(code, node)])

    assert ledger.active == (code,)
    assert ledger.details[0].resolution is None


@pytest.mark.parametrize(
    "code",
    ["plan:budget_exhausted", "plan:default_applied", "generate:1:budget_exhausted"],
)
def test_u11_budget_and_default_markers_are_always_active(code: str) -> None:
    ledger = _classify([_record(code, code.split(":", 1)[0])])

    assert ledger.active == (code,)
    assert ledger.resolved == ()


# ---- U3/U4/U5：边界与 fail-closed --------------------------------------------


def test_u3_empty_ledger_yields_three_empty_fields() -> None:
    ledger = classify_warnings([], [], verify_runs=0)

    assert (ledger.active, ledger.resolved, ledger.details) == ((), (), ())


@pytest.mark.parametrize(
    "code",
    [
        "retrieve:round_limit_reached",
        "finalize: 已移除 1 个未通过验证的 claim，正文按保留 claim 重建",
        "",  # 空串：fail-closed 不得崩在 ValidationError 上（PG-T302-02-R）
        OVERLONG_CODE,  # 实测 530 字符
        HUGE_CODE,  # 10 项 not_found 的量级
    ],
)
@pytest.mark.parametrize("verify_runs", [0, 1, 2])
def test_u4_unrecognized_codes_stay_active_verbatim(code: str, verify_runs: int) -> None:
    """U4：未识别 code 恒 active；空串与超长输入都不得抛异常，原文逐字节保留。"""
    ledger = _classify([_record(code, "finalize")], verify_runs=verify_runs)

    assert ledger.active == (code,)
    assert ledger.resolved == ()
    assert ledger.details[0].code == code  # 逐字节，无截断
    assert ledger.details[0].resolution is None


def test_u4b_models_accept_empty_and_overlong_codes_without_constraints() -> None:
    """`code` 是既有 list[str] 通道的原文转载：加任何非空/长度约束都会打断合法 run。"""
    for code in ("", OVERLONG_CODE, HUGE_CODE):
        assert WarningRecord(code=code, node="finalize", attempt=1).code == code
        assert WarningDetail(code=code, status="active").code == code


@pytest.mark.parametrize(
    ("codes", "records"),
    [
        (["a", "b"], [_record("a", "plan")]),  # 记录少于 codes
        (["a"], [_record("a", "plan"), _record("b", "plan")]),  # 记录多于 codes
        (["a", "b"], [_record("b", "plan"), _record("a", "plan")]),  # 逐位不等（顺序错位）
        ([VERIFY_FAILED_CODE], [_record("other", "verify")]),  # 逐位不等（内容不符）
    ],
)
def test_u5_misaligned_ledger_falls_back_to_all_active_without_attribution(
    codes: list[str], records: list[WarningRecord]
) -> None:
    """U5：账本对不齐时绝不猜归属、绝不隐藏条目（绕过图构造 state 的调用方）。"""
    ledger = classify_warnings(codes, records, verify_runs=2)

    assert list(ledger.active) == codes  # 一条不少、保序
    assert ledger.resolved == ()
    assert [detail.code for detail in ledger.details] == codes
    assert all(detail.node is None and detail.attempt is None for detail in ledger.details)
    assert all(detail.status == "active" for detail in ledger.details)


# ---- U6：投影式不变量 --------------------------------------------------------


def test_u6_three_fields_are_order_preserving_projections_of_one_ledger() -> None:
    """U6（PG-T302-03）：details 是唯一权威账本，另两个字段是它的保序投影。

    构造刻意让状态**交错**且含**重复 code**——"拼接=划分"与"计数相等"都过不了这一关。
    """
    duplicate = "generate:1:request_failed:LLMError"
    records = [
        _record(VERIFY_FAILED_CODE, "verify", 1),  # resolved
        _record(duplicate, "generate", 1),  # active（同组有 default_applied）
        _record(duplicate, "generate", 1),  # active，与上条逐字节相同
        _record("generate:1:default_applied", "generate", 1),  # active
        _record("refine:invalid_structured_output", "refine", 1),  # resolved
        _record(VERIFY_FAILED_CODE, "verify", 2),  # active
    ]
    codes = [record.code for record in records]
    ledger = classify_warnings(codes, records, verify_runs=2)

    assert [detail.code for detail in ledger.details] == codes
    assert list(ledger.active) == [d.code for d in ledger.details if d.status == "active"]
    assert list(ledger.resolved) == [d.code for d in ledger.details if d.status == "resolved"]
    assert len(ledger.active) + len(ledger.resolved) == len(ledger.details)
    # 交错见证：直接拼接无法还原全局序 → 旧「保序划分」表述在此处即失效
    assert [d.status for d in ledger.details] == [
        "resolved",
        "active",
        "active",
        "active",
        "resolved",
        "active",
    ]
    # 重复 code 不去重、不合并
    assert ledger.active.count(duplicate) == 2


def test_u6b_attribute_pairs_codes_with_node_and_attempt_in_order() -> None:
    codes = ["a", "b", "a"]
    records = attribute("evaluate", 2, codes)

    assert [record.code for record in records] == codes
    assert all(record.node == "evaluate" and record.attempt == 2 for record in records)
    assert attribute("plan", 1, []) == []


# ---- U7：非法状态在类型层不可表达 --------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "c", "status": "active", "resolution": "reask_succeeded"},
        {
            "code": "c",
            "status": "active",
            "node": "plan",
            "attempt": 1,
            "resolution": "reask_succeeded",
        },
        {"code": "c", "status": "resolved", "node": "plan", "attempt": 1},
        {"code": "c", "status": "resolved", "node": "plan", "attempt": 1, "resolution": "nope"},
        {"code": "c", "status": "active", "node": "plan", "attempt": 0},
        {"code": "c", "status": "active", "node": "plan"},
        {"code": "c", "status": "active", "attempt": 1},
        {"code": "c", "status": "resolved", "resolution": "reask_succeeded"},
        {"code": "c", "status": "unknown", "node": "plan", "attempt": 1},
        {"code": "c", "status": "active", "node": "nowhere", "attempt": 1},
        {"code": "c", "status": "active", "extra": 1},
    ],
)
def test_u7_illegal_warning_detail_combinations_are_unrepresentable(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WarningDetail(**kwargs)  # type: ignore[arg-type]


def test_u7b_records_and_details_are_frozen() -> None:
    detail = WarningDetail(code="c", status="active")
    record = WarningRecord(code="c", node="plan", attempt=1)

    with pytest.raises(ValidationError):
        detail.status = "resolved"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.attempt = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        WarningRecord(code="c", node="plan", attempt=0)
