"""T22.2 full 硬约束：用户指定的必需证据类型未被同类型引用覆盖时不得判 full。"""

from __future__ import annotations

import uuid

from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime
from devkb.agent.state import AgentInput, Evidence
from devkb.llm import FakeLLM

PLAN = '{"intent":"knowledge_qa","queries":["订单校验"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["订单校验"],"missing_aspects":[]}'
GEN_OWNER = (
    '{"answer_text":"订单校验由 owner 过滤保护 [E1]。",'
    '"claims":[{"text":"订单校验由 owner 过滤保护",'
    '"evidence_ids":["E1"],"quotes":["订单校验由 owner 过滤保护。"]}],"not_found":[]}'
)
_CONTENT = "订单校验由 owner 过滤保护。"
_QUESTION = "请引用生产 Java 源码说明订单校验。"


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


def _input(question: str = _QUESTION) -> AgentInput:
    return AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question=question)


async def test_production_required_but_only_test_evidence_downgrades_to_partial() -> None:
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, GEN_OWNER], "backend/src/test/java/FooTest.java"), _input()
    )
    assert "production_source" in result["required_evidence"].required_types
    # 仅测试证据不能满足"必需生产源码"：即使 evaluate 充分、引用通过，也降级 partial
    assert result["final_mode"] == "partial"
    assert any("必需证据类型未满足" in w for w in result["warnings"])
    assert any("生产源码" in note for note in result["final_not_found"])


async def test_production_required_and_production_evidence_allows_full() -> None:
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, GEN_OWNER], "backend/src/main/java/Foo.java"), _input()
    )
    assert result["final_mode"] == "full"
    assert result["final_not_found"] == []


async def test_no_required_evidence_keeps_p1_full_behavior() -> None:
    # 自然问法无证据类型要求 → 空约束，不因证据来自 test 路径而降级（不劣于 P1）
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, GEN_OWNER], "backend/src/test/java/FooTest.java"),
        _input("订单校验是怎么做的？"),
    )
    assert result["required_evidence"].required_types == ()
    assert result["final_mode"] == "full"


async def test_plan_echo_unanchored_required_evidence_warns() -> None:
    plan_forged = (
        '{"intent":"knowledge_qa","queries":["订单校验"],"required_evidence":["伪造的证据要求"]}'
    )
    result = await run_agent(
        _runtime([plan_forged, EVAL_OK, GEN_OWNER], "backend/src/main/java/Foo.java"), _input()
    )
    # LLM 回显问题中不存在的 required_evidence → 警告并忽略；权威来源仍是确定性解析
    assert any("required_evidence_unanchored" in w for w in result["warnings"])
    assert "production_source" in result["required_evidence"].required_types
