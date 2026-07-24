"""T22.2 逐项 full 硬约束（图级）：点名类必须由匹配的直接引用覆盖才允许 full。

对抗复审发现1：点名 3 个类却引用无关生产文件曾错误判 full；此处锁定为 partial。
"""

from __future__ import annotations

import uuid

from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime
from devkb.agent.state import AgentInput, Evidence
from devkb.llm import FakeLLM

PLAN = '{"intent":"knowledge_qa","queries":["订单校验"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["订单校验"],"missing_aspects":[]}'
REFINE = '{"queries":["OrderService 生产源码"]}'
GEN = (
    '{"answer_text":"订单校验由 owner 过滤保护 [E1]。",'
    '"claims":[{"text":"订单校验由 owner 过滤保护",'
    '"evidence_ids":["E1"],"quotes":["订单校验由 owner 过滤保护。"]}],"not_found":[]}'
)
_CONTENT = "订单校验由 owner 过滤保护。"
_NAMED_Q = "请引用 OrderService 的生产实现说明订单校验。"


def _runtime(script: list[str | Exception], rel_path: str) -> AgentRuntime:
    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return [
            Evidence(
                evidence_id="E1",
                chunk_id=uuid.UUID(int=1),
                rel_path=rel_path,
                title_path="",
                content=_CONTENT,
                start_line=1,
                end_line=3,
                score=0.5,
            )
        ]

    return AgentRuntime(llm=FakeLLM(script), retriever=retriever)


def _input(question: str) -> AgentInput:
    return AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question=question)


async def test_named_class_wrong_file_downgrades_to_partial() -> None:
    # 覆盖缺口先驱动一次补检（发现4）；补检仍只召回无关文件 → 诚实降级 partial
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, REFINE, EVAL_OK, GEN], "backend/src/main/java/other/Other.java"),
        _input(_NAMED_Q),
    )
    assert [item.symbol for item in result["required_evidence"].items] == ["OrderService"]
    assert "refine" in result["node_history"]
    # 点名 OrderService 却引用无关生产文件：逐项覆盖失败，不得 full
    assert result["final_mode"] == "partial"
    assert any("必需证据未覆盖" in w for w in result["warnings"])


async def test_named_class_exact_file_allows_full() -> None:
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, GEN], "backend/src/main/java/svc/OrderService.java"),
        _input(_NAMED_Q),
    )
    assert result["final_mode"] == "full"
    assert result["final_not_found"] == []


async def test_named_class_test_file_does_not_cover() -> None:
    # OrderServiceTest.java 是 test 类型，不能覆盖 production_source 的点名项
    result = await run_agent(
        _runtime(
            [PLAN, EVAL_OK, REFINE, EVAL_OK, GEN],
            "backend/src/test/java/svc/OrderServiceTest.java",
        ),
        _input(_NAMED_Q),
    )
    assert result["final_mode"] == "partial"


async def test_no_requirement_keeps_full_with_any_evidence() -> None:
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, GEN], "backend/src/test/java/FooTest.java"),
        _input("订单校验是怎么做的？"),
    )
    assert result["required_evidence"].items == ()
    assert result["final_mode"] == "full"


async def test_plan_echo_unknown_item_id_warns() -> None:
    plan_forged = '{"intent":"knowledge_qa","queries":["订单校验"],"required_evidence":["R99"]}'
    result = await run_agent(
        _runtime([plan_forged, EVAL_OK, GEN], "backend/src/main/java/svc/OrderService.java"),
        _input(_NAMED_Q),
    )
    # LLM 回显非权威 item_id → 警告并忽略；确定性裁决不受影响
    assert any("required_evidence_mismatch" in w for w in result["warnings"])
    assert result["final_mode"] == "full"


async def test_uncovered_required_forces_refine_even_when_llm_says_sufficient() -> None:
    # 发现4：即便 LLM 判 sufficient，确定性覆盖缺口也必须先补检，不能直奔 generate
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, REFINE, EVAL_OK, GEN], "backend/src/main/java/other/Other.java"),
        _input(_NAMED_Q),
    )
    assert result["node_history"].index("refine") < result["node_history"].index("generate")


async def test_llm_omitting_required_reports_warns_but_deterministic_full_stands() -> None:
    # 发现4：plan 漏回显、evaluate 漏 coverage（当前 Fake 即如此）都记诊断 mismatch；
    # 但确定性覆盖成立时 full 不受影响（LLM 报告不能改判）。
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, GEN], "backend/src/main/java/svc/OrderService.java"),
        _input(_NAMED_Q),
    )
    assert any("plan:required_evidence_mismatch" in w for w in result["warnings"])
    assert any("evaluate:coverage_mismatch" in w for w in result["warnings"])
    assert result["final_mode"] == "full"


async def test_negation_period_combination_does_not_full_on_test_only() -> None:
    # 三审发现1（图级）：否定跨句 + 显式路径组合，只引测试文件不得 full
    question = "不要引用测试. 请引用 backend/src/main/java/foo/OrderService.java."
    result = await run_agent(
        _runtime(
            [PLAN, EVAL_OK, REFINE, EVAL_OK, GEN],
            "backend/src/test/java/foo/OrderServiceTest.java",
        ),
        _input(question),
    )
    assert result["final_mode"] == "partial"


async def test_evaluate_coverage_evidence_omission_is_mismatch() -> None:
    # 三审发现5：LLM 报 covered=true 但漏报命中的 evidence_id → 记诊断 mismatch
    eval_cov_empty = (
        '{"sufficiency":"sufficient","supported_aspects":["订单校验"],'
        '"missing_aspects":[],"coverage":[{"item_id":"R1","covered":true,"evidence_ids":[]}]}'
    )
    result = await run_agent(
        _runtime([PLAN, eval_cov_empty, GEN], "backend/src/main/java/svc/OrderService.java"),
        _input(_NAMED_Q),
    )
    assert any("evaluate:coverage_mismatch" in w for w in result["warnings"])
    assert result["final_mode"] == "full"  # 确定性覆盖成立，诊断不改判
