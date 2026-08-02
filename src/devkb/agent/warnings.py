"""T30.2 warning 归属与时态划分（规格 §14 / RT-10 / m14 断言 4）。

`warnings` 通道本身是扁平只增的 `list[str]`，看不出"哪条由哪个节点的第几次执行产生"，
也看不出"这条在终态还成不成立"——案例二/八因此在 `mode=full` 的答案上继续展示首稿的
`verify:l0_l1_failed`，案例五/十三则给出两条逐字节相同、无法区分稿次的记录。

本模块只做两件事，全部确定性、零 LLM 调用：
1. `attribute`：把节点边界拿到的 (node, attempt) 机械附到该次返回的每条 warning 上；
2. `classify_warnings`：在终态按**两条冻结规则**把账本划成 active / resolved。

判据可证边界（合同「关键不变量 · 判据可证边界」B1–B7）：
- `resolved` 只证明"该记录报告的对象不是终态交付物"或"该逻辑调用最终取得了合法结构化
  输出"，**推不出**后一稿更正确、重问无成本、该节点业务结论正确；
- `active` 只证明"两条规则都没判定它被取代/已解决"，**推不出**"它在终态仍然成立"——
  未识别 code 一律 active 是 fail-closed 默认值，不是对内容的再判定；
- `node`/`attempt` 只证明该 warning 由该节点的第 N 次执行**返回**，**推不出**问题**源自**
  该节点（finalize 转载的 L0 越界标记，其成因在 generate）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 与 graph.py 的 add_node 集合同源；新增节点必须同步，否则该节点的 warning 无法归属
NodeName = Literal[
    "plan",
    "retrieve",
    "evaluate",
    "refine",
    "generate",
    "verify",
    "finalize",
    "policy_refuse",
]
WarningStatus = Literal["active", "resolved"]
# superseded_by_regeneration = 该稿已被后一次 verify 取代（不等于后一稿通过）
# reask_succeeded           = 同一逻辑调用重问后取得了合法结构化输出（不等于内容可信）
WarningResolution = Literal["superseded_by_regeneration", "reask_succeeded"]

VERIFY_FAILED_CODE = "verify:l0_l1_failed"
_DEFAULT_APPLIED_SUFFIX = ":default_applied"
# 只有这四个节点调用 _structured_call（nodes.py:939/1069/1128/1181），因此只有它们
# 可能产出重问类 code。retrieve/verify/finalize/policy_refuse 的 warning 一律不适用 R2。
_LLM_CALL_NODES = frozenset({"plan", "evaluate", "refine", "generate"})
# `_structured_call` 的机器格式（nodes.py:676/699）。**完整锚定** ^…$：不用 in/endswith——
# finalize 的多条 warning 逐字插值 LLM 自由文本（not_found.py:691 → :768-772），
# 子串匹配会把真实的终态 warning 误判成已解决（G8 实测可达）。
_REASK_FAILURE = re.compile(
    r"^(?P<call_key>plan|refine|evaluate:[1-9][0-9]*|generate:[1-9][0-9]*)"
    r":(?:invalid_structured_output|request_failed:[A-Za-z_][A-Za-z0-9_]*)$"
)


class WarningRecord(BaseModel):
    """状态通道元素：一条 warning 的产生归属，由节点边界包装器机械构造。

    `code` 是既有 `warnings` 通道的**原文转载**，故与该通道同合同——plain `str`，
    不加非空或长度约束：`GenerateOutput.not_found` 单项合法上限就是 500 字符
    （state.py:59/145），finalize 把原文连模板前缀拼出的 warning 实测可达 530 字符，
    任何长度上限都会把结构化输出完全合法的 run 打成 failed。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    code: str
    node: NodeName
    attempt: int = Field(ge=1)


class WarningDetail(BaseModel):
    """Answer 层逐条明细；四条交叉约束使非法状态在类型层不可表达。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    code: str
    node: NodeName | None = None
    attempt: int | None = Field(default=None, ge=1)
    status: WarningStatus
    resolution: WarningResolution | None = None

    @model_validator(mode="after")
    def _status_and_attribution_are_consistent(self) -> WarningDetail:
        if self.status == "active" and self.resolution is not None:
            raise ValueError("active 的条目不得带 resolution")
        if self.status == "resolved" and self.resolution is None:
            raise ValueError("resolved 的条目必须给出 resolution")
        if (self.node is None) != (self.attempt is None):
            raise ValueError("node 与 attempt 必须成对出现")
        if self.status == "resolved" and self.node is None:
            # 无归属的条目不可能被任何规则判定——两条规则都读 node/attempt
            raise ValueError("resolved 的条目必须有归属")
        return self


class WarningLedger(BaseModel):
    """终态账本：`details` 是唯一权威，`active`/`resolved` 是它的**保序投影**。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    active: tuple[str, ...]
    resolved: tuple[str, ...]
    details: tuple[WarningDetail, ...]


def attribute(node: NodeName, attempt: int, codes: Sequence[str]) -> list[WarningRecord]:
    """把 (node, attempt) 附到本次节点执行返回的每条 warning 上，保序、不去重。"""
    return [WarningRecord(code=code, node=node, attempt=attempt) for code in codes]


def _aligned(codes: Sequence[str], records: Sequence[WarningRecord]) -> bool:
    return len(codes) == len(records) and all(
        record.code == code for code, record in zip(codes, records, strict=True)
    )


def _resolution(
    record: WarningRecord,
    *,
    verify_runs: int,
    defaulted: frozenset[tuple[str, int, str]],
) -> WarningResolution | None:
    """两条冻结规则；任何一项不成立都落回 None（active）——fail-closed 方向。"""
    # R1：该稿之后还跑过 verify，说明它不是终态交付的那一稿（案例二/八）。
    # 取 verify_runs 而非"记录里的最大 attempt"：第二稿通过时 verify **不发** warning，
    # 用记录反推恰好会漏判案例二/八这个形状。
    if (
        record.node == "verify"
        and record.code == VERIFY_FAILED_CODE
        and record.attempt < verify_runs
    ):
        return "superseded_by_regeneration"
    # R2 ①：只有四个 LLM 调用点节点可能产出重问类 code
    if record.node not in _LLM_CALL_NODES:
        return None
    # R2 ②：完整锚定的规范语法
    match = _REASK_FAILURE.match(record.code)
    if match is None:
        return None
    call_key = match.group("call_key")
    # R2 ③：call_key 的节点前缀须与记录节点一致（generate:1 只能来自 generate）
    if call_key.split(":", 1)[0] != record.node:
        return None
    # R2 ④：同一次节点执行内若最终走了冻结默认值，则该组全部保持 active
    if (record.node, record.attempt, call_key) in defaulted:
        return None
    return "reask_succeeded"


def classify_warnings(
    codes: Sequence[str],
    records: Sequence[WarningRecord],
    *,
    verify_runs: int,
) -> WarningLedger:
    """终态账本 → active/resolved 划分（纯函数，只读结构化状态）。

    `codes` 是权威条目集合（既有 `warnings` 通道），`records` 只提供归属。两者逐位
    不一致时**全部按 active 呈现且不给归属**：绝不猜、绝不隐藏——绕过图直接构造
    AgentState 的调用方会走到这一支。
    """
    if not _aligned(codes, records):
        unattributed = tuple(WarningDetail(code=code, status="active") for code in codes)
        return WarningLedger(active=tuple(codes), resolved=(), details=unattributed)

    defaulted = frozenset(
        (record.node, record.attempt, record.code[: -len(_DEFAULT_APPLIED_SUFFIX)])
        for record in records
        if record.code.endswith(_DEFAULT_APPLIED_SUFFIX)
    )
    details: list[WarningDetail] = []
    for record in records:
        resolution = _resolution(record, verify_runs=verify_runs, defaulted=defaulted)
        details.append(
            WarningDetail(
                code=record.code,
                node=record.node,
                attempt=record.attempt,
                status="active" if resolution is None else "resolved",
                resolution=resolution,
            )
        )
    return WarningLedger(
        active=tuple(detail.code for detail in details if detail.status == "active"),
        resolved=tuple(detail.code for detail in details if detail.status == "resolved"),
        details=tuple(details),
    )
