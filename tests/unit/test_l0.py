"""L0 引用存在性校验（纯函数，规格 §6）。"""

from __future__ import annotations

from devkb.answer import apply_l0


def test_valid_citations_kept_and_sorted() -> None:
    text, cited, warnings = apply_l0("结论 A [E2]，结论 B [E1]。", evidence_count=3)
    assert text == "结论 A [E2]，结论 B [E1]。"
    assert cited == [1, 2]
    assert warnings == []


def test_out_of_range_removed_with_warning() -> None:
    text, cited, warnings = apply_l0("真实 [E1]。虚构 [E99]。", evidence_count=2)
    assert text == "真实 [E1]。虚构 。"
    assert cited == [1]
    assert len(warnings) == 1 and "[E99]" in warnings[0]


def test_zero_is_invalid_evidence_id() -> None:
    text, cited, warnings = apply_l0("[E0] 编号从 1 开始。", evidence_count=2)
    assert text == " 编号从 1 开始。"
    assert cited == []
    assert len(warnings) == 1


def test_duplicate_citations_counted_once() -> None:
    _, cited, warnings = apply_l0("[E1] 和 [E1] 重复。", evidence_count=1)
    assert cited == [1]
    assert warnings == []


def test_no_citations() -> None:
    text, cited, warnings = apply_l0("没有任何引用。", evidence_count=5)
    assert text == "没有任何引用。"
    assert cited == []
    assert warnings == []


def test_empty_evidence_set_rejects_all() -> None:
    _, cited, warnings = apply_l0("[E1] 无中生有。", evidence_count=0)
    assert cited == []
    assert len(warnings) == 1
