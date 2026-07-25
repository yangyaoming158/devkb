"""T22.2 逐项 full 硬约束（图级）：点名类必须由匹配的直接引用覆盖才允许 full。

对抗复审发现1：点名 3 个类却引用无关生产文件曾错误判 full；此处锁定为 partial。
"""

from __future__ import annotations

import uuid

from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime
from devkb.agent.state import AgentInput, AgentState, Evidence
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


# ---- 第四轮复审 §五.A：图级错误 full 反例（结构性 fail-closed） ----

# 每条：确定性覆盖缺口/unresolved 必须驱动一次补检，最终不得 full。补检轮脚本
# plan→evaluate→refine→evaluate→generate。
_SCRIPT5: list[str | Exception] = [PLAN, EVAL_OK, REFINE, EVAL_OK, GEN]


async def _run5(question: str, cited_rel_path: str) -> AgentState:
    return await run_agent(_runtime(_SCRIPT5, cited_rel_path), _input(question))


async def test_gA1_negation_period_no_space_test_only_not_full() -> None:
    result = await _run5("不要引用测试.请引用Foo.java。", "backend/src/test/java/foo/FooTest.java")
    # 解析必须是干净的 Foo.java，而非把"请引用Foo.java"整段吞成路径
    assert "Foo.java" in {i.path for i in result["required_evidence"].items if i.path}
    assert result["final_mode"] == "partial"


async def test_gA2_comma_enumeration_partial_citation_not_full() -> None:
    result = await _run5("请引用 Foo.java，Bar.java。", "backend/src/main/java/foo/Foo.java")
    assert "refine" in result["node_history"]
    assert result["final_mode"] == "partial"


async def test_gA3_named_enumeration_partial_citation_not_full() -> None:
    result = await _run5(
        "请引用 OrderService，CitationParser 的生产实现。",
        "backend/src/main/java/svc/OrderService.java",
    )
    assert "refine" in result["node_history"]
    assert result["final_mode"] == "partial"


async def test_gA4_single_word_class_wrong_file_not_full() -> None:
    result = await _run5("请引用 Order 类的生产实现。", "backend/src/main/java/foo/Foo.java")
    assert "refine" in result["node_history"]
    assert result["final_mode"] == "partial"


async def test_gA5_txt_path_required_test_evidence_not_full() -> None:
    result = await _run5("请引用 docs/notes.txt。", "backend/src/test/java/FooTest.java")
    assert "refine" in result["node_history"]
    assert result["final_mode"] == "partial"


async def test_gA6_test_required_but_production_cited_not_full() -> None:
    # "对应测试" → test 必需；只引生产 Contest.java（大小写不得伪命中 test）→ 不 full
    result = await _run5("请引用对应测试。", "backend/src/main/java/Contest.java")
    assert "refine" in result["node_history"]
    assert result["final_mode"] == "partial"


async def test_gA7_case_mismatch_not_full() -> None:
    result = await _run5("请引用 Foo.java。", "backend/src/main/java/foo/foo.java")
    assert "refine" in result["node_history"]  # 大小写敏感 → 覆盖缺口 → 补检
    assert result["final_mode"] == "partial"


async def test_gA8_resolved_plus_unresolved_target_not_full() -> None:
    # Foo.java 已引用，但"某个关键实现"未解析 → unresolved → 缺口 → 补检 → 最高 partial
    result = await _run5("请引用 Foo.java 和某个关键实现。", "backend/src/main/java/foo/Foo.java")
    assert "refine" in result["node_history"]
    assert result["final_mode"] == "partial"


# ---- 第五轮复审 §五.B：约束跨度账本的图级错误 full 反例 ----


def _runtime_multi(script: list[str | Exception], rel_paths: list[str]) -> AgentRuntime:
    async def retriever(_project_id: uuid.UUID, _queries: tuple[str, ...]) -> list[Evidence]:
        return [
            Evidence(
                evidence_id=f"E{index}",
                chunk_id=uuid.UUID(int=index),
                rel_path=rel_path,
                title_path="",
                content=_CONTENT,
                start_line=1,
                end_line=3,
                score=0.5,
            )
            for index, rel_path in enumerate(rel_paths, start=1)
        ]

    return AgentRuntime(llm=FakeLLM(script), retriever=retriever)


def _gen_citing(*evidence_ids: str) -> str:
    marks = "".join(f"[{eid}]" for eid in evidence_ids)
    ids = ",".join(f'"{eid}"' for eid in evidence_ids)
    return (
        f'{{"answer_text":"订单校验由 owner 过滤保护 {marks}。",'
        '"claims":[{"text":"订单校验由 owner 过滤保护",'
        f'"evidence_ids":[{ids}],'
        f'"quotes":["{_CONTENT}"]}}],"not_found":[]}}'
    )


async def test_gB1_partial_parse_leftover_target_blocks_full() -> None:
    # 五审发现1：引用了 Foo.java 但"关键实现"未解析 → 不得 full，且须给出确定性限制说明
    result = await _run5("请引用 Foo.java 和关键实现。", "backend/src/main/java/foo/Foo.java")
    assert [uc.anchor for uc in result["required_evidence"].unresolved_constraints] == ["关键实现"]
    assert result["final_mode"] == "partial"
    assert any("无法确定性定位" in item for item in result["final_not_found"])


async def test_gB2_vague_config_target_blocks_full_even_with_three_exact_citations() -> None:
    # 五审发现1（c08）：README/docker-compose.yml/application.yml 全部直接引用，
    # 但"相关 Java 配置"未解析 → 仍不得 full
    question = (
        "请区分本地裸 JVM 与 Docker Compose 的生效范围，"
        "并引用 README、docker-compose.yml、application.yml 和相关 Java 配置。"
    )
    paths = ["README.md", "docker-compose.yml", "backend/src/main/resources/application.yml"]
    result = await run_agent(
        _runtime_multi(
            [PLAN, EVAL_OK, REFINE, EVAL_OK, _gen_citing("E1", "E2", "E3")],
            paths,
        ),
        _input(question),
    )
    assert result["final_mode"] == "partial"
    assert any("相关 Java 配置" in item for item in result["final_not_found"])


async def test_gB3_verification_style_question_cannot_be_full_on_arbitrary_evidence() -> None:
    # 五审发现1（c02）：整题曾解析为 none → 任意证据即 full；现在必须 ambiguous
    question = (
        "README 声称默认 Mock Provider 不配置模型 key 也能运行。"
        "Java 配置和 Provider 实现是否支持这个说法？"
    )
    result = await _run5(question, "backend/src/main/java/other/Other.java")
    assert result["required_evidence"].status == "ambiguous"
    assert result["final_mode"] == "partial"


async def test_gB4_named_readme_not_satisfied_by_other_current_doc() -> None:
    # 五审发现2：点名 README 必须带身份（symbol），不得退化为任意 current_doc 都能满足
    result = await _run5("请引用 README。", "docs/architecture.md")
    assert [(i.type, i.symbol) for i in result["required_evidence"].items] == [
        ("current_doc", "README")
    ]
    assert not all(entry.covered for entry in result["coverage"])
    assert result["final_mode"] == "partial"


async def test_gB5_named_test_symbol_with_production_context_not_full() -> None:
    # 五审发现2：src/test/OrderTest.java + 任意生产文件不得满足"OrderTest 的生产实现"
    result = await run_agent(
        _runtime_multi(
            [PLAN, EVAL_OK, REFINE, EVAL_OK, _gen_citing("E1", "E2")],
            ["backend/src/test/java/OrderTest.java", "backend/src/main/java/Foo.java"],
        ),
        _input("请引用 OrderTest 的生产实现。"),
    )
    assert [(i.type, i.symbol) for i in result["required_evidence"].items] == [
        ("production_source", "OrderTest")
    ]
    assert not all(entry.covered for entry in result["coverage"])
    assert result["final_mode"] == "partial"


async def test_gB6_production_file_cannot_pose_as_historical_plan() -> None:
    # 五审发现3：src/main 下的 AuditService.java 不再是 historical_plan，不能满足计划文档要求
    result = await _run5("请引用计划文档说明规划过。", "src/main/java/AuditService.java")
    assert not all(entry.covered for entry in result["coverage"])
    assert result["final_mode"] == "partial"


async def test_gB7_forbidden_citation_type_blocks_full_and_warns() -> None:
    # 五审发现4：否定约束必须真正执行——同时引用生产源码与被禁止的设计文档 → 不得 full
    result = await run_agent(
        _runtime_multi(
            [PLAN, EVAL_OK, _gen_citing("E1", "E2")],
            ["backend/src/main/java/Foo.java", "docs/design/hybrid-search.md"],
        ),
        _input("不要引用设计文档，只引用生产源码。"),
    )
    assert result["required_evidence"].forbidden_citation_types == ("design_doc",)
    assert result["final_mode"] == "partial"
    assert any("被禁止的证据类型" in w for w in result["warnings"])


async def test_gB8_unresolved_note_survives_claim_removal_branch() -> None:
    # 五审发现5：有 claim 被验证移除时，unresolved 限制说明不得消失
    mixed = (
        '{"answer_text":"订单校验由 owner 过滤保护 [E1]。",'
        '"claims":[{"text":"订单校验由 owner 过滤保护","evidence_ids":["E1"],'
        f'"quotes":["{_CONTENT}"]}},'
        '{"text":"编造的说法","evidence_ids":["E1"],"quotes":["订单不需要任何校验"]}],'
        '"not_found":[]}'
    )
    result = await run_agent(
        _runtime([PLAN, EVAL_OK, REFINE, EVAL_OK, mixed, mixed], "backend/src/main/java/Foo.java"),
        _input("请引用 Foo.java 和关键实现。"),
    )
    assert result["final_mode"] == "partial"
    assert any("已移除" in w for w in result["warnings"])
    assert any("无法确定性定位" in item for item in result["final_not_found"])


async def test_gB9a_unresolved_note_survives_refusal_without_draft() -> None:
    # 五审发现5：零证据直奔 finalize（draft is None）时，必需证据说明同样不得缺失
    async def empty_retriever(_pid: uuid.UUID, _q: tuple[str, ...]) -> list[Evidence]:
        return []

    eval_insufficient = (
        '{"sufficiency":"insufficient","supported_aspects":[],"missing_aspects":["证据不足"]}'
    )
    runtime = AgentRuntime(
        llm=FakeLLM([PLAN, eval_insufficient, REFINE, eval_insufficient]),
        retriever=empty_retriever,
    )
    result = await run_agent(runtime, _input("请引用 Foo.java 和关键实现。"))
    assert result["answer_draft"] is None
    assert result["final_mode"] == "refusal"
    assert any("无法确定性定位" in item for item in result["final_not_found"])
    assert any("必需证据未覆盖" in w for w in result["warnings"])


async def test_gB9_unresolved_note_survives_generate_failure_branch() -> None:
    # 五审发现5：generate 两次格式失败走冻结默认值时，限制说明同样必须出现
    result = await run_agent(
        _runtime(
            [PLAN, EVAL_OK, REFINE, EVAL_OK, "not json", "not json"],
            "backend/src/main/java/Foo.java",
        ),
        _input("请引用 Foo.java 和关键实现。"),
    )
    assert result["generate_failed"] is True
    assert result["final_mode"] == "partial"
    assert any("无法确定性定位" in item for item in result["final_not_found"])
