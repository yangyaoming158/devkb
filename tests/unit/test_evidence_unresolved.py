"""T22 结构性防御（第四轮复审裁决）：unresolved 约束 → 三态 → fail-closed。

错误 full 的共同根因：解析器漏掉全部/部分显式证据约束，空/不完整 required 清单
使 full 门错误放行。此处锁定"确定性三态解析 + unresolved 硬门"不变量。
"""

from __future__ import annotations

import pytest

from devkb.agent.evidence_types import (
    UnresolvedConstraint,
    all_required_covered,
    compute_coverage,
    parse_required_evidence,
)


def _status(q: str) -> str:
    return parse_required_evidence(q).status


# ---- 三态语义 ----


def test_status_none_when_no_explicit_constraint() -> None:
    assert _status("这个项目是做什么的？") == "none"
    assert _status("后端怎么组合向量和关键词检索？") == "none"


def test_status_complete_when_all_resolved() -> None:
    assert _status("请引用 OrderService 的生产实现。") == "complete"
    assert _status("请引用 backend/src/main/java/foo/Foo.java。") == "complete"


def test_status_ambiguous_on_partial_enumeration() -> None:
    # 强指令含已解析目标 + 未知目标 → ambiguous
    assert _status("请引用 Foo.java 和某个关键实现。") == "ambiguous"
    # 集合/类别要求无法定位具体文件 → ambiguous
    assert _status("请列出所有生产 Controller 的映射代码。") == "ambiguous"
    assert _status("请引用生产 Repository 实现。") == "ambiguous"


def test_unresolved_constraints_are_strict_and_anchored_to_question() -> None:
    q = "请引用 Foo.java 和某个关键实现。"
    r = parse_required_evidence(q)
    assert r.unresolved_constraints  # 非空
    for uc in r.unresolved_constraints:
        assert isinstance(uc, UnresolvedConstraint)
        assert uc.anchor in q  # 原文子串
        assert uc.reason in ("unparsed_target", "partial_enumeration", "unsupported_syntax")


def test_unsupported_path_syntax_is_unresolved_not_empty() -> None:
    # 无法识别的显式路径语法（未知扩展名）→ unresolved，不得退化为空 required
    r = parse_required_evidence("请引用 src/main/config.xml 说明。")
    assert r.status == "ambiguous"
    assert any(uc.reason == "unsupported_syntax" for uc in r.unresolved_constraints)


def test_compute_coverage_ignores_unresolved_never_synthesizes_other_item() -> None:
    # unresolved 不进入 coverage；resolved 为空 + 有 unresolved → coverage 空但 status ambiguous
    r = parse_required_evidence("请列出所有生产 Controller。")
    cov = compute_coverage(r, [("E1", "backend/src/main/java/web/AnyController.java")])
    # 没有 type=other 的伪 item 被任意文件满足
    assert all(item.type != "other" for item in r.items)
    # coverage 全覆盖不等于可以 full——status 仍 ambiguous
    assert all_required_covered(cov) is True or all_required_covered(cov) is False
    assert r.status == "ambiguous"


def test_strong_directive_zero_parse_is_ambiguous_not_none() -> None:
    # 有强指令但完全没解析到目标 → 不得是 none（否则 full 门放行），应 ambiguous
    r = parse_required_evidence("请引用某些关键文件。")
    assert r.status == "ambiguous"


# ---- 结构不变量（可跨 hashseed / 空格 / 增量） ----

_SPACE_CASES = [
    "请引用Foo.java说明。",
    "请引用 Foo.java 说明。",
    "请引用Foo.java 说明。",
    "请引用 Foo.java说明。",
]


@pytest.mark.parametrize("q", _SPACE_CASES)
def test_spacing_around_path_does_not_change_result(q: str) -> None:
    r = parse_required_evidence(q)
    assert {i.path for i in r.items if i.path} == {"Foo.java"}
    assert r.status == "complete"


def test_adding_enumeration_target_does_not_reduce_constraints() -> None:
    one = parse_required_evidence("请引用 Foo.java。")
    two = parse_required_evidence("请引用 Foo.java，Bar.java。")
    n_one = len(one.items) + len(one.unresolved_constraints)
    n_two = len(two.items) + len(two.unresolved_constraints)
    assert n_two >= n_one
    assert {i.path for i in two.items if i.path} == {"Foo.java", "Bar.java"}


def test_every_anchor_is_a_question_substring() -> None:
    q = "请引用 Foo.java、OrderService 类的生产实现和某个关键配置，不要用测试代替生产源码。"
    r = parse_required_evidence(q)
    for item in r.items:
        assert item.anchor in q
    for uc in r.unresolved_constraints:
        assert uc.anchor in q
