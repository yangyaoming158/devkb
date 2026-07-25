"""P1.5 T23：not_found 四分类（T23.1）与全轮历史事实校验（T23.2）。

分两层：确定性分类器单测（本文件上半）+ 图级 Fake 矩阵（下半，判据要求
"前轮出现过该文件→不得报缺失"）。全部零真实模型。
"""

from __future__ import annotations

import uuid

import pytest

from devkb.agent.answer import build_answer
from devkb.agent.graph import run_agent
from devkb.agent.nodes import AgentRuntime
from devkb.agent.not_found import (
    RECOGNIZED_FILE_EXTS,
    TERM_SUFFIX_HINTS,
    CorpusProfile,
    NotFoundInput,
    calibrate_not_found,
    coverage_disclosure,
)
from devkb.agent.state import AgentInput, Evidence
from devkb.ingest.pipeline import SUPPORTED_SUFFIXES
from devkb.llm import FakeLLM

# ---- T23.1 分类器单测 ------------------------------------------------------


def _calibrate(
    text: str,
    *,
    evidence_paths: tuple[str, ...] = (),
    corpus: CorpusProfile | None = None,
    source: str = "generate_draft",
    supported: tuple[str, ...] = (),
):
    return calibrate_not_found(
        [NotFoundInput(text=text, source=source)],  # type: ignore[arg-type]
        evidence_paths=evidence_paths,
        corpus=corpus or CorpusProfile.unknown(),
        supported_aspects_history=supported,
    )


def test_supported_suffix_gap_is_missing_from_current_evidence() -> None:
    result = _calibrate("未找到 OrderService.java 的删除逻辑")
    assert [d.category for d in result.details] == ["missing_from_current_evidence"]
    assert result.details[0].basis == "no_conflict_found"
    assert result.texts == ("未找到 OrderService.java 的删除逻辑",)  # 无冲突则原文保留


@pytest.mark.parametrize(
    "text",
    [
        "未找到 V1__init_schema.sql 中的建表语句",
        "缺少 src/frontend/AnswerView.vue 的渲染逻辑",
    ],
)
def test_uningested_suffix_path_is_unsupported_with_conditional_wording(text: str) -> None:
    result = _calibrate(text)
    detail = result.details[0]
    assert detail.category == "unsupported_or_not_ingested"
    assert detail.basis == "static_suffix_rule"
    assert "不在当前摄取范围" in detail.text
    assert "无法据此确认仓库是否包含此类文件" in detail.text
    # 条件式：不得断言仓库有无该文件
    for forbidden in ("仓库中没有", "仓库没有", "仓库无源码", "项目中没有该文件"):
        assert forbidden not in detail.text
    assert detail.original_text == text
    assert any("未摄取格式" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("text", "expected_suffix"),
    [
        ("未取得用户要求的必需证据（前端源码:Vue）；测试/设计/历史材料不能替代", ".vue"),
        ("未取得用户要求的必需证据（前端源码:TypeScript）；测试/设计/历史材料不能替代", ".ts"),
        ("未找到 Flyway 迁移中的建表语句", ".sql"),
        ("未取得用户要求的必需证据（数据库迁移:数据库迁移）；测试/设计/历史材料不能替代", ".sql"),
    ],
)
def test_technical_term_map_triggers_coverage_disclosure(text: str, expected_suffix: str) -> None:
    # 用户只在问题里点名、未给路径的场景：由确定性 required-evidence 说明（问题原文解析
    # 而来、带类型标签）承载技术词，分类因此仍锚定在用户表述上
    source = "deterministic" if text.startswith("未取得") else "generate_draft"
    result = _calibrate(text, source=source)
    detail = result.details[0]
    assert detail.category == "unsupported_or_not_ingested"
    assert expected_suffix in detail.refs
    assert "不在当前摄取范围" in detail.text


@pytest.mark.parametrize("text", ["未找到 Python 脚本", "未找到 Kotlin 实现", "未找到 Rust 模块"])
def test_terms_outside_the_closed_table_are_not_inferred(text: str) -> None:
    # 判据：不做开放式语言/意图推断——表外技术词一律走当前证据缺口
    assert _calibrate(text).details[0].category == "missing_from_current_evidence"


def test_classification_reads_the_item_not_the_whole_question() -> None:
    # c10：同一道题里"迁移(.sql 未摄取)"与"已索引 Java 未召回"必须分成两类，
    # 不得因为问题里提到"数据库迁移"就把所有缺口标成格式未摄取
    corpus = CorpusProfile.from_paths(["backend/src/main/java/svc/DocumentService.java"])
    named = _calibrate("未找到 DocumentService 的删除实现", corpus=corpus)
    assert named.details[0].category == "missing_from_current_evidence"
    generic = _calibrate("删除逻辑", corpus=corpus, source="evaluator_missing")
    assert generic.details[0].category == "missing_from_current_evidence"
    migration = _calibrate("未找到迁移文件中的字段定义", corpus=corpus)
    assert migration.details[0].category == "unsupported_or_not_ingested"


@pytest.mark.parametrize(
    "text",
    [
        "未找到 com.example.repo.CitationRepository 的按 message 查询方法",
        "缺少 devkb.agent.nodes 中的收尾逻辑",
        "未找到 org.springframework.web.bind.annotation 的用法",
    ],
)
def test_java_package_names_are_not_mistaken_for_uningested_formats(text: str) -> None:
    # file-token 词法会把包名切成 `com.example`：只有封闭集合内的真实扩展名才参与
    # 摄取覆盖判定，否则一条普通召回缺口会被误报成"格式未摄取"
    detail = _calibrate(text).details[0]
    assert detail.category == "missing_from_current_evidence"
    assert "摄取范围" not in detail.text


def test_recognized_ext_set_excludes_package_like_suffixes() -> None:
    for ext in (".example", ".agent", ".repo", ".springframework"):
        assert ext not in RECOGNIZED_FILE_EXTS
    for ext in (".sql", ".vue", ".ts", *SUPPORTED_SUFFIXES):
        assert ext in RECOGNIZED_FILE_EXTS


def test_hidden_files_are_outside_ingestion_scope() -> None:
    # ingest.scan_files 跳过任一段以 . 开头的路径 → 隐藏文件恒不在摄取范围（e03）
    detail = _calibrate("未找到 .env.example 中的配置项说明").details[0]
    assert detail.category == "unsupported_or_not_ingested"
    assert ".env.example" in detail.refs
    assert "无法据此确认仓库是否包含此类文件" in detail.text


def test_term_map_is_bounded_and_normalized() -> None:
    assert len(TERM_SUFFIX_HINTS) <= 12  # "有限"：封闭表，不得膨胀成开放推断
    for term, suffixes in TERM_SUFFIX_HINTS.items():
        assert term == term.lower()
        assert suffixes and all(s.startswith(".") and s == s.lower() for s in suffixes)


def test_indexed_suffix_overrides_static_rule() -> None:
    # documents 表里确实有 .vue 文档时（未来扩摄取范围），不得再报"未摄取"
    corpus = CorpusProfile.from_paths(["frontend/src/AnswerView.vue"])
    result = _calibrate("未找到 frontend/src/Other.vue", corpus=corpus)
    assert result.details[0].category == "missing_from_current_evidence"


def test_coverage_disclosure_matches_supported_suffixes() -> None:
    disclosure = coverage_disclosure()
    for suffix in SUPPORTED_SUFFIXES:
        assert suffix in disclosure


def test_confirmed_undocumented_is_never_produced() -> None:
    # P1.5 无 inventory：不得断言"资料确实未记录"/全局不存在
    corpus = CorpusProfile.from_paths(["docs/README.md", "src/main/java/Foo.java"])
    for text in (
        "仓库中不存在 Phase 6 Agent 的生产实现",
        "未找到任何 Controller",
        "缺少 V1__init.sql",
        "资料未记录该行为",
    ):
        result = _calibrate(text, corpus=corpus)
        assert all(d.category != "confirmed_undocumented" for d in result.details)


# ---- T23.2 事实校验 --------------------------------------------------------


def test_path_seen_in_any_round_cannot_be_reported_missing() -> None:
    result = _calibrate(
        "未找到 RagService 的会话组装实现",
        evidence_paths=("backend/src/main/java/svc/RagService.java",),
    )
    detail = result.details[0]
    assert detail.basis == "evidence_history"
    assert detail.refs == ("backend/src/main/java/svc/RagService.java",)
    assert "已出现在本次检索证据中" in detail.text
    assert detail.text.startswith("RagService 的会话组装实现：")
    for marker in ("未找到", "不存在"):
        assert marker not in detail.text
    assert detail.original_text == "未找到 RagService 的会话组装实现"
    assert any("事实校验修正" in w for w in result.warnings)


def test_active_indexed_path_not_retrieved_is_reported_as_recall_gap() -> None:
    corpus = CorpusProfile.from_paths(["backend/src/main/java/repo/CitationRepository.java"])
    result = _calibrate("源码中不存在 CitationRepository 的 findByMessageIds", corpus=corpus)
    detail = result.details[0]
    assert detail.basis == "corpus_index"
    assert "已在当前项目索引中" in detail.text
    assert "本轮未被召回" in detail.text
    assert any("已索引 active 路径被写成缺失" in w for w in result.warnings)


def test_rewrite_keeps_the_method_level_aspect_but_drops_absence_wording() -> None:
    # 已索引/已召回的路径不得被写成"未找到"（c02），但方法级缺口信息不能被模板吃掉：
    # 改写时主语用"去缺失标记"后的方面名
    corpus = CorpusProfile.from_paths(["backend/src/main/java/repo/CitationRepository.java"])
    result = _calibrate("未找到 CitationRepository 的按 message 查询方法", corpus=corpus)
    detail = result.details[0]
    assert detail.basis == "corpus_index"
    assert detail.text.startswith("CitationRepository 的按 message 查询方法：")
    assert "已在当前项目索引中" in detail.text
    for marker in ("未找到", "不存在", "缺失"):
        assert marker not in detail.text


def test_uningested_note_is_appended_not_replacing_the_aspect() -> None:
    # 未摄取格式且无事实冲突：原文成立（确实不在证据里），只追加条件式覆盖披露
    result = _calibrate("未找到 V1__init_schema.sql 中的建表语句")
    detail = result.details[0]
    assert detail.text.startswith("未找到 V1__init_schema.sql 中的建表语句")
    assert "不在当前摄取范围" in detail.text


def test_unknown_corpus_makes_no_index_claim() -> None:
    # 未取得快照时 fail-closed：既不说已索引也不说未索引
    result = _calibrate("源码中不存在 CitationRepository", corpus=CorpusProfile.unknown())
    detail = result.details[0]
    assert detail.basis == "unverifiable_assertion"
    assert "已在当前项目索引中" not in detail.text


def test_repo_level_negation_without_evidence_is_weakened() -> None:
    result = _calibrate("src/main 中没有实现 Phase 6 Agent")
    detail = result.details[0]
    assert detail.basis == "unverifiable_assertion"
    assert "当前证据不足以确认该结论" in detail.text
    assert "需仓库级核验" in detail.text
    assert "没有实现" not in detail.text  # 无 inventory 的强断言不得原样交付
    assert detail.original_text == "src/main 中没有实现 Phase 6 Agent"


@pytest.mark.parametrize(
    "text",
    ["未找到任何其他 Controller", "所有 Service 都没有 owner 校验", "全部 endpoint 均未提供"],
)
def test_global_negation_is_downgraded_to_exhaustiveness_boundary(text: str) -> None:
    # 判据：全局不存在需 inventory（P1.6），P1.5 只能声明"当前证据不足以穷举确认"（c12）
    detail = _calibrate(text).details[0]
    assert detail.basis == "unverifiable_assertion"
    assert "当前证据不足以穷举确认" in detail.text
    assert detail.category != "confirmed_undocumented"


def test_orphan_prefix_left_by_marker_strip_is_cleaned() -> None:
    corpus = CorpusProfile.from_paths(["backend/src/main/java/repo/CitationRepository.java"])
    detail = _calibrate(
        "当前证据未覆盖 CitationRepository 的 findByMessageIds", corpus=corpus
    ).details[0]
    assert detail.text.startswith("CitationRepository 的 findByMessageIds：")


def test_evidence_scoped_wording_without_conflict_is_left_alone() -> None:
    # 不过度改写：措辞已限定在当前证据范围且无事实冲突 → 原样保留
    result = _calibrate("当前证据未展开 OrderService 的异常分支")
    assert result.details[0].original_text is None
    assert result.details[0].basis == "no_conflict_found"


def test_previously_supported_aspect_is_not_reported_missing() -> None:
    result = _calibrate("RagService 的非法引用处理", supported=("RagService 的非法引用处理",))
    assert result.texts == ()
    assert any("前轮已支持方面" in w for w in result.warnings)


def test_deterministic_items_are_annotated_not_rewritten() -> None:
    note = "未取得用户要求的必需证据（数据库迁移:V1__init_schema.sql）；测试/设计/历史材料不能替代"
    result = _calibrate(note, source="deterministic")
    detail = result.details[0]
    assert detail.category == "unsupported_or_not_ingested"
    assert detail.text.startswith(note)  # 原确定性说明完整保留
    assert "不在当前摄取范围" in detail.text


def test_deterministic_items_are_never_dropped_by_aspect_history() -> None:
    note = "未取得用户要求的必需证据（生产源码:OrderService）；测试/设计/历史材料不能替代"
    result = _calibrate(note, source="deterministic", supported=(note,))
    assert result.texts == (note,)


def test_details_align_with_texts_and_deduplicate() -> None:
    result = calibrate_not_found(
        [
            NotFoundInput(text="未找到 A 的实现", source="generate_draft"),
            NotFoundInput(text="未找到 A 的实现", source="evaluator_missing"),
            NotFoundInput(text="  ", source="evaluator_missing"),
        ],
        evidence_paths=(),
        corpus=CorpusProfile.unknown(),
    )
    assert len(result.texts) == len(result.details) == 1
    assert [d.text for d in result.details] == list(result.texts)


def test_corpus_profile_truncates_and_marks() -> None:
    profile = CorpusProfile.from_paths([f"docs/f{i}.md" for i in range(5)], limit=3)
    assert profile.known is True
    assert profile.truncated is True
    assert len(profile.indexed_paths) == 3


# ---- 图级 Fake 矩阵（判据："前轮出现过该文件→不得报缺失"） -------------------

_CONTENT = "订单校验由 owner 过滤保护。"
PLAN = '{"intent":"knowledge_qa","queries":["订单校验"]}'
EVAL_PARTIAL = (
    '{"sufficiency":"partial","supported_aspects":["订单校验"],"missing_aspects":["删除逻辑"]}'
)
REFINE = '{"queries":["RagService 会话组装"]}'


def _gen(not_found: list[str]) -> str:
    items = ",".join(f'"{item}"' for item in not_found)
    return (
        '{"answer_text":"订单校验由 owner 过滤保护 [E1]。",'
        '"claims":[{"text":"订单校验由 owner 过滤保护","evidence_ids":["E1"],'
        f'"quotes":["{_CONTENT}"]}}],"not_found":[{items}]}}'
    )


def _rounds_runtime(script: list[str], rounds: list[list[str]], **kwargs) -> AgentRuntime:
    """每轮返回不同证据集：模拟"前轮出现、后轮被挤出"。"""
    state = {"round": 0}

    async def retriever(_pid: uuid.UUID, _q: tuple[str, ...]) -> list[Evidence]:
        paths = rounds[min(state["round"], len(rounds) - 1)]
        state["round"] += 1
        return [
            Evidence(
                evidence_id=f"E{i}",
                chunk_id=uuid.UUID(int=i + 100 * state["round"]),
                rel_path=path,
                title_path="",
                content=_CONTENT,
                start_line=1,
                end_line=3,
                score=0.5,
            )
            for i, path in enumerate(paths, start=1)
        ]

    return AgentRuntime(llm=FakeLLM(list(script)), retriever=retriever, **kwargs)


def _input(question: str) -> AgentInput:
    return AgentInput(run_id=uuid.uuid4(), project_id=uuid.uuid4(), question=question)


async def test_graph_file_seen_in_earlier_round_is_not_reported_missing() -> None:
    # 案例九型：第一轮召回 RagService，第二轮被 CitationParser 挤出，
    # generate 却声称 RagService 不存在 → 事实校验必须修正（RT-15/16）
    runtime = _rounds_runtime(
        [PLAN, EVAL_PARTIAL, REFINE, EVAL_PARTIAL, _gen(["源码中不存在 RagService 的实现"])],
        [
            ["backend/src/main/java/svc/RagService.java"],
            ["backend/src/main/java/svc/CitationParser.java"],
        ],
    )
    result = await run_agent(runtime, _input("非法引用是怎么处理的？"))
    assert "backend/src/main/java/svc/RagService.java" in result["evidence_path_history"]
    assert not any("不存在 RagService" in item for item in result["final_not_found"])
    assert any("已出现在本次检索证据中" in item for item in result["final_not_found"])
    assert any("事实校验修正" in w for w in result["warnings"])
    detail = result["final_not_found_details"][0]
    assert detail.basis == "evidence_history"
    assert detail.original_text == "源码中不存在 RagService 的实现"


async def test_graph_separates_uningested_format_from_indexed_evidence_gap() -> None:
    # 案例十型：迁移 .sql 未摄取 vs 已索引 Java 未召回，两类缺口分类必须区分
    corpus = CorpusProfile.from_paths(
        [
            "backend/src/main/java/svc/DocumentService.java",
            "backend/src/main/java/dto/CitationDto.java",
        ]
    )
    runtime = _rounds_runtime(
        [
            PLAN,
            EVAL_PARTIAL,
            REFINE,
            EVAL_PARTIAL,
            _gen(["未找到数据库迁移文件的字段定义", "未找到 DocumentService 的删除实现"]),
        ],
        [["backend/src/main/java/dto/MessageDto.java"]],
        corpus=corpus,
    )
    result = await run_agent(
        runtime, _input("删除源文档后历史引用还能显示吗？请根据数据库迁移与 DocumentService 说明。")
    )
    categories = {d.category for d in result["final_not_found_details"]}
    assert categories == {"unsupported_or_not_ingested", "missing_from_current_evidence"}
    by_basis = {d.basis: d for d in result["final_not_found_details"]}
    assert "static_suffix_rule" in by_basis
    assert ".sql" in by_basis["static_suffix_rule"].refs
    assert by_basis["corpus_index"].refs == ("backend/src/main/java/svc/DocumentService.java",)


async def test_graph_previously_supported_aspect_not_flipped_to_missing() -> None:
    eval_flip = (
        '{"sufficiency":"partial","supported_aspects":["订单校验"],"missing_aspects":["订单校验"]}'
    )
    runtime = _rounds_runtime(
        [PLAN, EVAL_PARTIAL, REFINE, eval_flip, _gen([])],
        [["backend/src/main/java/Foo.java"]],
    )
    result = await run_agent(runtime, _input("订单校验是怎么做的？"))
    assert "订单校验" not in result["final_not_found"]
    assert any("前轮已支持方面" in w for w in result["warnings"])


async def test_answer_json_carries_parallel_not_found_details() -> None:
    runtime = _rounds_runtime(
        [PLAN, EVAL_PARTIAL, REFINE, EVAL_PARTIAL, _gen(["未找到 V1__init.sql 的字段定义"])],
        [["backend/src/main/java/Foo.java"]],
    )
    state = await run_agent(runtime, _input("迁移里建了哪些表？"))
    answer = build_answer(state)
    assert answer["not_found"] == [detail["text"] for detail in answer["not_found_details"]]
    assert all(isinstance(item, str) for item in answer["not_found"])  # 契约不变：list[str]
    assert answer["not_found_details"][0]["category"] == "unsupported_or_not_ingested"
    assert answer["not_found_details"][0]["original_text"] == "未找到 V1__init.sql 的字段定义"
