"""T22 逐项 required-evidence 解析、可信目录优先分类与确定性 coverage（RT-01/05）。

对抗性用例：覆盖复审确认的 4 处缺陷（点名不匹配错误 full、test SQL/flyway 文档冒充
生产、否定语义反转、漏识别），确保回归。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from devkb.agent.evidence_types import (
    MAX_REQUIRED_ITEMS,
    RequiredEvidence,
    all_required_covered,
    classify_path,
    compute_coverage,
    forbidden_citation_hits,
    iter_path_tokens,
    iter_symbol_tokens,
    iter_target_spans,
    parse_required_evidence,
    required_satisfied,
)


def _types(r: RequiredEvidence) -> set[str]:
    return {item.type for item in r.items}


def _symbols(r: RequiredEvidence) -> set[str]:
    return {item.symbol for item in r.items if item.symbol}


# ---- classify_path：可信目录优先，含复审冲突矩阵 ----


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        (
            "backend/src/main/java/com/ragdocs/repository/RetrievalRepository.java",
            "production_source",
        ),
        ("backend/src/test/java/com/ragdocs/repository/RetrievalRepositoryIT.java", "test"),
        ("backend/src/main/resources/db/migration/V1__init_schema.sql", "migration"),
        # 复审发现3：test 目录下的 .sql 不得判为生产 migration
        ("backend/src/test/resources/db/migration/V1__fixture.sql", "test"),
        ("backend/src/main/resources/application.yml", "production_config"),
        ("docker-compose.yml", "production_config"),
        (".env.example", "production_config"),
        # 三审发现3：application.* 只按配置扩展名，其余不得冒充生产配置
        ("application.md", "current_doc"),
        ("application.java", "production_source"),
        ("application.txt", "other"),
        ("backend/src/main/resources/application.properties", "production_config"),
        # 复审发现3：docs 下含 flyway/audit 的 markdown 不得判为生产
        ("docs/review/flyway-audit.md", "historical_plan"),
        ("docs/review/architecture-audit-2026-07-03.md", "historical_plan"),
        ("docs/design/hybrid-search.md", "design_doc"),
        ("docs/plans/phase-0.md", "historical_plan"),
        ("docs/dev-log.md", "dev_log"),
        ("README.md", "current_doc"),
        ("PROGRESS.md", "current_doc"),
        ("docs/architecture.md", "current_doc"),
        ("frontend/src/views/ChatView.vue", "frontend_source"),
        ("frontend/src/api/conversations.ts", "frontend_source"),
        ("data/model.bin", "other"),
    ],
)
def test_classify_path(rel_path: str, expected: str) -> None:
    assert classify_path(rel_path) == expected


# ---- parse：类型、否定语义、点名符号 ----


def test_parse_c01_design_and_production_type_items() -> None:
    r = parse_required_evidence("请分别引用设计文档和 Java 实现。")
    assert _types(r) == {"production_source", "design_doc"}
    assert r.forbidden_substitute_types == ()


def test_parse_c04_deployment_no_items() -> None:
    r = parse_required_evidence(
        "该系统生产环境部署在哪家云厂商？正式域名、SLA、RTO 和 RPO 是多少？"
    )
    assert r.items == ()


def test_parse_c05_test_required_devlog_and_plan_forbidden() -> None:
    r = parse_required_evidence(
        "不要使用 dev-log 或规划文档代替实现。请引用生产 Java 源码和对应测试。"
    )
    assert "production_source" in _types(r)
    assert "test" in _types(r)  # "对应测试" 是必需，不是被漏识别
    assert "dev_log" in r.forbidden_substitute_types
    assert "historical_plan" in r.forbidden_substitute_types


def test_parse_negation_do_not_cite_design_is_forbidden_not_required() -> None:
    # 复审发现4：否定语义反转 —— "不要引用设计文档" 不得反而把 design_doc 设为必需
    # 五审发现4：明确的"不要引用"是**禁止引用**（强于禁止替代），须分开记录
    r = parse_required_evidence("不要引用设计文档，只引用生产源码。")
    assert "design_doc" in r.forbidden_citation_types
    assert "design_doc" in r.non_substitutable_types
    assert "design_doc" not in _types(r)
    assert "production_source" in _types(r)


def test_parse_positive_plan_reference_is_required_not_forbidden() -> None:
    # 复审发现4：否定反转 —— "引用计划文档说明规划过" 是肯定引用，不得被禁止
    r = parse_required_evidence("请引用计划文档说明规划过。")
    assert "historical_plan" in _types(r)
    assert "historical_plan" not in r.forbidden_substitute_types


def test_parse_named_classes_each_become_item_not_merged() -> None:
    r = parse_required_evidence(
        "请引用 RetrievalRepository、CitationParser、RagService 的生产实现。"
    )
    assert _symbols(r) == {"RetrievalRepository", "CitationParser", "RagService"}
    assert all(item.type == "production_source" for item in r.items)


def test_parse_supplement_test_is_forbidden_substitute() -> None:
    r = parse_required_evidence(
        "请分别引用 DocumentController、DocumentService 和生产 Repository 实现，测试只能作为补充。"
    )
    assert "DocumentController" in _symbols(r)
    assert "test" in r.forbidden_substitute_types
    assert "test" not in _types(r)


def test_parse_natural_phrasing_no_items() -> None:
    r = parse_required_evidence("后端怎么把向量检索和关键词检索的结果合到一起排序？")
    assert r.items == ()


# ---- coverage：逐项匹配（复审发现1、3 的错误 full） ----


def test_named_mismatch_is_not_covered() -> None:
    # 复审发现1：点名 3 个类，引用无关生产文件 → 不得全覆盖（不得 full）
    r = parse_required_evidence(
        "请引用 RetrievalRepository、CitationParser、RagService 的生产实现。"
    )
    coverage = compute_coverage(r, [("E1", "backend/src/main/java/other/Foo.java")])
    assert not all_required_covered(coverage)


def test_named_exact_citation_is_covered() -> None:
    r = parse_required_evidence("请引用 RetrievalRepository 的生产实现。")
    coverage = compute_coverage(
        r, [("E1", "backend/src/main/java/com/ragdocs/repository/RetrievalRepository.java")]
    )
    assert all_required_covered(coverage)


def test_test_named_file_does_not_cover_production_symbol() -> None:
    # RetrievalRepositoryTest.java 是 test 类型，不能覆盖 production_source 的点名项
    r = parse_required_evidence("请引用 RetrievalRepository 的生产实现。")
    coverage = compute_coverage(
        r, [("E1", "backend/src/test/java/com/ragdocs/RetrievalRepositoryTest.java")]
    )
    assert not all_required_covered(coverage)


def test_test_sql_fixture_does_not_cover_migration() -> None:
    # 复审发现3连锁：仅引用 src/test 下的 .sql fixture 不能满足 migration 必需
    r = parse_required_evidence("请根据数据库迁移说明级联删除。")
    assert "migration" in _types(r)
    coverage = compute_coverage(
        r, [("E1", "backend/src/test/resources/db/migration/V1__fixture.sql")]
    )
    assert not all_required_covered(coverage)


def test_type_only_covered_by_any_of_type() -> None:
    r = parse_required_evidence("请引用生产 Java 源码。")
    coverage = compute_coverage(r, [("E1", "backend/src/main/java/anything/Any.java")])
    assert all_required_covered(coverage)


def test_no_requirements_all_covered() -> None:
    r = parse_required_evidence("这个项目是做什么的？")
    assert all_required_covered(compute_coverage(r, []))


def test_required_item_ids_are_stable_and_unique() -> None:
    r = parse_required_evidence("请引用 RetrievalRepository、CitationParser 的生产实现和设计文档。")
    ids = [item.item_id for item in r.items]
    assert ids == sorted(ids, key=lambda x: int(x[1:]))
    assert len(ids) == len(set(ids))


# ---- 第二轮复审对抗用例（4 处新发现，先红后修） ----


def test_explicit_path_survives_and_wrong_file_not_covered() -> None:
    # 复审发现1：子句切分不得切断 Foo.java；显式路径须完整进入 required
    r = parse_required_evidence("请引用 backend/src/main/java/foo/Foo.java 说明订单校验。")
    assert "backend/src/main/java/foo/Foo.java" in {i.path for i in r.items if i.path}
    cov = compute_coverage(r, [("E1", "backend/src/main/java/foo/NotFoo.java")])
    assert not all_required_covered(cov)


def test_bare_filename_matches_basename_not_substring() -> None:
    # 复审发现1：裸文件名按 basename 全等，foo.java 不得子串误配 notfoo.java
    r = parse_required_evidence("请引用 Foo.java 说明。")
    assert not all_required_covered(
        compute_coverage(r, [("E1", "backend/src/main/java/foo/NotFoo.java")])
    )
    assert all_required_covered(compute_coverage(r, [("E1", "backend/src/main/java/foo/Foo.java")]))


def test_substitution_forbids_x_but_protects_positive_y() -> None:
    # 复审发现2：'不要用测试代替生产源码' → test 禁止、生产源码仍必需（不得被删）
    r = parse_required_evidence("不要用测试代替生产源码，请引用生产源码说明订单校验。")
    assert "production_source" in _types(r)
    assert "test" in r.forbidden_substitute_types
    assert "test" not in _types(r)
    # 只引用测试文件不能满足被保护的生产源码必需项
    cov = compute_coverage(r, [("E1", "backend/src/test/java/FooTest.java")])
    assert not all_required_covered(cov)


def test_named_class_never_typed_migration_despite_migration_word() -> None:
    # 复审发现3：同句出现"数据库迁移"不得把 DocumentService 误判为 migration
    r = parse_required_evidence(
        "请根据数据库迁移、DocumentService、CitationRepository 说明级联删除。"
    )
    by_symbol = {i.symbol: i.type for i in r.items if i.symbol}
    assert by_symbol["DocumentService"] == "production_source"
    assert by_symbol["CitationRepository"] == "production_source"
    assert "migration" in _types(r)  # 迁移作为 type-only 项仍在


def test_type_only_anchor_is_substring_of_question() -> None:
    # 复审发现3：type-only 项的 anchor 必须锚定问题原文，而非规范化标签
    question = "请引用设计文档和生产 Java 源码。"
    r = parse_required_evidence(question)
    assert all(item.anchor in question for item in r.items)
    assert "生产 Java 源码" in {item.anchor for item in r.items}


def test_item_order_follows_text_position_deterministically() -> None:
    # 复审发现3：item_id 按原文位置排序，不依赖无序集合（PYTHONHASHSEED 无关）
    r = parse_required_evidence("请引用设计文档、生产 Java 源码和对应测试。")
    assert [(i.item_id, i.type) for i in r.items] == [
        ("R1", "design_doc"),
        ("R2", "production_source"),
        ("R3", "test"),
    ]


# ---- 第三轮复审对抗用例（组合场景，先红后修） ----


def test_negation_period_does_not_leak_across_sentences() -> None:
    # 三审发现1：删 "." 会让否定跨句污染；须遮蔽路径后按句点分句
    r = parse_required_evidence("不要引用测试. 请引用 backend/src/main/java/foo/Foo.java.")
    assert "production_source" in _types(r)  # Foo.java 仍是必需，未被否定吞掉
    assert "test" in r.forbidden_citation_types
    cov = compute_coverage(r, [("E1", "backend/src/test/java/foo/FooTest.java")])
    assert not all_required_covered(cov)  # 只引测试文件不得 full


def test_production_config_keyword_detected() -> None:
    # 三审发现2：冻结判据列出的"生产配置"须被识别为 production_config
    r = parse_required_evidence("请引用生产配置说明部署。")
    assert "production_config" in _types(r)
    cov = compute_coverage(r, [("E1", "backend/src/test/java/FooTest.java")])
    assert not all_required_covered(cov)  # 测试证据不能满足生产配置必需


def test_config_requirement_not_covered_by_application_markdown() -> None:
    # 三审发现3：application.md 不是配置，不能覆盖 production_config 必需
    r = parse_required_evidence("请引用配置文件说明。")
    assert "production_config" in _types(r)
    assert not all_required_covered(compute_coverage(r, [("E1", "docs/application.md")]))


def test_natural_question_tech_name_is_not_a_required_symbol() -> None:
    # 三审发现4：无"引用/根据"指令的自然问句里技术名不得成为伪必需点名类
    r = parse_required_evidence("这个后端有没有用到消息队列（如 Kafka/RabbitMQ）？")
    assert r.items == ()


def test_named_symbol_only_within_cite_directive_clause() -> None:
    # 有指令才提取点名；无指令的陈述句提及类名不算必需
    assert parse_required_evidence("系统里 OrderService 是干嘛的？").items == ()
    assert "OrderService" in _symbols(parse_required_evidence("请引用 OrderService 的生产实现。"))


def test_substitution_protected_named_side_is_required_without_directive() -> None:
    # 自查补洞：'不要用测试代替 Order.java' 的被保护侧无"引用"指令，也须成为必需，
    # 否则只引 OrderTest.java 会错误 full
    r = parse_required_evidence("不要用测试代替 Order.java。")
    assert "Order.java" in {i.path for i in r.items if i.path}
    assert "test" in r.forbidden_substitute_types
    assert not all_required_covered(
        compute_coverage(r, [("E1", "backend/src/test/java/OrderTest.java")])
    )


# ---- 第四轮复审：分类大小写、匹配大小写、单词类名、后缀、枚举、弱指令 ----


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        # 发现5：lower() 后 endswith("test.java") 的伪命中——生产目录优先
        ("backend/src/main/java/Contest.java", "production_source"),
        ("backend/src/main/java/Latest.java", "production_source"),
        ("backend/src/main/java/OrderTest.java", "production_source"),
        ("backend/src/test/java/Order.java", "test"),
        ("OrderTest.java", "test"),
        # 后缀对齐：P1.5 识别但未摄取的显式后缀也要能分类
        ("db/migration/V2__x.sql", "migration"),
        ("frontend/src/App.vue", "frontend_source"),
        ("frontend/src/main.ts", "frontend_source"),
        ("docs/notes.txt", "other"),
    ],
)
def test_classify_path_case_sensitive_and_dir_priority(rel_path: str, expected: str) -> None:
    assert classify_path(rel_path) == expected


def test_ingested_ext_lexer_matches_supported_suffixes() -> None:
    # 防漂移：required-path lexer 的"已摄取后缀"集合必须与 ingest.SUPPORTED_SUFFIXES 对齐
    from devkb.agent.evidence_types import _INGESTED_EXTS
    from devkb.ingest.pipeline import SUPPORTED_SUFFIXES

    assert {s.lstrip(".") for s in SUPPORTED_SUFFIXES} == _INGESTED_EXTS


def test_path_and_symbol_match_is_case_sensitive() -> None:
    # 发现6：ext4 大小写敏感，Foo.java / foo.java / NotFoo.java 互不覆盖
    r = parse_required_evidence("请引用 Foo.java。")
    assert not all_required_covered(compute_coverage(r, [("E1", "x/foo.java")]))
    assert not all_required_covered(compute_coverage(r, [("E1", "x/NotFoo.java")]))
    assert all_required_covered(compute_coverage(r, [("E1", "x/Foo.java")]))


def test_single_word_and_abbrev_class_names_resolve() -> None:
    # 发现3：单词类名 Order、缩写 URLParser、"X 类" 都要成为逐项 symbol
    assert "Order" in _symbols(parse_required_evidence("请引用 Order 类的生产实现。"))
    assert "URLParser" in _symbols(parse_required_evidence("请引用 URLParser 的实现代码。"))
    assert "DTO" in _symbols(parse_required_evidence("请引用 DTO 类。"))


def test_comma_enumeration_keeps_all_targets() -> None:
    # 发现2：逗号/顿号列表在同一证据指令下不得丢目标
    r1 = parse_required_evidence("请引用 Foo.java，Bar.java。")
    assert {i.path for i in r1.items if i.path} == {"Foo.java", "Bar.java"}
    r2 = parse_required_evidence("请引用 OrderService，CitationParser 的生产实现。")
    assert {"OrderService", "CitationParser"} <= _symbols(r2)


def test_explicit_txt_path_is_required() -> None:
    # 发现4：.txt 是受支持后缀，必须成为必需路径
    r = parse_required_evidence("请引用 docs/notes.txt。")
    assert "docs/notes.txt" in {i.path for i in r.items if i.path}
    assert not all_required_covered(
        compute_coverage(r, [("E1", "backend/src/test/java/FooTest.java")])
    )


def test_weak_directive_needs_evidence_context() -> None:
    # 弱指令（根据/结合）只有邻接证据类型词/路径/X类 才进入约束解析
    assert parse_required_evidence("系统根据 OrderStatus 如何选择分支？").items == ()
    assert parse_required_evidence("OrderService 如何结合 RabbitMQ 实现异步处理？").items == ()
    # 但 "根据 OrderService 的生产源码" 是明确证据约束
    r = parse_required_evidence("请根据 OrderService 的生产源码说明处理流程。")
    assert "OrderService" in _symbols(r)
    assert "production_source" in _types(r)


def test_role_word_category_is_unresolved_not_resolved_symbol() -> None:
    # 泛化角色词（Repository/Controller/DTO）作为集合要求 → 不得当已解析 symbol
    r = parse_required_evidence("请引用生产 Repository 实现。")
    assert "Repository" not in _symbols(r)
    assert r.status == "ambiguous"


# ---- 第五轮复审：约束跨度账本（逐段结算、身份、分类、两类否定、schema 上限） ----


def test_partial_parse_keeps_remaining_target_as_unresolved() -> None:
    # 五审发现1：解析出一个目标后，同一义务下剩余目标不得静默消失
    r = parse_required_evidence("请引用 Foo.java 和关键实现。")
    assert {i.path for i in r.items if i.path} == {"Foo.java"}
    assert [uc.anchor for uc in r.unresolved_constraints] == ["关键实现"]
    assert r.status == "ambiguous"


def test_vague_type_category_in_enumeration_is_unresolved() -> None:
    # 五审发现1（c08）："相关 Java 配置"无身份 → unresolved，不得只留 3 个具体项后报 complete
    r = parse_required_evidence(
        "请区分本地裸 JVM 与 Docker Compose 的生效范围，"
        "并引用 README、docker-compose.yml、application.yml 和相关 Java 配置。"
    )
    assert {i.path for i in r.items if i.path} == {"docker-compose.yml", "application.yml"}
    assert "README" in _symbols(r)
    assert any(uc.anchor == "相关 Java 配置" for uc in r.unresolved_constraints)
    assert r.status == "ambiguous"


def test_verification_style_question_is_an_evidence_obligation() -> None:
    # 五审发现1（c02）：比较/核验表达（"README 声称…""…是否支持这个说法"）也是证据义务，
    # 整题不得解析为 status=none 使硬门真空化
    r = parse_required_evidence(
        "README 声称默认 Mock Provider 不配置模型 key 也能运行。"
        "Java 配置和 Provider 实现是否支持这个说法？如果文档与实现不一致，请明确指出。"
    )
    assert r.status == "ambiguous"
    assert "README" in _symbols(r)
    anchors = {uc.anchor for uc in r.unresolved_constraints}
    assert "Java 配置" in anchors
    assert "Provider" in anchors


def test_non_target_tail_clause_creates_no_constraint() -> None:
    # 逐段结算不得把谓语/说明部分当目标：只有含目标名词的段才结算为 unresolved
    r = parse_required_evidence("请引用生产 Java 源码，说明订单如何校验。")
    assert _types(r) == {"production_source"}
    assert r.unresolved_constraints == ()
    assert r.status == "complete"


def test_canonical_doc_name_keeps_identity() -> None:
    # 五审发现2：裸 README 不得退化为 type-only current_doc（否则任意文档都能顶替）
    r = parse_required_evidence("请引用 README。")
    assert _symbols(r) == {"README"}
    assert not all_required_covered(compute_coverage(r, [("E1", "docs/architecture.md")]))
    assert all_required_covered(compute_coverage(r, [("E1", "README.md")]))


def test_named_test_symbol_with_production_context_requires_production_file() -> None:
    # 五审发现2："OrderTest 的生产实现"要求生产文件 OrderTest.java，
    # 不得被 src/test/OrderTest.java + 任意生产文件的组合满足
    r = parse_required_evidence("请引用 OrderTest 的生产实现。")
    assert [(i.type, i.symbol) for i in r.items] == [("production_source", "OrderTest")]
    assert not all_required_covered(
        compute_coverage(
            r, [("E1", "src/test/java/OrderTest.java"), ("E2", "src/main/java/Foo.java")]
        )
    )
    assert all_required_covered(compute_coverage(r, [("E1", "src/main/java/OrderTest.java")]))


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        # 五审发现3：文件名约定只应用于文档扩展名，生产源码不得冒充非生产证据
        ("src/main/java/AuditService.java", "production_source"),
        ("src/main/java/ChangelogService.java", "production_source"),
        ("src/main/java/review/ReviewService.java", "production_source"),
        ("src/main/java/design/DesignService.java", "production_source"),
        ("src/main/java/plan/PlanService.java", "production_source"),
        # Test* 词边界：Testing/Testimony 不是测试；Test 开头的真测试仍是测试
        ("Testimony.java", "production_source"),
        ("Testing.java", "production_source"),
        ("TestOrderService.java", "test"),
        ("OrderServiceSpec.java", "test"),
        ("OrderServiceIT.java", "test"),
        ("SPLIT.java", "production_source"),
        # 文档仍按约定分层
        ("docs/CHANGELOG.md", "dev_log"),
        ("docs/audit-2026.md", "historical_plan"),
    ],
)
def test_classify_path_filename_conventions_are_doc_only(rel_path: str, expected: str) -> None:
    assert classify_path(rel_path) == expected


def test_production_file_cannot_satisfy_non_production_requirement() -> None:
    # 分类修复的连锁效果：AuditService.java 不再能满足"历史计划"类要求
    r = parse_required_evidence("请引用计划文档说明规划过。")
    assert "historical_plan" in _types(r)
    assert not all_required_covered(
        compute_coverage(r, [("E1", "src/main/java/AuditService.java")])
    )


def test_forbidden_citation_is_enforced_not_a_dead_signal() -> None:
    # 五审发现4：禁止引用必须真正执行——直接引用被禁类型时 required_satisfied 为假
    r = parse_required_evidence("不要引用设计文档，只引用生产源码。")
    both = [("E1", "backend/src/main/java/Foo.java"), ("E2", "docs/design/hybrid.md")]
    assert all_required_covered(compute_coverage(r, both))  # 生产源码本身已覆盖
    assert forbidden_citation_hits(r, both) == (("E2", "docs/design/hybrid.md", "design_doc"),)
    assert not required_satisfied(r, compute_coverage(r, both), cited=both)
    only_production = [("E1", "backend/src/main/java/Foo.java")]
    assert required_satisfied(r, compute_coverage(r, only_production), cited=only_production)


@pytest.mark.parametrize(
    "question",
    [
        "请引用生产源码，测试只能作为补充。",
        "不要用测试代替生产源码，请引用生产源码。",
        "不要只引用测试，请引用生产源码。",
    ],
)
def test_supplement_and_substitution_are_not_citation_bans(question: str) -> None:
    # 五审发现4："只能作为补充"/"不要用 X 代替 Y"/"不要只引用 X" ≠ 禁止引用
    r = parse_required_evidence(question)
    assert "test" in r.forbidden_substitute_types
    assert r.forbidden_citation_types == ()
    cited = [("E1", "backend/src/main/java/Foo.java"), ("E2", "src/test/java/FooTest.java")]
    assert forbidden_citation_hits(r, cited) == ()
    assert required_satisfied(r, compute_coverage(r, cited), cited=cited)


def test_negation_list_across_commas_keeps_every_forbidden_type() -> None:
    # 自查补洞：顿号切开的否定列表 + 替代结构不得被截断成"不要用 README"
    # （否则丢掉 test 禁止、且把替代结构误判成禁止引用）
    r = parse_required_evidence("不要用 README、架构文档或测试代替实现。请引用 RagService。")
    assert "current_doc" in r.forbidden_substitute_types
    assert "test" in r.forbidden_substitute_types
    assert r.forbidden_citation_types == ()
    assert "RagService" in _symbols(r)


@pytest.mark.parametrize(
    "question",
    [
        "删除源文档后，历史会话中的引用是否还能显示？",
        "非法引用编号如何处理？",
        "这些引用是从哪里来的？",
    ],
)
def test_noun_use_of_citation_word_is_not_an_obligation(question: str) -> None:
    # 自查补洞：'引用' 作名词（"…中的引用""非法引用编号"）不得当祈使指令，
    # 否则会凭空产出 unresolved 噪声（"是否还能显示"）
    r = parse_required_evidence(question)
    assert r.items == ()
    assert r.unresolved_constraints == ()


def test_imperative_connectives_still_activate_strong_directive() -> None:
    # 祈使锚定不得误杀"并引用 X""请分别引用 X"
    assert "README" in _symbols(parse_required_evidence("并引用 README。"))
    assert "README" in _symbols(parse_required_evidence("请分别引用 README 和生产源码。"))
    # 非祈使但邻接证据信号（"在回答中引用生产源码"）仍按证据约束处理
    assert "production_source" in _types(parse_required_evidence("在回答中引用生产源码。"))


def test_required_items_over_schema_limit_are_not_reported_complete() -> None:
    # 五审发现6：resolved 项数不得超过 plan/evaluate schema 上限却仍报 complete
    paths = "、".join(f"A{i}.java" for i in range(1, MAX_REQUIRED_ITEMS + 3))
    r = parse_required_evidence(f"请引用 {paths}。")
    assert len(r.items) == MAX_REQUIRED_ITEMS
    assert r.status == "ambiguous"
    assert any(uc.reason == "partial_enumeration" for uc in r.unresolved_constraints)


def test_required_item_limit_matches_llm_schema_capacity() -> None:
    # 防漂移：确定性上限必须与 plan 回显/evaluate coverage 的 schema 上限同源
    from devkb.agent.state import EvaluateOutput, PlanOutput

    assert PlanOutput.model_fields["required_evidence"].metadata[0].max_length == (
        MAX_REQUIRED_ITEMS
    )
    assert EvaluateOutput.model_fields["coverage"].metadata[0].max_length == MAX_REQUIRED_ITEMS


@pytest.mark.parametrize(
    "question",
    [
        "后端是怎么把向量检索和关键词检索的结果合到一起排序的？隔离是怎么保证的？",
        "作为开发者，我应该怎么安全地管理这个项目的 API key 和 JWT secret？有什么最佳实践？",
        "项目的 .env.example 里列了哪些配置项？各自是做什么用的？",
        "这个后端有没有用到消息队列（如 Kafka/RabbitMQ）？",
        "系统启动时会不会自动建表？",
    ],
)
def test_natural_phrasing_still_has_no_explicit_constraint(question: str) -> None:
    # 反过拟合：新增的核验谓词/目标名词规则不得让自然问法凭空产生约束
    r = parse_required_evidence(question)
    assert r.items == ()
    assert r.unresolved_constraints == ()
    assert r.status == "none"


def test_target_spans_agree_with_the_shared_token_lexer() -> None:
    # T23 的目标切分靠这些区间定位"连接词两侧是不是目标"：区间必须与 token 词法同源，
    # 不得漂移出第二套口径（同一 token 被多条正则命中时只留一段）
    text = "未找到 backend/src/main/java/svc/OrderService.java、README 与 DocumentService 的实现"
    spans = iter_target_spans(text)
    covered = [text[start:end] for start, end in spans]

    for token in (*iter_path_tokens(text), *iter_symbol_tokens(text)):
        assert any(token in chunk for chunk in covered), token
    assert spans == sorted(spans)
    assert all(start < end for start, end in spans)
    assert all(left[1] <= right[0] for left, right in pairwise(spans))


def test_target_spans_exclude_role_words_and_stop_words() -> None:
    # 角色词/语言名能匹配任意文件，用作切分锚点只会制造假连接处
    assert iter_target_spans("未找到 Service 与 Controller 的 Java 实现") == []
