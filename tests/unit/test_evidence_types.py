"""T22 逐项 required-evidence 解析、可信目录优先分类与确定性 coverage（RT-01/05）。

对抗性用例：覆盖复审确认的 4 处缺陷（点名不匹配错误 full、test SQL/flyway 文档冒充
生产、否定语义反转、漏识别），确保回归。
"""

from __future__ import annotations

import pytest

from devkb.agent.evidence_types import (
    RequiredEvidence,
    all_required_covered,
    classify_path,
    compute_coverage,
    parse_required_evidence,
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
    r = parse_required_evidence("不要引用设计文档，只引用生产源码。")
    assert "design_doc" in r.forbidden_substitute_types
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
