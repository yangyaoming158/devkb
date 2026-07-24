"""T22 证据类型分层与 required-evidence 确定性解析（RT-01/05）。"""

from __future__ import annotations

import pytest

from devkb.agent.evidence_types import (
    classify_path,
    parse_required_evidence,
    unmet_required_types,
)


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        (
            "backend/src/main/java/com/ragdocs/repository/RetrievalRepository.java",
            "production_source",
        ),
        ("backend/src/test/java/com/ragdocs/repository/RetrievalRepositoryIT.java", "test"),
        ("backend/src/main/resources/db/migration/V1__init_schema.sql", "migration"),
        ("backend/src/main/resources/application.yml", "production_config"),
        ("docker-compose.yml", "production_config"),
        (".env.example", "production_config"),
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


def test_parse_c01_design_and_production() -> None:
    r = parse_required_evidence("请分别引用设计文档和 Java 实现。")
    assert set(r.required_types) == {"production_source", "design_doc"}
    assert r.forbidden_substitute_types == ()


def test_parse_c04_deployment_no_production_demand() -> None:
    r = parse_required_evidence(
        "该系统生产环境部署在哪家云厂商？正式域名、SLA、RTO 和 RPO 分别是多少？"
    )
    assert r.required_types == ()


def test_parse_c05_forbid_devlog_and_plan() -> None:
    r = parse_required_evidence(
        "不要使用 dev-log 或规划文档代替实现。IngestionRecoveryRunner 如何处理？"
        "请引用生产 Java 源码和对应测试。"
    )
    assert "production_source" in r.required_types
    assert "dev_log" in r.forbidden_substitute_types
    assert "historical_plan" in r.forbidden_substitute_types


def test_parse_c09_readme_and_test_are_substitutes_not_required() -> None:
    r = parse_required_evidence(
        "只依据生产 Java 源码解释 NO_ANSWER，不要用 README、架构文档或测试代替实现。"
    )
    assert "production_source" in r.required_types
    assert "test" in r.forbidden_substitute_types
    # README 出现在"不要用 README...代替"语境，不得被当作必需
    assert "current_doc" not in r.required_types


def test_parse_c10_migration_required_design_forbidden() -> None:
    r = parse_required_evidence(
        "请根据数据库迁移、DocumentService 和 DTO 说明 chunk_id 会发生什么，不要只引用设计文档。"
    )
    assert "migration" in r.required_types
    assert "design_doc" in r.forbidden_substitute_types
    assert "design_doc" not in r.required_types


def test_parse_c11_plan_forbidden_srcmain_required() -> None:
    r = parse_required_evidence(
        "Phase 6 Agent 是否已实现？不要把规划文档当作实现证据；请引用实际 src/main 代码。"
    )
    assert "production_source" in r.required_types
    assert "historical_plan" in r.forbidden_substitute_types


def test_parse_natural_phrasing_no_over_constraint() -> None:
    # e01：无显式证据类型词 → 空约束，优雅降级到 P1 行为
    r = parse_required_evidence(
        "后端是怎么把向量检索和关键词检索的结果合到一起排序的？隔离怎么保证一个库不串到另一个？"
    )
    assert r.required_types == ()


def test_parse_named_symbols() -> None:
    r = parse_required_evidence("请引用 RetrievalRepository 和 CitationParser 的实现。")
    assert "RetrievalRepository" in r.named_symbols
    assert "CitationParser" in r.named_symbols


def test_unmet_test_does_not_cover_production() -> None:
    r = parse_required_evidence("请引用生产 Java 源码。")
    unmet = unmet_required_types(r, ["backend/src/test/java/FooTest.java"])
    assert unmet == ["production_source"]


def test_unmet_historical_plan_does_not_cover_production() -> None:
    # T22.3 权威性分层："规划材料不支撑已实现"——规划文档不能替代必需生产源码
    r = parse_required_evidence("请引用生产 Java 源码。")
    assert unmet_required_types(r, ["docs/plans/phase-6.md"]) == ["production_source"]


def test_unmet_production_citation_covers() -> None:
    r = parse_required_evidence("请引用生产 Java 源码。")
    assert unmet_required_types(r, ["backend/src/main/java/Foo.java"]) == []


def test_unmet_empty_when_no_requirements() -> None:
    r = parse_required_evidence("这个项目是做什么的？")
    assert unmet_required_types(r, []) == []


def test_unmet_partial_coverage_reports_missing_type() -> None:
    r = parse_required_evidence("请分别引用设计文档和 Java 实现。")
    # 只引用了生产源码，缺设计文档
    unmet = unmet_required_types(r, ["backend/src/main/java/Foo.java"])
    assert unmet == ["design_doc"]
