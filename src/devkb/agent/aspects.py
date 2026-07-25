"""P1.5 T24：证据方面（aspect）分解、跨轮单调覆盖与 partial 保留（RT-16/20，规格 §6）。

三条纪律（全部确定性、零 LLM 调用）：

1. **方面分解**：用户要求被拆成可逐条判定的 aspect。**确定性轨**
   （``origin="required_evidence"``）来自 T22 的 required-evidence 解析——每条已解析
   必需证据即一个方面，身份是问题原文锚点与路径/符号；**诊断轨**
   （``origin="evaluator"``）来自各轮 evaluate 自报的 supported_aspects，只承担"已支持
   不得回退"的单调保留，永不用于放宽 full 门（裁决权在确定性代码，T22 用户裁决同口径）。
2. **跨轮保留**：合并新旧证据时先给"锚定某个方面"的证据留位（``plan_retention``），
   剩余槽位才轮到普通证据；被容量截断的证据逐条记录淘汰原因，其中"该方面在保留集中
   彻底失去直接证据"记为 ``aspect_anchor_capacity``，绝不静默消失（案例九：补检
   CitationParser 时把第一轮的 RagService 挤出，覆盖 2→1 后整体假拒答）。
3. **单调性**：覆盖只增不减。某方面一旦在任一轮取得直接证据即恒为 ``supported``；
   仅被后轮挤出不算"原证据被证伪"（P1.5 没有任何证伪机制，因此矩阵**不存在**降级
   路径），后轮 evaluate 把它报成缺失时只记告警并按已支持处理。

**能力边界（诚实声明）**：跨轮保留的位次保护只对"能确定性绑定到路径"的方面生效——
required-evidence 点名项，以及 evaluator 方面标签里出现的路径/类名 token（复用 T22 词法）。
纯自然语言方面（如"订单校验"）无法绑定文件，保留退化为原有的"新证据优先、旧证据补位"；
此时单调矩阵仍然阻止假拒答（RT-20），但无法阻止其证据被挤出（属检索层，RT-06/P1.6）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from devkb.agent.evidence_types import (
    CoverageEntry,
    RequiredEvidence,
    iter_path_tokens,
    iter_symbol_tokens,
    matching_item_ids,
    path_matches_token,
)

AspectOrigin = Literal["required_evidence", "evaluator"]
EliminationReason = Literal["aspect_anchor_capacity", "capacity_limit"]

_EVALUATOR_PREFIX = "evaluator:"
_LABEL_NOISE = re.compile(r"[\s。．.，,；;：:、！!？?（）()【】\[\]\"'`]+")


def normalize_aspect_label(text: str) -> str:
    """方面标签的比较口径（T23/T24 共用）：去空白与标点后全等比较。

    T23 用它判断"这条 not_found 是不是前轮已支持的方面"，T24 用它判断"末轮 missing
    里哪些方面从未被支持过"。两处必须同源，否则会出现"not_found 里被剔除、却仍按
    未满足缺口降级"的自相矛盾终态。
    """
    return _LABEL_NOISE.sub("", text)


class AspectObservation(BaseModel):
    """某一检索轮里"该方面已取得直接证据"的观察；缺失由矩阵反推，不单独落库。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    aspect_id: str
    label: str
    origin: AspectOrigin
    round_index: int
    # evidence_id 是轮内编号（每轮重排），跨轮无意义；历史只记稳定的 rel_path
    rel_paths: tuple[str, ...] = ()


class AspectStatus(BaseModel):
    """单调覆盖矩阵的一行：``supported`` 跨轮只增，``present_now`` 才是当前证据集。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    aspect_id: str
    label: str
    origin: AspectOrigin
    supported: bool
    first_supported_round: int = 0  # 0 = 从未取得直接证据
    present_now: bool = False
    rel_paths: tuple[str, ...] = ()


class EliminationRecord(BaseModel):
    """一条被跨轮合并淘汰的证据及其原因（T24.1 判据："记录淘汰原因"）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    chunk_id: uuid.UUID
    rel_path: str
    aspect_ids: tuple[str, ...]
    reason: EliminationReason


class RetentionPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kept: tuple[uuid.UUID, ...]
    # 为方面单调性从旧轮保下来的证据（否则会被本轮新证据挤出）
    retained_anchors: tuple[uuid.UUID, ...] = ()
    eliminated: tuple[EliminationRecord, ...] = ()


def _aspect_label(label: str) -> str:
    return label.strip()


def _evaluator_aspect_id(label: str) -> str:
    return _EVALUATOR_PREFIX + normalize_aspect_label(label)


def label_tokens(label: str) -> tuple[str, ...]:
    """方面标签里可用于路径绑定的 token（复用 T22 词法，不另建一套）。

    自然语言标签（"订单校验"）会得到空元组——绑定不到文件就不做位次保护，
    宁可退化为原有合并顺序，也不靠模糊匹配去猜哪条证据属于哪个方面。
    """
    return tuple(dict.fromkeys([*iter_path_tokens(label), *iter_symbol_tokens(label)]))


def observe_round(
    required: RequiredEvidence,
    coverage: Sequence[CoverageEntry],
    cited: Sequence[tuple[str, str]],
    evaluator_supported: Sequence[str],
    *,
    round_index: int,
) -> list[AspectObservation]:
    """把本轮"确定性覆盖 + evaluate 自报已支持"折成方面观察（只记已支持）。

    确定性轨用 ``coverage``（权威覆盖矩阵）而非 LLM 的 coverage 报告；诊断轨按
    归一化标签去重，同一方面在同轮重复自报只算一次。
    """
    path_by_id = dict(cited)
    item_by_id = {item.item_id: item for item in required.items}
    observations: list[AspectObservation] = []
    for entry in coverage:
        item = item_by_id.get(entry.item_id)
        if item is None or not entry.covered:
            continue
        observations.append(
            AspectObservation(
                aspect_id=item.item_id,
                label=item.symbol or item.path or item.anchor,
                origin="required_evidence",
                round_index=round_index,
                rel_paths=tuple(
                    dict.fromkeys(
                        path_by_id[evidence_id]
                        for evidence_id in entry.matched_evidence_ids
                        if evidence_id in path_by_id
                    )
                ),
            )
        )
    seen: set[str] = set()
    for label in evaluator_supported:
        key = normalize_aspect_label(label)
        if not key or key in seen:
            continue
        seen.add(key)
        observations.append(
            AspectObservation(
                aspect_id=_evaluator_aspect_id(label),
                label=_aspect_label(label),
                origin="evaluator",
                round_index=round_index,
            )
        )
    return observations


def build_matrix(
    required: RequiredEvidence,
    observations: Sequence[AspectObservation],
    coverage: Sequence[CoverageEntry],
    *,
    current_round: int,
) -> tuple[AspectStatus, ...]:
    """跨轮单调覆盖矩阵：确定性轨（按 required items 顺序）在前，诊断轨在后。

    ``supported`` = 任一轮观察到直接证据；``present_now`` = 当前证据集仍有直接证据。
    二者的差就是"被挤出但不算证伪"，由调用方记告警（绝不据此把方面改回缺失）。
    """
    by_id: dict[str, list[AspectObservation]] = {}
    for observation in observations:
        by_id.setdefault(observation.aspect_id, []).append(observation)
    covered_now = {entry.item_id for entry in coverage if entry.covered}
    rows: list[AspectStatus] = []
    for item in required.items:
        history = by_id.get(item.item_id, [])
        rows.append(
            AspectStatus(
                aspect_id=item.item_id,
                label=item.symbol or item.path or item.anchor,
                origin="required_evidence",
                supported=bool(history) or item.item_id in covered_now,
                first_supported_round=min(
                    (observation.round_index for observation in history), default=0
                ),
                present_now=item.item_id in covered_now,
                rel_paths=tuple(dict.fromkeys(path for obs in history for path in obs.rel_paths)),
            )
        )
    for aspect_id, history in by_id.items():
        if not aspect_id.startswith(_EVALUATOR_PREFIX):
            continue
        rows.append(
            AspectStatus(
                aspect_id=aspect_id,
                label=history[0].label,
                origin="evaluator",
                supported=True,  # 只在"自报已支持"时才产生观察
                first_supported_round=min(observation.round_index for observation in history),
                present_now=any(
                    observation.round_index == current_round for observation in history
                ),
            )
        )
    return tuple(rows)


def has_deliverable_aspect(matrix: Sequence[AspectStatus]) -> bool:
    """≥1 方面跨轮取得过直接证据 → 有可交付内容（T24.2：不得整体 refusal）。"""
    return any(row.supported for row in matrix)


def supported_labels(
    matrix: Sequence[AspectStatus], *, origin: AspectOrigin | None = None
) -> list[str]:
    """跨轮已支持方面的标签（按矩阵顺序，可按轨过滤）。"""
    return [
        row.label for row in matrix if row.supported and (origin is None or row.origin == origin)
    ]


def displaced_aspects(matrix: Sequence[AspectStatus]) -> tuple[AspectStatus, ...]:
    """已支持、但当前证据集里没有直接证据的方面——被挤出，不是被证伪。"""
    return tuple(row for row in matrix if row.supported and not row.present_now)


def outstanding_missing(
    matrix: Sequence[AspectStatus], missing_aspects: Iterable[str]
) -> list[str]:
    """末轮 missing 里**从未**被支持过的方面；已支持过的按单调性不算缺口。"""
    supported = {normalize_aspect_label(row.label) for row in matrix if row.supported}
    result: list[str] = []
    seen: set[str] = set()
    for label in missing_aspects:
        key = normalize_aspect_label(label)
        if not key or key in supported or key in seen:
            continue
        seen.add(key)
        result.append(label.strip())
    return result


def is_monotonically_sufficient(
    matrix: Sequence[AspectStatus], *, outstanding: Sequence[str]
) -> bool:
    """充分性按**单调矩阵**判定，而非末轮扁平 top-k（规格 §6）。

    要求：矩阵非空、每个方面都跨轮取得过直接证据、且末轮不存在从未被支持的缺口。
    矩阵为空（既无必需证据项也无自报已支持方面）时一律不充分——"没有任何方面被支持"
    绝不能因为"也没有任何方面被判缺失"而升格为 full。
    """
    return bool(matrix) and all(row.supported for row in matrix) and not outstanding


def aspect_ids_for_path(
    required: RequiredEvidence,
    protected: Sequence[AspectStatus],
    rel_path: str,
) -> tuple[str, ...]:
    """该路径锚定了哪些方面：必需项按 T22 matcher，evaluator 方面按标签 token 绑定。"""
    ids = list(matching_item_ids(required, rel_path))
    for row in protected:
        if row.origin != "evaluator" or row.aspect_id in ids:
            continue
        if any(path_matches_token(rel_path, token) for token in label_tokens(row.label)):
            ids.append(row.aspect_id)
    return tuple(ids)


def plan_retention(
    required: RequiredEvidence,
    protected: Sequence[AspectStatus],
    fresh: Sequence[tuple[uuid.UUID, str]],
    carried: Sequence[tuple[uuid.UUID, str]],
    *,
    limit: int,
) -> RetentionPlan:
    """跨轮证据合并：为"旧轮锚定、本轮未覆盖"的方面**预留槽位**，避免补检把已覆盖方面
    挤出（RT-16）。

    ``limit`` 个槽位按 ①新证据（原召回顺序，不重排）→ ②旧轮锚点 → ③其余旧证据 分配，
    其中 ②先扣除自己的槽位再让 ① 填充。两条纪律：

    - **本轮内不改顺序**：新证据的相对次序=召回排名，重排会改变 ``E#`` 编号进而改变
      模型该引哪条，属检索层行为，不在 T24 范围内。
    - **锚点最多占 ``limit-1`` 个槽位**：至少给本轮新证据留一个位置，否则补检可能完全
      看不见新结果；被挤掉的锚点记 ``aspect_anchor_capacity``，不静默消失。

    ``protected`` 是本轮之前的单调矩阵行（用于给 evaluator 方面做路径绑定）；同一方面
    的"最佳证据"取旧证据集中排名最高（即最靠前）的一条。
    """
    if limit < 1:
        raise ValueError("limit 必须 ≥1")
    path_of: dict[uuid.UUID, str] = {}
    for chunk_id, rel_path in [*fresh, *carried]:
        path_of.setdefault(chunk_id, rel_path)  # 同 chunk 重复出现时以新轮为准
    aspects_of = {
        chunk_id: aspect_ids_for_path(required, protected, rel_path)
        for chunk_id, rel_path in path_of.items()
    }
    fresh_ids = list(dict.fromkeys(chunk_id for chunk_id, _ in fresh))
    fresh_set = set(fresh_ids)
    carried_ids = [
        chunk_id
        for chunk_id in dict.fromkeys(chunk_id for chunk_id, _ in carried)
        if chunk_id not in fresh_set
    ]

    anchored = {aspect_id for chunk_id in fresh_ids for aspect_id in aspects_of[chunk_id]}
    wanted = [item.item_id for item in required.items] + [
        row.aspect_id for row in protected if row.origin == "evaluator" and row.supported
    ]
    anchors: list[uuid.UUID] = []
    for aspect_id in wanted:
        if aspect_id in anchored:
            continue
        for chunk_id in carried_ids:
            if aspect_id in aspects_of[chunk_id] and chunk_id not in anchors:
                anchors.append(chunk_id)
                anchored.update(aspects_of[chunk_id])
                break
    reserved = anchors[: max(limit - 1, 0)]
    kept = fresh_ids[: max(limit - len(reserved), 0)] + reserved
    kept_set = set(kept)
    for chunk_id in [*fresh_ids, *carried_ids]:  # 剩余槽位按原顺序补：先新证据再旧证据
        if len(kept) >= limit:
            break
        if chunk_id in kept_set:
            continue
        kept_set.add(chunk_id)
        kept.append(chunk_id)
    kept_aspects = {aspect_id for chunk_id in kept for aspect_id in aspects_of[chunk_id]}
    eliminated = tuple(
        EliminationRecord(
            chunk_id=chunk_id,
            rel_path=path_of[chunk_id],
            aspect_ids=aspects_of[chunk_id],
            reason=(
                "aspect_anchor_capacity"
                if set(aspects_of[chunk_id]) - kept_aspects
                else "capacity_limit"
            ),
        )
        for chunk_id in [*fresh_ids, *carried_ids]
        if chunk_id not in kept_set
    )
    return RetentionPlan(
        kept=tuple(kept),
        retained_anchors=tuple(chunk_id for chunk_id in reserved if chunk_id in kept_set),
        eliminated=eliminated,
    )


def retention_warnings(plan: RetentionPlan) -> list[str]:
    """淘汰原因的用户可见摘要：方面失锚必须显式可见，普通截断只报条数。

    失锚 warning 带 ``limit_reached`` 机器标记——它是"上限触顶导致的降级"，轨迹层
    （``derive_step_status``）据此把该 step 记为 degraded，与预算/轮次触顶同口径。
    """
    warnings: list[str] = []
    lost = [record for record in plan.eliminated if record.reason == "aspect_anchor_capacity"]
    if lost:
        aspect_ids = sorted({aspect_id for record in lost for aspect_id in record.aspect_ids})
        warnings.append(
            "retrieve:aspect_anchor_limit_reached: 容量上限使方面失去直接证据（"
            + ",".join(aspect_ids)
            + "）"
        )
    plain = len(plan.eliminated) - len(lost)
    if plain:
        warnings.append(f"retrieve: 证据因容量上限淘汰 {plain} 条（未影响已锚定方面）")
    return warnings
