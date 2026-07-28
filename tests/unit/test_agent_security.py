"""T26.2 安全隔离层级分层的纯函数矩阵（合同：docs/tasks/T26.2-security-matrix.md）。

测试侧**不导入**生产常量，字面量就是合同——实现改一个字必须在这里同步改，
避免"测试镜像实现"（T26.1 首审 CR-01 的教训）。
"""

from __future__ import annotations

import random
import re

import pytest

from devkb.agent.security import (
    enforcement_layer_note,
    is_isolation_question,
    owner_predicate_in,
)

# 表 A 的冻结副本（顺序即合同）
T262_TERMS = (
    "隔离",
    "owner_id",
    "kb_id",
    "user_id",
    "owner-scoped",
    "能否读取",
    "读取或删除",
    "强制实现",
)
# 前审 PG-05 删除的 4 个词条：出处不成立，必须**不**命中
T262_DELETED = ("能否删除", "访问控制", "权限", "越权")
T262_PREFIX = "（本次隔离层级分层："
T262_UNJUDGED = "本阶段不作判定"
# T26.1 的闭合全称词表（冻结副本）：分层段命中其中任一词就会自触发 T26.1 的披露 warning
T261_UNIVERSAL = (
    "任何",
    "所有",
    "全部",
    "一切",
    "每个",
    "各个",
    "均",
    "都",
    "一律",
    "无一",
    "毫无",
    "从不",
)


@pytest.mark.parametrize("term", T262_TERMS)
def test_t262_u1_each_retained_trigger_term_is_detected(term: str) -> None:
    """U1（逐词参数化）：表 A 每个保留词条各自可被识别。"""
    assert is_isolation_question(f"请说明{term}的实现") == (term,)


@pytest.mark.parametrize("term", T262_DELETED)
def test_t262_u1b_deleted_trigger_terms_never_match(term: str) -> None:
    """U1b：PG-05 删除的 4 个词条不得复活——它们没有隔离语境的非-holdout 出处。"""
    assert is_isolation_question(f"这个项目有没有{term}问题？") == ()


def test_t262_u1c_real_dev_questions_hit_the_expected_term_sets() -> None:
    """U1c：三条 dev 原文（c01/c07/e01）的命中集合，按表 A 声明顺序、去重。"""
    c07 = (
        "用户 A 能否读取或删除用户 B 的知识库、文档和会话？"
        "请根据生产 Java 和 SQL 说明 owner_id 隔离分别在哪些 Service 与 Repository 强制实现"
    )
    c01 = "Hybrid Search 如何组合 vector、keyword 和 RRF？kb_id 隔离在哪一层强制实现？"
    e01 = (
        "后端是怎么把向量检索和关键词检索的结果合到一起排序的？"
        "隔离是怎么保证一个知识库不串到另一个的？"
    )

    assert is_isolation_question(c07) == (
        "隔离",
        "owner_id",
        "能否读取",
        "读取或删除",
        "强制实现",
    )
    assert is_isolation_question(c01) == ("隔离", "kb_id", "强制实现")
    assert is_isolation_question(e01) == ("隔离",)
    assert is_isolation_question("库存扣减是怎么实现的？") == ()


# U3：表 B 判据在六个仓库真实形态上的取值（合同「表 B」表格逐条）
@pytest.mark.parametrize(
    ("line", "expected", "why"),
    [
        ("SELECT * FROM knowledge_bases WHERE id = ? AND owner_id = ?", True, "AND<owner_id<="),
        (
            "DELETE FROM documents d USING knowledge_bases kb WHERE kb.owner_id = ?",
            True,
            "WHERE<kb.owner_id<=",
        ),
        (
            "SELECT * FROM conversations c JOIN knowledge_bases kb ON c.user_id = ?",
            True,
            "ON<c.user_id<=（CONVERSATIONS 内的 ON 因词边界不误命中）",
        ),
        ("SELECT * FROM documents WHERE kb_id = ?", False, "kb_id 不在表 B——§10.3 中央反例"),
        ("SELECT owner_id FROM t WHERE id = ?", False, "owner_id 在选择列，谓词未约束 owner"),
        ("private UUID owner_id;", False, "该行无谓词关键词"),
        # 第 7 形态：执行者用变异检验发现前六个形态无法判别「词边界」这条精度
        # （去掉 \\b 后六者取值不变）。CONVERSATIONS 内含 "ON"、其后又有 owner_id 与
        # "="，无边界即误判为谓词——而该行根本没有 WHERE/AND/ON 子句。
        (
            'String sql = "SELECT * FROM conversations"; long owner_id = 1;',
            False,
            "CONVERSATIONS 内的 ON 不是谓词关键词——词边界必须生效",
        ),
    ],
)
def test_t262_u3_owner_predicate_judgement_matches_the_frozen_table(
    line: str, expected: bool, why: str
) -> None:
    assert owner_predicate_in(line) is expected, why


def test_t262_u2_note_matches_the_frozen_template() -> None:
    """U2：三行两含一不含，分层段逐字等于冻结模板，行标签为 rel_path(evidence_id)。"""
    note = enforcement_layer_note(
        [
            ("E1", "A.java", True, True),
            ("E2", "B.java", True, True),
            ("E3", "C.java", False, True),
        ]
    )
    assert note == (
        "（本次隔离层级分层：已交付引用中，2 条语句片段内含 owner 谓词（A.java(E1)、B.java(E2)）；"
        "1 条片段内未见 owner 谓词（C.java(E3)）。"
        '"Service 前置校验后调用非 owner-scoped 语句"需跨 chunk 调用链判定，本阶段不作判定。'
        "本分层只覆盖上述引用片段，未覆盖同一文件或方法中未被引用的其余语句。）"
    )


def test_t262_u4_more_than_three_rows_use_the_frozen_truncation() -> None:
    """U4：行标签超 3 条沿用「前 3 条 + 等 N 条」，N 是实际行数。"""
    rows = [(f"E{i}", f"F{i}.java", True, True) for i in range(1, 6)]
    note = enforcement_layer_note(rows)

    assert "5 条语句片段内含 owner 谓词" in note
    assert "F1.java(E1)、F2.java(E2)、F3.java(E3) 等 5 条" in note
    assert "F4.java(E4)" not in note


def test_t262_u5_non_statement_evidence_is_excluded_from_both_counts() -> None:
    """U5：文档/测试类引用归入「非语句证据」，不参与两个计数。"""
    note = enforcement_layer_note(
        [
            ("E1", "A.java", True, True),
            ("E2", "docs/design.md", False, False),
            ("E3", "T.java", False, False),
        ]
    )

    assert "1 条语句片段内含 owner 谓词（A.java(E1)）" in note
    assert "未见 owner 谓词" not in note  # 没有语句证据落在这一档
    assert "另 2 条为非语句证据，不作分层。" in note
    assert "docs/design.md" not in note

    # 全部为非语句证据时仍出一段（否则「存在 ⟺ 命中 ∧ 有交付行」不成立）
    only_docs = enforcement_layer_note([("E1", "docs/a.md", False, False)])
    assert only_docs.startswith(T262_PREFIX)
    assert "1 条为非语句证据，不作分层。" in only_docs
    assert T262_UNJUDGED in only_docs


def test_t262_u5b_same_path_different_evidence_ids_stay_two_rows() -> None:
    """U5b（PG-03）：同路径不同 evidence_id 是两行，分别落入两个计数，不被路径合并。"""
    note = enforcement_layer_note([("E1", "A.java", True, True), ("E2", "A.java", False, True)])

    assert "1 条语句片段内含 owner 谓词（A.java(E1)）" in note
    assert "1 条片段内未见 owner 谓词（A.java(E2)）" in note


def test_t262_u8_positive_wording_is_scoped_to_the_delivered_fragment() -> None:
    """U8（fail-closed #1）：含谓词的措辞限定在「片段内」，不得升格为文件/方法级断言。"""
    note = enforcement_layer_note([("E1", "A.java", True, True)])

    assert "语句片段内含 owner 谓词" in note
    for over in ("该文件所有查询", "该方法", "均带 owner", "所有查询", "全部查询"):
        assert over not in note, over


def test_t262_u9_negative_wording_never_claims_absence() -> None:
    """U9（fail-closed #2）：未见谓词只说「片段内未见」，不得说成「没有/缺少/不安全」。"""
    note = enforcement_layer_note([("E1", "A.java", False, True)])

    assert "片段内未见 owner 谓词" in note
    for over in ("没有 owner", "缺少 owner", "未做隔离", "不安全", "存在越权", "无 owner 过滤"):
        assert over not in note, over


def test_t262_u10_service_precheck_layer_is_always_declared_unjudged() -> None:
    """U10（fail-closed #3）：service_precheck 层恒声明未判定，不得被肯定或否定。"""
    for rows in (
        [("E1", "A.java", True, True)],
        [("E1", "A.java", False, True)],
        [("E1", "docs/a.md", False, False)],
    ):
        note = enforcement_layer_note(rows)
        assert "需跨 chunk 调用链判定，本阶段不作判定。" in note
        for verdict in (
            "Service 前置校验成立",
            "Service 层也重复校验",
            "存在双重防线",
            "未做前置校验",
            "Service 未校验",
        ):
            assert verdict not in note, verdict


def test_t262_u13_note_is_a_pure_function_of_the_rows() -> None:
    """U13：随机 200 组——存在性、计数守恒、对 T26.1 全称词表零命中、恒无 [E#]。"""
    rng = random.Random(2620)
    seen_empty = seen_all_docs = seen_dup_path = seen_truncated = False
    for _ in range(200):
        count = rng.randint(0, 6)
        rows: list[tuple[str, str, bool, bool]] = []
        for i in range(count):
            # 刻意让路径在小集合里取值，制造同路径不同 evidence_id
            rows.append(
                (
                    f"E{i + 1}",
                    f"pkg/F{rng.randint(1, 2)}.java",
                    rng.random() < 0.5,
                    rng.random() < 0.7,
                )
            )
        note = enforcement_layer_note(rows)

        # ① 存在 ⟺ 有交付行
        if not rows:
            assert note == ""
            seen_empty = True
            continue
        assert note.startswith(T262_PREFIX)

        # ② 三个计数之和 == 行数（不是路径数）
        statements = [r for r in rows if r[3]]
        with_pred = sum(1 for r in statements if r[2])
        without_pred = len(statements) - with_pred
        non_statement = len(rows) - len(statements)
        assert with_pred + without_pred + non_statement == len(rows)
        if with_pred:
            assert f"{with_pred} 条语句片段内含 owner 谓词" in note
        if without_pred:
            assert f"{without_pred} 条片段内未见 owner 谓词" in note
        if non_statement:
            assert f"{non_statement} 条为非语句证据" in note
        seen_all_docs |= not statements
        seen_dup_path |= len({r[1] for r in rows}) < len(rows)
        seen_truncated |= with_pred > 3 or without_pred > 3

        # ③ 对 T26.1 闭合全称词表零命中（否则两段互相触发）
        for term in T261_UNIVERSAL:
            assert term not in note, (term, note)
        # ④ 恒无 [E#]——evaluation.py:214 的 Answer L0 会把它当未知标记
        assert "[E" not in note and not re.search(r"\[E\d+\]", note)
    assert seen_empty and seen_all_docs and seen_dup_path and seen_truncated
