"""T26.2 安全隔离题的确定性层级分层（规格 §8 第 2 句，RT-18）。

三条纪律（全部确定性、零 LLM 调用）：

1. **只陈述可证事实**。能证的是"某条交付引用的**片段内**，语句自身含/不含 owner
   谓词"；证不出"该文件或该方法的查询都带 owner 过滤"——P1.5 的 chunk 是文件/粗段
   级且可能截断，方法级召回属 P1.6（RT-06）。
2. **`service_precheck` 层恒不判定**。"Service 前置校验后调用非 owner-scoped 语句"
   需要跨 chunk 调用链，本阶段既无方法级 chunk 也无调用图，故只声明未判定——既不说
   它成立，也不说它不成立。
3. **两张闭合表互不复用**。表 A 是**问题**触发词，表 B 是**内容**里的 owner token。
   ``kb_id`` 只在表 A：《P1后真实仓库可用性测试问题记录》§10.3 明写 ``listByKb`` /
   ``countByKb`` 只按 ``kb_id`` 过滤是 **Service 前置保护、不是 Repository 语句自身
   带 owner 条件**——它恰恰是本模块要判为"未见 owner 谓词"的中央反例。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from devkb.agent.not_found import _render_refs

# 表 A · 问题触发词（**有序**闭合表；每一词条的逐字出处见任务合同，扩充需新任务）。
# 出处仅限 contract_dev(c01/c07)、contract_dev_extended(e01)、规格 §8、问题记录 §10.3；
# 前审 PG-05 删除了 4 个出处不成立的词条（能否删除/访问控制/权限/越权）。
ISOLATION_TERMS: tuple[str, ...] = (
    "隔离",
    "owner_id",
    "kb_id",
    "user_id",
    "owner-scoped",
    "能否读取",
    "读取或删除",
    "强制实现",
)

# 表 B · owner 谓词 token（闭合）。限定形式 kb.owner_id / c.user_id 含上述子串，自动覆盖。
# **kb_id 不在此表**——理由见模块 docstring 第 3 条。
_OWNER_TOKENS: tuple[str, ...] = ("OWNER_ID", "USER_ID")
# 谓词关键词与比较运算符都要词边界：CONVERSATIONS 里含 "ON"、BRAND 里含 "AND"，
# 不加边界会把选择列误判成谓词位置。
_PREDICATE_KEYWORD = re.compile(r"\b(?:WHERE|AND|ON)\b")
_COMPARISON = re.compile(r"!=|<>|=|\bIN\b")

_LAYER_PREFIX = "（本次隔离层级分层："
_SERVICE_LAYER_UNJUDGED = (
    "“Service 前置校验后调用非 owner-scoped 语句”需跨 chunk 调用链判定，本阶段不作判定。"
)
# 末句刻意避开 T26.1 的闭合全称词表（_UNIVERSAL_TERMS）：写成"不代表…全部查询"会命中
# "全部"，让本段自触发 T26.1 的全称披露 warning（前审 PG-01）。
_LAYER_SUFFIX = "本分层只覆盖上述引用片段，未覆盖同一文件或方法中未被引用的其余语句。）"


def is_isolation_question(question: str) -> tuple[str, ...]:
    """问题命中了表 A 的哪几个触发词（按表声明顺序，去重）。

    只是**词法事实**。推不出"这是安全问题"，也推不出"这不是安全问题"——表外的问法
    （例如只说"有没有越权风险"）不命中，命中也可能只是巧合。调用方据此决定是否输出
    分层，漏判的后果是**整段省略**（退化为 T26.1 的范围句），而不是输出错误分层。
    """
    normalized = unicodedata.normalize("NFKC", question).lower()
    return tuple(term for term in ISOLATION_TERMS if term in normalized)


def owner_predicate_in(content: str) -> bool:
    """证据片段里是否存在**约束 owner 的谓词**（表 B 判据，按行判定）。

    某行成立当且仅当三者按位置递增出现：谓词关键词（WHERE/AND/ON，词边界）→ 表 B 的
    owner token → 比较运算符（``=`` / ``!=`` / ``<>`` / ``IN``）。位置序是关键——
    ``SELECT owner_id FROM t WHERE id = ?`` 里 owner token 在选择列、早于 WHERE，
    谓词并未约束 owner，故判 False；只做"同行共现"会把它误判成 True。

    返回 False **只**表示"本片段内未见"，推不出"该处没有 owner 过滤"——片段可能被
    截断，也可能保护在别的方法里。调用方的文案必须相应限定。
    """
    for line in content.splitlines():
        upper = line.upper()
        keyword = _PREDICATE_KEYWORD.search(upper)
        if keyword is None:
            continue
        for token in _OWNER_TOKENS:
            position = upper.find(token, keyword.end())
            if position < 0:
                continue
            if _COMPARISON.search(upper, position + len(token)):
                return True
    return False


def enforcement_layer_note(
    rows: Sequence[tuple[str, str, bool, bool]],
) -> str:
    """渲染分层段；``rows`` 为 ``(evidence_id, rel_path, 含谓词, 是语句证据)``。

    行身份是 ``(evidence_id, rel_path)`` 而**不是**路径：同一文件的两个 chunk 在
    Answer ``citations`` 里就是两条，按路径去重会让"该读哪段内容"不可确定（前审
    PG-03）。行标签因此渲染为 ``rel_path(evidence_id)``——用圆括号而非方括号，
    避免撞上 ``evaluation.py`` 对 ``[E#]`` 的 Answer L0 扫描。

    ``rows`` 为空返回空串（无分层对象）；非空时恒返回一段，即使全部是非语句证据——
    否则"分层段存在 ⟺ 命中 ∧ 有交付行"这条不变量不成立。
    """
    if not rows:
        return ""
    with_predicate = [
        f"{rel_path}({evidence_id})"
        for evidence_id, rel_path, has_predicate, is_statement in rows
        if is_statement and has_predicate
    ]
    without_predicate = [
        f"{rel_path}({evidence_id})"
        for evidence_id, rel_path, has_predicate, is_statement in rows
        if is_statement and not has_predicate
    ]
    non_statement = sum(1 for *_rest, is_statement in rows if not is_statement)

    clauses: list[str] = []
    if with_predicate:
        clauses.append(
            f"{len(with_predicate)} 条语句片段内含 owner 谓词（{_render_refs(with_predicate)}）"
        )
    if without_predicate:
        clauses.append(
            f"{len(without_predicate)} 条片段内未见 owner 谓词（{_render_refs(without_predicate)}）"
        )
    head = f"已交付引用中，{'；'.join(clauses)}。" if clauses else ""
    tail = f"另 {non_statement} 条为非语句证据，不作分层。" if non_statement else ""
    if not clauses:
        head = f"已交付引用中，{non_statement} 条为非语句证据，不作分层。"
        tail = ""
    return f"{_LAYER_PREFIX}{head}{_SERVICE_LAYER_UNJUDGED}{tail}{_LAYER_SUFFIX}"
