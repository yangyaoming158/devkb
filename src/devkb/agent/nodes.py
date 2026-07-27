"""T16 Agent 节点：结构化调用、预算计数与冻结默认路径。"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.agent import prompts
from devkb.agent.aspects import (
    MAX_ELIMINATION_RECORDS,
    AspectStatus,
    EvidenceRef,
    build_matrix,
    displaced_aspects,
    is_monotonically_sufficient,
    observe_round,
    outstanding_missing,
    plan_retention,
    retention_warnings,
    supported_labels,
)
from devkb.agent.evidence_types import (
    TYPE_LABELS,
    EvidenceType,
    RequiredEvidence,
    RequiredEvidenceItem,
    bound_required_ids,
    classify_path,
    compute_coverage,
    forbidden_citation_hits,
    iter_path_tokens,
    iter_symbol_tokens,
    iter_target_spans,
    path_matches_token,
    required_satisfied,
    target_tokens_all_match,
    uncovered_items,
)
from devkb.agent.not_found import (
    CorpusProfile,
    NotFoundInput,
    NotFoundSource,
    calibrate_not_found,
    strip_absence_markers,
)
from devkb.agent.state import (
    MAX_GENERATE_CALLS,
    MAX_LLM_REQUESTS,
    MAX_REASKS_PER_CALL,
    MAX_RETRIEVAL_ROUNDS,
    AgentState,
    ClaimOutput,
    DraftSnapshot,
    EvaluateOutput,
    Evidence,
    FinalMode,
    GenerateOutput,
    PlanOutput,
    RefineOutput,
    StrictModel,
    VerificationOutput,
)
from devkb.agent.trace import TraceRecorder, clip_list
from devkb.agent.verification import verify_draft
from devkb.answer import apply_l0
from devkb.embedding import Embedder
from devkb.llm import LLMClient, compute_cost
from devkb.retrieval import (
    HNSW_EF_SEARCH,
    MAX_FINAL_TOP_K,
    ChannelRanking,
    RetrievedChunk,
    retrieve,
    rrf_fuse,
)

Retriever = Callable[[uuid.UUID, tuple[str, ...]], Awaitable[list[Evidence]]]


def _refusal_text(missing: list[str]) -> str:
    """确定性拒答模板（说明缺什么），不消耗 LLM 预算。"""
    if missing:
        return f"现有资料不足以回答该问题。缺少：{'；'.join(missing)}。"
    return "现有资料不足以回答该问题。"


def _merge_unique(*groups: list[str]) -> list[str]:
    return list(dict.fromkeys(item for group in groups for item in group))


def _not_found_items(texts: list[str], source: NotFoundSource) -> list[NotFoundInput]:
    """带来源标签的缺口条目：来源决定是否可被事实校验改写（确定性条目不改写）。"""
    return [NotFoundInput(text=text, source=source) for text in texts]


def _draft_items(texts: list[str]) -> list[NotFoundInput]:
    return _not_found_items(texts, "generate_draft")


def _evaluator_items(texts: list[str]) -> list[NotFoundInput]:
    return _not_found_items(texts, "evaluator_missing")


def _deterministic_items(texts: list[str]) -> list[NotFoundInput]:
    return _not_found_items(texts, "deterministic")


def _merge_citations(*groups: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """按 evidence_id 去重合并引用账本，保持出现顺序（确定性）。"""
    return list(dict.fromkeys(item for group in groups for item in group))


_EVIDENCE_MARK = re.compile(r"\[E\d+\]")


def monotonic_matrix(state: AgentState) -> tuple[AspectStatus, ...]:
    """当前状态的跨轮单调覆盖矩阵（T24）；路由与 finalize 共用同一口径。"""
    return build_matrix(
        state["required_evidence"],
        state["aspect_observations"],
        state["coverage"],
        present_chunk_ids=[evidence.chunk_id for evidence in state["evidences"]],
    )


def required_evidence_tail(
    required: RequiredEvidence,
    support_citations: list[tuple[str, str]],
    visible_citations: list[tuple[str, str]],
    retrieved: list[tuple[str, str]] | None = None,
    matrix: Sequence[AspectStatus] = (),
) -> tuple[list[str], list[str], bool]:
    """确定性必需证据收尾（所有 finalize 分支共用）→ (not_found 追加项, warnings, 满足)。

    第五轮复审发现5：unresolved/未覆盖说明只写在"无 claim 被移除"的一条分支里，
    draft is None / generate_failed / 有 claim 被移除时都会静默丢失。故收敛为公共收尾：
    任何终态都按同一口径披露"必需证据未取得 / 约束无法定位 / 引用了被禁止的类型"。

    **两套引用账本（第六轮复审 P1）**：

    - ``support_citations``——保留 claim 申报的 evidence，只有它能满足必需证据；正文
      裸标记不得反过来充当支撑（否则 [E#] 就能凭空满足点名要求）。
    - ``visible_citations``——最终交付给用户的全部引用（正文过 L0 后仍保留的 [E#]
      ∪ claim 支撑）。禁止引用类型按这一套判：正文引用了被禁类型即违规，即便 claim
      没申报它——L0/L1 只校验标记存在与引文忠实，不要求正文标记出现在 claim 中。

    ``retrieved``（终态证据集）与 ``matrix``（跨轮单调矩阵）用来把"未覆盖"拆成**三种
    事实不同**的缺口（T24 复审发现2：跨轮保留会让后两种明显变多，若都写成"未取得"，
    就与矩阵里的 ``present_now`` / ``supported`` 自相矛盾——同一个方面不能既告警
    "被挤出、非证伪"又被告知用户"未取得"）：

    1. **从未取得**——任何一轮都没有该证据；
    2. **仍在证据集但未被引用**——证据在，但没有任何保留 claim 引用它；
    3. **前轮取得、终态被挤出**——曾有直接证据，因跨轮容量上限不在终态证据集里，
       本次无法作为引用支撑（覆盖仍按单调矩阵保留为已支持，不得改写成缺失）。
    """
    coverage = compute_coverage(required, support_citations)
    uncovered = uncovered_items(required, coverage)
    in_evidence = {
        entry.item_id for entry in compute_coverage(required, retrieved or []) if entry.covered
    }
    ever_supported = {
        row.aspect_id for row in matrix if row.origin == "required_evidence" and row.supported
    }
    violations = forbidden_citation_hits(required, visible_citations)
    notes: list[str] = []
    warnings: list[str] = []
    if uncovered:
        # 按证据类型分组各出一条说明：混在一条里会让 T23 的缺口分类失去分辨率
        # （"数据库迁移(.sql 未摄取)" 与 "生产源码(已索引未召回)" 必须能分开，c10）
        absent: dict[str, list[str]] = {}
        uncited: list[str] = []
        displaced: list[str] = []
        for item in uncovered:
            label = f"{TYPE_LABELS[item.type]}:{item.symbol or item.path or item.anchor}"
            if item.item_id in in_evidence:
                uncited.append(label)
            elif item.item_id in ever_supported:
                displaced.append(label)
            else:
                absent.setdefault(item.type, []).append(label)
        for labels in absent.values():
            notes.append(
                f"未取得用户要求的必需证据（{'、'.join(labels)}）；测试/设计/历史材料不能替代"
            )
        if uncited:
            notes.append(
                f"用户要求的必需证据已在本次证据集中，但未被任何断言直接引用"
                f"（{'、'.join(uncited)}）；该部分结论未经引用支撑"
            )
        if displaced:
            notes.append(
                f"用户要求的必需证据在前几轮检索中已取得，但受证据容量上限被挤出终态证据集"
                f"（{'、'.join(displaced)}）；本次无法作为引用支撑，覆盖按单调矩阵保留"
            )
        warnings.append(
            "finalize: 必需证据未覆盖（" + ",".join(item.item_id for item in uncovered) + "）"
        )
    if required.unresolved_constraints:
        anchors = "、".join(uc.anchor for uc in required.unresolved_constraints)
        notes.append(
            f"用户要求的部分证据目标无法确定性定位（{anchors}）；"
            "当前证据不足以穷举确认，最高 partial"
        )
        warnings.append(
            "finalize: 存在未解析证据约束（"
            + ",".join(uc.reason for uc in required.unresolved_constraints)
            + "）"
        )
    if violations:
        labels = "、".join(f"{TYPE_LABELS[etype]}:{path}" for _eid, path, etype in violations)
        notes.append(f"用户明确要求不引用的证据类型被引用（{labels}）；该部分不作为支撑依据")
        warnings.append(
            "finalize: 引用了被禁止的证据类型（"
            + ",".join(sorted({etype for _eid, _path, etype in violations}))
            + "）"
        )
    return notes, warnings, required_satisfied(required, coverage, cited=visible_citations)


# 缺口文本去掉目标后允许残留的**封闭**限定词：只覆盖"这条证据本身"的说法。
# 与类型无关的身份词——只指"这个文件/这个类本身"，任何类型的三态说明都覆盖得住。
_IDENTITY_QUALIFIERS = frozenset({"", "文件", "类"})
# 按证据类型分表的限定词：残余必须与**绑定到的 required item 类型**相容（四审 P1）。
# 三态说明只讲"生产源码:RagService 前轮已取得、被挤出"，它替代不了"RagService 的测试"
# 这条测试缺口；类型不符即原样保留。表里每个词都是 T22 `_TYPE_PATTERNS` 同类型的说法。
_TYPE_QUALIFIERS: dict[EvidenceType, frozenset[str]] = {
    "production_source": frozenset(
        {"生产源码", "生产实现", "生产代码", "实现代码", "源码", "实现", "代码"}
    ),
    "production_config": frozenset({"生产配置", "配置", "配置文件"}),
    "migration": frozenset({"数据库迁移", "迁移", "迁移文件", "迁移脚本"}),
    "design_doc": frozenset({"设计文档", "设计说明"}),
    "current_doc": frozenset({"当前文档", "文档"}),
    "test": frozenset({"测试", "测试代码"}),
    "historical_plan": frozenset({"计划文档", "规划文档", "路线图"}),
    "dev_log": frozenset({"开发日志", "dev-log"}),
    "frontend_source": frozenset({"前端源码", "前端代码", "前端组件"}),
    "other": frozenset(),
}
# 结构助词/方位词：与标点一起从残余里剥掉，不构成语义内容。
# **不含连接词**——把"与/和/及/或/、/，"当噪音抹掉，"RagService 和测试"就会退化成
# "测试"并被整条吞掉（四审 P1 根因）；它们改由 `_CONNECTOR` 单独 fail-closed。
_QUALIFIER_NOISE = re.compile(r"[\s的了中里内之。：:（）()\[\]\"'`]+")
# 协调连接词：紧挨目标出现即说明这句还在枚举**另一个**目标（"RagService 及迁移文件"），
# 而上面两张限定词表里的词都不含这些字符，故这道闸门不会误伤真正的纯身份缺口。
_CONNECTOR = re.compile(r"以及|或者|[与和及或、，,；;]")
# 连接词的"另一侧只剩这些字符"即无并列对象：句末逗号/分号不是连接词（五审 P1）
_PUNCT_EDGE = " \t\n。．.！!？?：:；;，,、"


def _has_coordinator(text: str, *, target_on_right: bool) -> bool:
    """这段紧邻目标的文本里，是否存在**真正起并列作用**的连接词。

    连接词靠目标的那一侧天然有内容（就是目标本身），故只判**背离目标的一侧**有没有
    真正的并列对象。两类东西不算并列对象：

    - 纯标点/空白——那只是句末或句首标点（"RagService 生产源码，"）。``ShortText``
      并未禁止句末标点，把它当连接词会让合法输出重新与"被挤出"三态说明并存（五审 P1）。
    - **可剥离的缺失措辞与范围前缀**——"证据中，RagService 的生产源码" 里的"证据中"
      是 T23 ``_ORPHAN_PREFIXES`` 明确要剥掉的范围限定，不是第二个目标；把它当并列
      对象会让"加不加逗号"改变吸收结果（六审 P1）。故用 T23 同一个去标记器判定，
      与后面算限定词用的是同一套口径。
    """
    for match in _CONNECTOR.finditer(text):
        outward = text[: match.start()] if target_on_right else text[match.end() :]
        if strip_absence_markers(outward).strip(_PUNCT_EDGE):
            return True
    return False


def _is_pure_identity_gap(text: str, item: RequiredEvidenceItem) -> bool:
    """整句是否只是"该目标本身 + 与其类型相容的通用限定词"——即确定性三态说明能
    完整替代的那种缺口。

    三道 fail-closed 闸门，缺一不可（四审 P1）：

    1. **目标计数**：文本里的每个目标 token 都必须指向 ``item`` 本身。只数
       ``iter_target_spans`` 不够——相接的跨度会被 ``merge_spans`` 合成一段，
       ``RagService.javaOrderService`` 这种无分隔符拼接只剩一段却含两个符号。
    2. **协调连接词**：目标左右两段各自判，**背离目标的一侧还有实词**才算并列——
       "RagService 和测试" / "RagService、设计文档" 不吸收，"RagService 生产源码，"
       的句末逗号不算（五审 P1）。T22 还能从"测试/迁移文件/设计文档"这类**类型词**
       解析出 type-only 目标，它们进不了 spans，只能靠这道与下一道闸门兜住。
    3. **类型相容**：残余限定词必须落在该 item 类型自己的表里；"RagService 的测试"
       绑到 production_source 的 R1 上时不得吸收——三态说明只覆盖生产源码。反向也成立：
       绑定 item 必有 path/symbol，T22 的 ``_build`` 在同类型已有点名项时会丢弃 type-only
       项，故"与绑定类型相容"恰好等价于"这个类型词不可能另有一条 required 目标"。

    先按原文位置摘掉目标，再用 T23 同一个去标记器剥掉缺失措辞与悬空前缀
    （"当前证据未覆盖 X 的生产源码" → "生产源码"）；该去标记器**不截断**，否则长条目
    可以把方法级语义藏在第 60 字之后骗过本判据。
    """
    spans = iter_target_spans(text)
    if len(spans) != 1 or not target_tokens_all_match(item, text):
        return False  # 零个目标无从绑定；还有别的目标就绝不整条吞掉
    start, end = spans[0]
    before, after = text[:start], text[end:]
    if _has_coordinator(before, target_on_right=True) or _has_coordinator(
        after, target_on_right=False
    ):
        return False
    qualifier = _QUALIFIER_NOISE.sub("", strip_absence_markers(before + after))
    return qualifier in _IDENTITY_QUALIFIERS or qualifier in _TYPE_QUALIFIERS[item.type]


def strip_items_contradicting_displaced_notes(
    items: list[NotFoundInput],
    required: RequiredEvidence,
    displaced_ids: frozenset[str],
) -> tuple[list[NotFoundInput], list[str]]:
    """LLM 报的缺口若**唯一绑定**到"前轮已取得、终态被挤出"的必需证据项，就只留确定性说明。

    二审发现3：已被挤出的 RagService 若被末轮报成"RagService 生产源码"，终态会同时出现
    原始缺失项与"前几轮已取得、受容量上限被挤出"的三态说明——两句话对同一方面给出相反
    印象（一句说没有、一句说取得过）。T23 的归一化全等比较建立不了跨轨身份（限定词一变
    即失效），故改用 T22 的路径/符号 matcher 绑定；一句话提了多个目标时保守保留原文。

    **只吸收被挤出这一态**：从未取得（两句同向、只是冗余）与仍在证据集但未引用
    （T23 的事实校验会把它改写成"已出现在本次检索证据中"）都不构成矛盾，继续走 T23，
    以免吞掉方法级细节和 `corpus_index` 这类事实校验来源（c10 要求两类缺口可分辨）。

    **只吸收"纯身份"缺口**（三审发现2 / 四审 P1）：判据见 ``_is_pure_identity_gap``——
    一个点名目标、两侧无协调连接词、残余限定词与该 item 的**类型相容**。
    "RagService 中 NO_ANSWER 的触发条件"含方法级语义、"RagService 和 UnknownService
    生产源码"含第二个目标（后者还不是 required item，只数 ``bound`` 的长度根本发现不了）、
    "RagService 及迁移文件"含 T22 认得的 type-only 目标、"RagService 的测试"与被挤出的
    生产源码项类型不符，全都必须原样保留——否则确定性说明覆盖不到的信息会被静默删除，
    直接回归 T23 的"方法级缺口信息保留"要求。
    """
    by_id = {required_item.item_id: required_item for required_item in required.items}
    kept: list[NotFoundInput] = []
    dropped: list[str] = []
    for item in items:
        bound = bound_required_ids(required, item.text)
        if (
            item.source != "deterministic"
            and len(bound) == 1
            and bound[0] in displaced_ids
            and _is_pure_identity_gap(item.text, by_id[bound[0]])
        ):
            dropped.append(item.text)
            continue
        kept.append(item)
    return kept, dropped


def citation_scope(
    text: str,
    *,
    cited_paths: Sequence[str],
    known_paths: Sequence[str],
) -> tuple[str, ...]:
    """条目的**全部可解析目标**是否都落在本次回答的交付引用里 → 命中的引用路径（E1）。

    资格按**整条原始条目**判定，不按 T23 切出的目标段判定：
    `RagService 和 DocumentRepository 的实现未找到` 实测会被切成 evidence_history +
    corpus_index 两段，逐段判定会让"只有部分目标被交付引用"的条目在第一段误命中
    （前审 PG-03）。

    **可解析目标** = 能匹配到任一已知 rel_path（交付引用 ∪ 全轮检索历史 ∪ 语料索引）的
    token。匹配不到任何已知路径的 token 既不参与也不阻断——词法会产出 `Conversation`
    这类泛化词与 `RagService.findConvers` 这类截断词，它们指认不了任何文件，用来
    卡判据只会让判据永不成立。

    判据按 token 的**全部**解析结果结算，不是"找到一个被引用的就算数"：`README` 同时
    指向根级 `README.md` 与 `docs/README.md` 时，只引用了后者并不能证明这条缺口说的是
    后者，据此宣称"已按引用事实更正"就成了不可证的断言（首审 T25.1-CR-01）。因此只要
    某个可解析目标的任一候选路径不在交付引用里，整条 fail-closed。
    """
    universe = list(dict.fromkeys([*cited_paths, *known_paths]))
    delivered = set(cited_paths)
    hits: list[str] = []
    for token in [*iter_path_tokens(text), *iter_symbol_tokens(text)]:
        resolved = [path for path in universe if path_matches_token(path, token)]
        if not resolved:
            continue
        if any(path not in delivered for path in resolved):
            return ()
        hits.extend(resolved)
    return tuple(dict.fromkeys(hits))


def is_identity_only_gap(text: str, rel_path: str) -> bool:
    """条目是否只是"该目标本身 + 与其类型相容的通用限定词"（E2）。

    与 T24 的 `_is_pure_identity_gap` 同口径、共用同一套闭合限定词表与噪音正则，
    区别只在锚点：那边绑定 `RequiredEvidenceItem`，这里绑定一条交付引用路径。
    连接词不在 `_QUALIFIER_NOISE` 里，故 `RagService 和测试` 的残余会落在表外 → False；
    类型不相容（生产源码的引用配上"测试"限定词）同样 False——这两类都必须原样保留原文，
    只能追加披露，不能把主语换成引用路径。
    """
    spans = iter_target_spans(text)
    if not spans:
        return False
    remainder: list[str] = []
    cursor = 0
    for start, end in spans:
        remainder.append(text[cursor:start])
        cursor = end
    remainder.append(text[cursor:])
    qualifier = _QUALIFIER_NOISE.sub("", strip_absence_markers("".join(remainder)))
    return (
        qualifier in _IDENTITY_QUALIFIERS or qualifier in _TYPE_QUALIFIERS[classify_path(rel_path)]
    )


# 终态严重度：一致性裁决只允许沿这个顺序**下调**（full → partial → refusal）
_MODE_RANK: dict[FinalMode, int] = {"refusal": 0, "partial": 1, "full": 2}


def finalize_consistency(
    mode: FinalMode,
    kept: Sequence[ClaimOutput],
    not_found: Sequence[str],
    answer_text: str,
) -> tuple[FinalMode, list[str]]:
    """终态五字段的结构前提兜底（T25.1）：只降不升。

    这些前提今天由 finalize 的分支顺序**隐式**保证，没有独立可判定的检查。显式化之后
    两件事成立：`full` 不可能带着缺口或零 claim 交付（此前 evaluator missing 会在
    `mode == "full"` 时被静默丢弃），`refusal` 的 claim/引用残留可被观测。

    只降不升由 `min(..., key=_MODE_RANK)` 结构性保证，不依赖分支写法。
    """
    warnings: list[str] = []
    resolved = mode
    if mode == "full" and not_found:
        warnings.append("finalize: full 终态存在未覆盖缺口，降级 partial")
        resolved = "partial"
    if mode == "full" and not kept:
        warnings.append("finalize: full 终态无结构化 claim，降级 partial")
        resolved = "partial"
    if mode == "refusal" and kept:
        warnings.append("finalize: refusal 终态残留结构化 claim")
    if mode == "refusal" and _EVIDENCE_MARK.search(answer_text):
        warnings.append("finalize: refusal 正文残留引用标记")
    return min(mode, resolved, key=lambda candidate: _MODE_RANK[candidate]), warnings


def regen_full_block(
    generate_calls: int,
    first: DraftSnapshot | None,
    draft_not_found: Sequence[str],
    evidence_ids: Sequence[str],
) -> tuple[bool, list[str]]:
    """T25.2 跨稿确定性复检：发生重生成时决定是否否决 ``full``（规格 §7 第 3 句）。

    动机（RT-08 的对偶）：``full_candidate`` 里的 ``not draft.not_found`` 读的是**最终
    那一稿**的自述，故第一稿声明缺口、第二稿把缺口删掉，就能把终态升成本项目最强的
    ``full``——而两次 generate 之间没有 retrieve 边，不可能有新证据进来。

    三个前置条件全部成立才否决：**P1** 确实发生了重生成、**P2** 两稿看到的是同一套
    证据、**P3** 第一稿声明过缺口而第二稿的**原始** ``not_found`` 为空。

    只做两件事：否决 ``full`` 与发 warning。**不新增、不删除、不改写任何一条
    ``final_not_found``**——"第一稿那条缺口仍然存在"是推不出来的（第二稿可能确实解决了
    它，只是没再声明），把它回填进用户可见清单等于用未经证明的话冒充事实。

    七行真值表（完整、互斥、穷尽，每行至多一条 warning）见任务合同；行 2
    （``generate_calls == 2`` 且快照缺失）在当前拓扑不可达，但类型上合法：此时 P2
    **不可求值**，并入"两稿证据集不同"会发出一句我们并不知道真假的断言，故单列并
    fail-closed 到不否决。判据因此不依赖"重生成必有快照"这条不变式成立。
    """
    if generate_calls != 2:  # 行 1：未发生重生成，跨稿复检整体不适用
        return False, []
    if first is None:  # 行 2：只陈述我们自己的状态，不碰两稿证据集
        return False, ["finalize: 发生重生成但第一稿快照缺失，跨稿复检不适用"]
    if first.evidence_ids != tuple(evidence_ids):  # 行 3
        return False, ["finalize: 两稿证据集不同，跨稿复检不适用"]
    second = tuple(draft_not_found)
    if first.not_found == second:  # 行 4/5：逐字相同（空与非空同理），无可披露的差分
        return False, []
    if first.not_found and not second:  # 行 6：P3 成立
        return True, [
            f"finalize: 第一稿声明过 {len(first.not_found)} 条未覆盖方面、第二稿未再声明，"
            "其间无新证据，据此不判 full"
        ]
    # 行 7：两稿都非空且不同。第二稿改写措辞即可让朴素集合差分同时报出一增一减，
    # 跨稿逐条身份不可确定性建立，故只披露条数并显式声明未作判定。
    return False, [
        f"finalize: 第一稿 not_found {len(first.not_found)} 条、第二稿 {len(second)} 条，"
        "两稿逐条对应关系未作判定"
    ]


def _with_citation_scope(
    item: NotFoundInput,
    cited_paths: Sequence[str],
    known_paths: Sequence[str],
) -> NotFoundInput:
    """给条目打上交付引用事实（E1/E2）；确定性条目由本模块自己生成，不参与。"""
    if item.source == "deterministic":
        return item
    scope = citation_scope(item.text, cited_paths=cited_paths, known_paths=known_paths)
    if not scope:
        return item
    return replace(
        item,
        cited_paths=scope,
        # 与**每一个**命中引用的类型都相容才算纯身份陈述（类型不相容即保留原文）
        identity_only=all(is_identity_only_gap(item.text, path) for path in scope),
    )


def _rebuild_answer_from_claims(kept: list[ClaimOutput]) -> str:
    """二次验证失败后由保留 claim 确定性重建正文，杜绝不可信句子/标记残留。

    claim.text 内嵌的 [E#] 未经 L0 检查，一律剔除；标记只由已通过 L0 的
    claim.evidence_ids 规范生成。
    """
    sentences = []
    for claim in kept:
        text = " ".join(_EVIDENCE_MARK.sub("", claim.text).split())
        marks = "".join(f"[{evidence_id}]" for evidence_id in claim.evidence_ids)
        sentences.append(f"{text.rstrip('。.')} {marks}。")
    return "".join(sentences)


@dataclass(frozen=True)
class AgentRuntime:
    llm: LLMClient
    retriever: Retriever
    max_evidences: int = MAX_FINAL_TOP_K
    # 本项目已摄取语料快照（documents 表 active 路径）：T23 的 not_found 事实校验与
    # 摄取覆盖判定用。默认 unknown = 未取得快照，与索引有关的判断一律 fail-closed。
    corpus: CorpusProfile = field(default_factory=CorpusProfile.unknown)

    def __post_init__(self) -> None:
        if not 1 <= self.max_evidences <= MAX_FINAL_TOP_K:
            raise ValueError(f"max_evidences 必须在 1..{MAX_FINAL_TOP_K}")


@dataclass(frozen=True)
class _CallResult[StructuredT: StrictModel]:
    value: StructuredT
    used_default: bool
    updates: dict[str, Any]
    warnings: list[str]


async def _structured_call[StructuredT: StrictModel](
    state: AgentState,
    *,
    llm: LLMClient,
    call_key: str,
    system: str,
    user: str,
    schema: type[StructuredT],
    default: StructuredT,
    recorder: TraceRecorder | None = None,
) -> _CallResult[StructuredT]:
    """单逻辑调用最多重问一次；每次请求先占用全局预算再调用供应商。

    每次实际发出的请求（含重问）逐条记入轨迹：分档 usage、单次 cost、时延与
    终态，run 汇总值可由这些明细复算（T18.2）。预算耗尽未发出的请求不记录。
    """
    calls = state["llm_calls"]
    retries = state["llm_retries"]
    retry_counts = dict(state["retry_counts"])
    tokens_in = state["tokens_in"]
    tokens_out = state["tokens_out"]
    cost = state["cost"]
    latency_ms = state["latency_ms"]
    model = state["model"]
    warnings: list[str] = []

    for attempt in range(MAX_REASKS_PER_CALL + 1):
        if calls >= MAX_LLM_REQUESTS:
            warnings.append(f"{call_key}:budget_exhausted")
            break
        calls += 1
        if attempt:
            retries += 1
            retry_counts[call_key] = retry_counts.get(call_key, 0) + 1

        started = time.perf_counter()
        try:
            result = await llm.complete(system=system, user=user)
        except Exception as exc:
            call_latency_ms = int((time.perf_counter() - started) * 1000)
            latency_ms += call_latency_ms
            warnings.append(f"{call_key}:request_failed:{type(exc).__name__}")
            if recorder is not None:
                recorder.record_llm_request(
                    call_key=call_key,
                    attempt=attempt + 1,
                    status="request_failed",
                    model=None,
                    usage={},
                    cost=None,
                    latency_ms=call_latency_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )
            continue
        call_latency_ms = int((time.perf_counter() - started) * 1000)
        latency_ms += call_latency_ms
        tokens_in += int(result.usage.get("prompt_tokens", 0))
        tokens_out += int(result.usage.get("completion_tokens", 0))
        call_cost = compute_cost(result.model, result.usage)
        cost = None if cost is None or call_cost is None else cost + call_cost
        model = result.model
        try:
            value = schema.model_validate_json(result.text)
        except ValidationError:
            warnings.append(f"{call_key}:invalid_structured_output")
            if recorder is not None:
                recorder.record_llm_request(
                    call_key=call_key,
                    attempt=attempt + 1,
                    status="invalid_output",
                    model=result.model,
                    usage=dict(result.usage),
                    cost=call_cost,
                    latency_ms=call_latency_ms,
                    error="invalid_structured_output",
                )
            continue
        if recorder is not None:
            recorder.record_llm_request(
                call_key=call_key,
                attempt=attempt + 1,
                status="ok",
                model=result.model,
                usage=dict(result.usage),
                cost=call_cost,
                latency_ms=call_latency_ms,
            )
        return _CallResult(
            value=value,
            used_default=False,
            updates={
                "llm_calls": calls,
                "llm_retries": retries,
                "retry_counts": retry_counts,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost": cost,
                "latency_ms": latency_ms,
                "model": model,
            },
            warnings=warnings,
        )

    return _CallResult(
        value=default,
        used_default=True,
        updates={
            "llm_calls": calls,
            "llm_retries": retries,
            "retry_counts": retry_counts,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost": cost,
            "latency_ms": latency_ms,
            "model": model,
        },
        warnings=[*warnings, f"{call_key}:default_applied"],
    )


def make_pg_retriever(
    session: AsyncSession,
    embedder: Embedder,
    *,
    top_k: int = 8,
) -> Retriever:
    """创建在线默认 vector-HNSW 多查询检索器；不启用 Hybrid lexical channel。"""
    if not 1 <= top_k <= MAX_FINAL_TOP_K:
        raise ValueError(f"top_k 必须在 1..{MAX_FINAL_TOP_K}")

    async def _retrieve(project_id: uuid.UUID, queries: tuple[str, ...]) -> list[Evidence]:
        rankings: list[ChannelRanking] = []
        catalog: dict[uuid.UUID, RetrievedChunk] = {}
        for query in dict.fromkeys(queries):
            try:
                hits = await retrieve(
                    session,
                    project_id,
                    query,
                    embedder=embedder,
                    top_k=top_k,
                    mode="hnsw",
                    ef_search=HNSW_EF_SEARCH,
                )
            except SQLAlchemyError:
                # DB 异常会使事务 aborted：先回滚恢复 session 再上抛，
                # 否则 retrieve 节点降级后轨迹/run 终态无法落库（T18.4 一致性）
                await session.rollback()
                raise
            rankings.append(ChannelRanking(query, "vector", tuple(hit.chunk_id for hit in hits)))
            for hit in hits:
                catalog.setdefault(hit.chunk_id, hit)
        fused = rrf_fuse(rankings)[:top_k]
        return [
            Evidence(
                evidence_id=f"E{index}",
                chunk_id=item.chunk_id,
                rel_path=catalog[item.chunk_id].rel_path,
                title_path=catalog[item.chunk_id].title_path,
                content=catalog[item.chunk_id].content,
                start_line=catalog[item.chunk_id].start_line,
                end_line=catalog[item.chunk_id].end_line,
                score=item.fused_score,
            )
            for index, item in enumerate(fused, start=1)
        ]

    return _retrieve


class AgentNodes:
    def __init__(self, runtime: AgentRuntime, recorder: TraceRecorder | None = None) -> None:
        self._runtime = runtime
        self._recorder = recorder

    async def plan(self, state: AgentState) -> dict[str, Any]:
        default = PlanOutput(intent="knowledge_qa", queries=[state["question"]])
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key="plan",
            system=prompts.PLAN_SYSTEM,
            user=prompts.build_plan_user(state["question"], state["required_evidence"]),
            schema=PlanOutput,
            default=default,
            recorder=self._recorder,
        )
        # plan 回显确认 required_evidence 的 item_id：须与权威集合完整一致；遗漏或
        # 出现非权威 id 都记诊断警告并忽略。权威来源是 state["required_evidence"]
        # （确定性解析），LLM 不得新增/伪造/漏报，也不影响确定性裁决。
        warnings = list(call.warnings)
        authoritative_ids = state["required_evidence"].item_ids
        reported_ids = set(call.value.required_evidence)
        if (authoritative_ids and reported_ids != authoritative_ids) or (
            reported_ids - authoritative_ids
        ):
            warnings.append("plan:required_evidence_mismatch")
        return {
            **call.updates,
            "plan": call.value,
            "queries": call.value.queries,
            "node_history": ["plan"],
            "warnings": warnings,
        }

    async def retrieve(self, state: AgentState) -> dict[str, Any]:
        if state["retrieval_round"] >= MAX_RETRIEVAL_ROUNDS:
            return {
                "node_history": ["retrieve"],
                "warnings": ["retrieve:round_limit_reached"],
            }
        tool_arguments = {"queries": clip_list(state["queries"])}
        started = time.perf_counter()
        try:
            fresh = await self._runtime.retriever(state["project_id"], tuple(state["queries"]))
        except Exception as exc:
            if self._recorder is not None:
                self._recorder.record_tool(
                    tool_name="retrieve",
                    status="failed",
                    arguments=tool_arguments,
                    result_summary=None,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            return {
                "retrieval_round": state["retrieval_round"] + 1,
                "evidences": state["evidences"],
                "node_history": ["retrieve"],
                "errors": [f"retrieve:{type(exc).__name__}"],
            }
        if self._recorder is not None:
            self._recorder.record_tool(
                tool_name="retrieve",
                status="ok",
                arguments=tool_arguments,
                result_summary={"result_count": len(fresh)},
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        # 补检结果优先，旧证据只用于填充剩余槽位；否则首轮 top-k 已满时
        # 第二轮新证据会被截断为零，形成“走了 refine 但证据没变”的假补检。
        # T24（RT-16）：但"整体优先"不等于"可以把已锚定的方面挤掉"——先按方面留位
        # 再填普通证据，被容量截断的逐条记录淘汰原因（案例九：补 CitationParser
        # 时丢掉第一轮的 RagService，覆盖 2→1 后整体假拒答）。
        merged: dict[uuid.UUID, Evidence] = {}
        for evidence in [*fresh, *state["evidences"]]:
            merged.setdefault(evidence.chunk_id, evidence)
        plan = plan_retention(
            state["required_evidence"],
            monotonic_matrix(state),
            [(item.chunk_id, item.rel_path) for item in fresh],
            [(item.chunk_id, item.rel_path) for item in state["evidences"]],
            limit=self._runtime.max_evidences,
        )
        evidences = [
            merged[chunk_id].model_copy(update={"evidence_id": f"E{index}"})
            for index, chunk_id in enumerate(plan.kept, start=1)
        ]
        return {
            "retrieval_round": state["retrieval_round"] + 1,
            "evidences": evidences,
            # 本轮召回过的路径进历史账本（即便随后被截断/被后轮挤出）：T23.2 的
            # "任一轮出现过的文件不得被写成不存在"必须看全轮历史，不能只看终态证据
            "evidence_path_history": list(dict.fromkeys(item.rel_path for item in fresh)),
            "node_history": ["retrieve"],
            # 逐条淘汰记录进状态与轨迹（汇总 warning 只是给人看的摘要，不能代替审计）
            "evidence_eliminations": list(plan.eliminated[:MAX_ELIMINATION_RECORDS]),
            "warnings": retention_warnings(plan),
        }

    async def evaluate(self, state: AgentState) -> dict[str, Any]:
        default = EvaluateOutput(
            sufficiency="insufficient",
            supported_aspects=[],
            missing_aspects=["证据充分性无法确认"],
        )
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key=f"evaluate:{state['retrieval_round']}",
            system=prompts.EVALUATE_SYSTEM,
            user=prompts.build_evaluate_user(
                state["question"], state["evidences"], state["required_evidence"]
            ),
            schema=EvaluateOutput,
            default=default,
            recorder=self._recorder,
        )
        # 确定性覆盖矩阵（按当前证据可用性）：权威，驱动 route/refine；LLM 覆盖报告
        # 只作诊断。报告须逐项完整、covered 与命中 evidence 都要与确定性矩阵对齐；
        # 遗漏/翻转/乱报 evidence 都记 mismatch 警告，但绝不改判确定性覆盖。
        cited = [(ev.evidence_id, ev.rel_path) for ev in state["evidences"]]
        coverage = compute_coverage(state["required_evidence"], cited)
        deterministic = {entry.item_id: entry for entry in coverage}
        authoritative_ids = state["required_evidence"].item_ids
        reported_ids = {report.item_id for report in call.value.coverage}
        warnings = list(call.warnings)
        mismatch = bool(authoritative_ids) and reported_ids != authoritative_ids
        for report in call.value.coverage:
            entry = deterministic.get(report.item_id)
            if entry is None or report.covered != entry.covered:
                mismatch = True
            elif set(report.evidence_ids) != set(entry.matched_evidence_ids):
                mismatch = True  # 自报命中的 evidence 与确定性矩阵不符（含遗漏/多报）
        if mismatch:
            warnings.append("evaluate:coverage_mismatch")
        return {
            **call.updates,
            "evaluation": call.value,
            "coverage": coverage,
            # 本轮方面观察进只增账本（T24）：确定性覆盖 + evaluate 自报已支持方面。
            # 后轮不得把任一轮已支持的方面凭空升级为缺失（T23.2/T24.1 单调性）。
            "aspect_observations": observe_round(
                state["required_evidence"],
                coverage,
                [
                    EvidenceRef(
                        evidence_id=evidence.evidence_id,
                        chunk_id=evidence.chunk_id,
                        rel_path=evidence.rel_path,
                    )
                    for evidence in state["evidences"]
                ],
                call.value.supported_aspects,
                round_index=state["retrieval_round"],
            ),
            "node_history": ["evaluate"],
            "warnings": warnings,
        }

    async def refine(self, state: AgentState) -> dict[str, Any]:
        evaluation = state["evaluation"]
        if evaluation is None:
            raise RuntimeError("refine 节点缺少 evaluation")
        default = RefineOutput(queries=state["queries"])
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key="refine",
            system=prompts.REFINE_SYSTEM,
            user=prompts.build_refine_user(
                state["question"],
                state["queries"],
                evaluation,
                uncovered_hints=[
                    item.symbol or item.path or item.anchor
                    for item in uncovered_items(state["required_evidence"], state["coverage"])
                ]
                # unresolved 约束的原文 anchor 也进补检提示（结构性 fail-closed 缺口）
                + [uc.anchor for uc in state["required_evidence"].unresolved_constraints],
            ),
            schema=RefineOutput,
            default=default,
            recorder=self._recorder,
        )
        return {
            **call.updates,
            "queries": call.value.queries,
            "refine_calls": state["refine_calls"] + 1,
            "refine_failed": call.used_default,
            "node_history": ["refine"],
            "warnings": call.warnings,
        }

    async def generate(self, state: AgentState) -> dict[str, Any]:
        evaluation = state["evaluation"]
        if evaluation is None:
            raise RuntimeError("generate 节点缺少 evaluation")
        if state["generate_calls"] >= MAX_GENERATE_CALLS:
            return {
                "generate_failed": True,
                "node_history": ["generate"],
                "warnings": ["generate:call_limit_reached"],
            }
        default = GenerateOutput(answer_text="现有证据不足以可靠生成回答。")
        call = await _structured_call(
            state,
            llm=self._runtime.llm,
            call_key=f"generate:{state['generate_calls'] + 1}",
            system=prompts.GENERATE_SYSTEM,
            user=prompts.build_generate_user(
                state["question"],
                state["evidences"],
                evaluation,
                verification_errors=state["verification_feedback"] or None,
                required_hints=[
                    f"{TYPE_LABELS[item.type]}:{item.symbol or item.path or item.anchor}"
                    for item in state["required_evidence"].items
                ]
                or None,
                # 禁止直接引用的类型也进 Prompt：确定性门是最终防线，但先让 generate
                # 避免踩线，否则只能被降级（五审发现4：否定约束此前是死信号）
                forbidden_citation_hints=[
                    TYPE_LABELS[etype]
                    for etype in state["required_evidence"].forbidden_citation_types
                ]
                or None,
            ),
            schema=GenerateOutput,
            default=default,
            recorder=self._recorder,
        )
        return {
            **call.updates,
            "answer_draft": call.value,
            # T25.2：第一稿在被第二稿覆写前留一份跨稿复检快照。格式/传输重问不推进
            # generate_calls，故重问不会写第二份；此后只读。
            "first_draft": (
                DraftSnapshot(
                    not_found=tuple(call.value.not_found),
                    evidence_ids=tuple(evidence.evidence_id for evidence in state["evidences"]),
                )
                if state["generate_calls"] == 0
                else state["first_draft"]
            ),
            "generate_calls": state["generate_calls"] + 1,
            "generate_failed": call.used_default,
            "node_history": ["generate"],
            "warnings": call.warnings,
        }

    async def verify(self, state: AgentState) -> dict[str, Any]:
        """确定性 L0/L1 验证（T17.4）；不通过时 errors 作为重生成反馈。"""
        draft = state["answer_draft"]
        if draft is None or state["generate_failed"]:
            verification = VerificationOutput(
                passed=False,
                l0_passed=False,
                l1_passed=False,
                errors=["verify:no_valid_draft"],
            )
            return {
                "verification": verification,
                "verification_feedback": [],
                "node_history": ["verify"],
            }
        verification = verify_draft(draft, state["evidences"])
        return {
            "verification": verification,
            "verification_feedback": [] if verification.passed else list(verification.errors),
            "node_history": ["verify"],
            "warnings": [] if verification.passed else ["verify:l0_l1_failed"],
        }

    async def finalize(self, state: AgentState) -> dict[str, Any]:
        """三态确定性收尾（规格 §10）：只读结构化状态，不发起任何 LLM 调用。

        结构：先按 draft/验证状态定出候选终态与正文，再对**所有分支**统一跑必需证据收尾
        （见 required_evidence_tail），最后才裁决 full。避免限制说明只出现在部分分支
        （五审发现5）。

        充分性（T24.1）取自**跨轮单调覆盖矩阵**而非末轮扁平 top-k：末轮 evaluate 因证据
        集合变化退化为 insufficient，不得抹掉前轮已取得直接证据的方面。矩阵只在存在确定性
        轨（required items）时接管裁决——没有可确定性判定的方面时，仍沿用末轮 evaluate 的
        自报充分性（P1 行为不变），避免把纯 LLM 报告升格为权威信号。
        """
        draft = state["answer_draft"]
        evaluation = state["evaluation"]
        verification = state["verification"]
        required = state["required_evidence"]
        missing = list(evaluation.missing_aspects) if evaluation else []
        warnings: list[str] = []
        kept: list[ClaimOutput] = []
        answer = ""
        answer_marks: list[int] = []  # 正文过 L0 后仍保留的有效引用编号（可见引用账本）
        full_candidate = False

        matrix = monotonic_matrix(state)
        outstanding = outstanding_missing(matrix, missing)
        displaced_ids = frozenset(
            row.aspect_id for row in displaced_aspects(matrix) if row.origin == "required_evidence"
        )
        if required.items:
            sufficient = is_monotonically_sufficient(matrix, outstanding=outstanding)
        else:
            sufficient = evaluation is not None and evaluation.sufficiency == "sufficient"
        for row in displaced_aspects(matrix):
            # 已支持方面的证据不在终态证据集里：仅被挤出不算证伪，矩阵保留已支持状态，
            # 但必须显式可见（否则"跨轮丢证据"又会变成不可诊断的静默降级）。
            # 只为**确定性轨**发这条告警：确定性轨的 present_now 来自权威覆盖矩阵，
            # "点名的文件不在终态证据集"是可核验的事实；evaluator 方面的 present_now
            # 建立在粗粒度的整轮绑定上（见 aspects.observe_round），据此对用户断言
            # "证据被挤出"会超出可证范围。
            if row.origin != "required_evidence":
                continue
            warnings.append(
                f"finalize: 方面 {row.aspect_id}({row.label}) 在第 {row.first_supported_round} "
                "轮已有直接证据，但未出现在终态证据集（被挤出，非证伪），按单调覆盖矩阵保留"
            )

        if draft is None:
            mode: FinalMode = "refusal"
            raw_not_found = _evaluator_items(missing)
        elif state["generate_failed"]:
            mode = "partial" if state["evidences"] else "refusal"
            # 冻结默认值/上一稿正文同样过 L0：越界标记不得随降级路径漏出
            answer, answer_marks, failed_l0_warnings = apply_l0(
                draft.answer_text, len(state["evidences"])
            )
            warnings.extend(failed_l0_warnings)
            raw_not_found = _draft_items(draft.not_found) + _evaluator_items(missing)
        else:
            failed = set(verification.failed_claims) if verification else set()
            kept = [claim for index, claim in enumerate(draft.claims) if index not in failed]
            removed = len(draft.claims) - len(kept)
            raw_not_found = _draft_items(draft.not_found) + _evaluator_items(missing)
            if removed:
                # 不可信 claim 的正文与其 [E#] 标记不得残留：正文按保留 claim 确定性重建
                warnings.append(
                    f"finalize: 已移除 {removed} 个未通过验证的 claim，正文按保留 claim 重建"
                )
                # 重建后再过一次确定性 L0，作为越界标记的最终防线
                answer, answer_marks, rebuild_l0_warnings = apply_l0(
                    _rebuild_answer_from_claims(kept), len(state["evidences"])
                )
                warnings.extend(rebuild_l0_warnings)
                mode = "partial" if kept else "refusal"
            else:
                answer, answer_marks, l0_warnings = apply_l0(
                    draft.answer_text, len(state["evidences"])
                )
                warnings.extend(l0_warnings)
                verification_ok = verification is None or verification.passed
                full_candidate = (
                    sufficient and bool(kept) and not draft.not_found and verification_ok
                )
                mode = "partial"
                if sufficient and not kept:
                    warnings.append("finalize: 充分判定但无结构化 claim，降级 partial")

        # T22 full 硬约束（结构性 fail-closed）：逐项确定性覆盖——每条已解析必需证据都须有
        # 匹配的**保留 claim** 直接引用，且无 unresolved 约束（解析器漏检/部分解析绝不
        # full），且**最终可见引用**里没有用户禁止直接引用的类型（正文裸标记也算可见引用，
        # 六审 P1）。测试/设计/历史材料因类型不同无法替代必需生产证据。
        evidence_by_id = {ev.evidence_id: ev for ev in state["evidences"]}
        support_citations = [
            (evidence_id, evidence_by_id[evidence_id].rel_path)
            for claim in kept
            for evidence_id in claim.evidence_ids
            if evidence_id in evidence_by_id
        ]
        visible_citations = _merge_citations(
            support_citations,
            [
                (f"E{num}", evidence_by_id[f"E{num}"].rel_path)
                for num in answer_marks
                if f"E{num}" in evidence_by_id
            ],
        )
        required_notes, required_warnings, satisfied = required_evidence_tail(
            required,
            support_citations,
            visible_citations,
            [(ev.evidence_id, ev.rel_path) for ev in state["evidences"]],
            matrix,
        )
        warnings.extend(required_warnings)
        # T25.2 跨稿复检：重生成缩小自述缺口不得把终态升成 full（判据见 regen_full_block）。
        # 输入取第二稿**原始** draft.not_found，与上面 full_candidate 的 not draft.not_found
        # 同源——T23 校准后的清单会混入 evaluator missing 与确定性说明，口径不同。
        regen_blocked, regen_warnings = regen_full_block(
            state["generate_calls"],
            state["first_draft"],
            draft.not_found if draft else [],
            [evidence.evidence_id for evidence in state["evidences"]],
        )
        warnings.extend(regen_warnings)
        if full_candidate and satisfied and not regen_blocked:
            mode = "full"

        # T23 确定性收尾：四分类 + 全轮历史事实校验（只读结构化状态，零 LLM 调用）。
        # 必须在 refusal 文案生成之前——拒答正文由校准后的缺口清单确定性拼出。
        raw_not_found, absorbed = strip_items_contradicting_displaced_notes(
            raw_not_found, required, displaced_ids
        )
        if absorbed:
            warnings.append("finalize: 缺失项已由确定性说明覆盖（" + "；".join(absorbed) + "）")
        # T25.1：交付引用账本（E0）——用户在 Answer 的 citations 里看到的就是这一套，
        # 正文独立 [E#] 同样算数（前审 PG-02）。已知路径全集用来判"这个目标可不可解析"，
        # 少了它，匹配不到交付引用的目标会被当成噪音跳过，整条 fail-closed 就形同虚设。
        cited_paths = list(dict.fromkeys(path for _evidence_id, path in visible_citations))
        known_paths = list(
            dict.fromkeys(
                [
                    *cited_paths,
                    *state["evidence_path_history"],
                    *(evidence.rel_path for evidence in state["evidences"]),
                    *sorted(self._runtime.corpus.indexed_paths),
                ]
            )
        )
        raw_not_found = [
            _with_citation_scope(item, cited_paths, known_paths) for item in raw_not_found
        ]
        calibration = calibrate_not_found(
            [*raw_not_found, *_deterministic_items(required_notes)],
            evidence_paths=state["evidence_path_history"],
            corpus=self._runtime.corpus,
            # 两轨的已支持方面都进来：evaluator 轨是 T23.2 原有输入；确定性轨（点名
            # 类/路径）同样不得在后轮被凭空报成缺失——只传诊断轨会让"RagService 已在
            # 第 1 轮取得"这类单调性事实对 not_found 不可见（复审发现2）
            supported_aspects_history=supported_labels(matrix),
        )
        not_found = list(calibration.texts)
        warnings.extend(calibration.warnings)

        # T25.1：终态五字段的结构前提兜底，只降不升。必须在 refusal 正文重建之前——
        # 重建后的模板文案恒无 [E#]，放在之后就永远检不出正文残留引用标记。
        mode, consistency_warnings = finalize_consistency(mode, kept, not_found, answer)
        warnings.extend(consistency_warnings)

        if mode == "refusal":
            answer = _refusal_text(not_found)
            kept = []
        return {
            "final_answer": answer,
            "final_mode": mode,
            "final_claims": kept,
            "final_not_found": not_found if mode != "full" else [],
            "final_not_found_details": calibration.details if mode != "full" else (),
            "status": "succeeded",
            "node_history": ["finalize"],
            "warnings": warnings,
        }
