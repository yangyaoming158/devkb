"""P1.5 T23：`not_found` 四分类与全轮历史事实校验（RT-02/15，规格 §5）。

三条纪律（全部确定性、零 LLM 调用）：

1. **分类**：四类枚举齐备，但 P1.5 只产出 ``missing_from_current_evidence`` 与
   ``unsupported_or_not_ingested``（策略类由 T27 的 policy-refusal 终态产出）。
   ``confirmed_undocumented`` 需要仓库级 inventory（属 P1.6），本模块**永不产出**——
   "全局不存在"在 P1.5 一律降级为"当前证据不足以穷举确认"。
2. **摄取覆盖判定**：``unsupported_or_not_ingested`` 只由 ``SUPPORTED_SUFFIXES`` +
   documents 表（``CorpusProfile``）+ 一张**有限且单测覆盖**的技术词→后缀映射表判定，
   不做开放式语言/意图推断。措辞一律条件式：只说"该格式不在摄取范围、未纳入索引"，
   绝不断言仓库是否包含此类文件（在线路径无法证明，仓库级普查属 P1.6）。
3. **事实校验**：出现在**任一检索轮**证据路径中的文件、或 documents 表中 active 的
   路径，不得被写成"不存在/未找到"；仓库级否定强断言（"源码中不存在"）无证据支撑时
   降级为条件式；前轮已支持的方面不得在后轮凭空升级为缺失。

被改写的原文保留在 ``NotFoundDetail.original_text`` 里，事实校验来源保留在
``basis``/``refs``，使每一次改写都可事后审计。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from devkb.agent.evidence_types import (
    iter_path_tokens,
    iter_symbol_tokens,
    path_matches_token,
)
from devkb.ingest.pipeline import SUPPORTED_SUFFIXES

NotFoundCategory = Literal[
    "missing_from_current_evidence",
    "unsupported_or_not_ingested",
    # 需确定性 inventory 支撑，属 P1.6：本模块永不产出（有单测锁定）
    "confirmed_undocumented",
    # 由 T27 policy-refusal 终态产出；不进入普通 not_found
    "forbidden_or_unavailable_by_policy",
]

NotFoundSource = Literal["generate_draft", "evaluator_missing", "deterministic"]

FactCheckBasis = Literal[
    "static_suffix_rule",  # SUPPORTED_SUFFIXES / 技术词→后缀映射
    "evidence_history",  # 全轮检索证据路径
    "corpus_index",  # documents 表 active 路径
    "unverifiable_assertion",  # 仓库级否定强断言无证据支撑 → 降级条件式
    "no_conflict_found",  # 与证据/索引无冲突，原文保留
]

# documents 表快照的硬上限：只用"命中"作事实，绝不用"未命中"下结论，故截断不会
# 产生错误断言；上限本身冻结为常量，避免大项目把整表读进内存。
MAX_CORPUS_PATHS = 5000

# 有限技术词 → 后缀映射（规格 §5 允许的封闭表；不做开放式语言/意图推断）。
# **只匹配缺口条目自身的文本**，不拿整个问题的技术词去套所有条目——否则"请根据数据库
# 迁移与 DocumentService 说明"会把已索引 Java 的召回缺口一并标成"格式未摄取"（c10 要求
# 两类缺口分开）。用户只在问题里点名、未给路径的场景由**确定性 required-evidence 说明**
# 覆盖：该说明本身由问题原文解析而来，文本里带类型标签（如"前端源码:Vue"）。
TERM_SUFFIX_HINTS: dict[str, tuple[str, ...]] = {
    "vue": (".vue",),
    "typescript": (".ts", ".tsx"),
    "tsx": (".tsx",),
    "前端源码": (".vue", ".ts", ".tsx"),
    "flyway": (".sql",),
    "数据库迁移": (".sql",),
    "迁移文件": (".sql",),
    "迁移脚本": (".sql",),
    "建表语句": (".sql",),
}

# 被承认为"真实文件扩展名"的封闭集合。file-token 词法（与 T22 共用）会把 Java 包名
# `com.example.repo` 也切成 `com.example`，若直接拿 `.example` 当后缀判定，就会把普通
# 的包名缺口误报成"格式未摄取"。故只有落在本集合里的扩展名才参与摄取覆盖判定；
# 其余一律退回 missing_from_current_evidence（保守方向）。
RECOGNIZED_FILE_EXTS: frozenset[str] = SUPPORTED_SUFFIXES | frozenset(
    {
        ".sql",
        ".vue",
        ".ts",
        ".tsx",
        ".jsx",
        ".js",
        ".py",
        ".go",
        ".kt",
        ".rb",
        ".rs",
        ".cs",
        ".php",
        ".scala",
        ".html",
        ".css",
        ".xml",
        ".json",
        ".sh",
        ".gradle",
    }
)

# 隐藏文件/目录（路径任一段以 . 开头）恒不在摄取范围——`ingest.scan_files` 直接跳过。
# 前置 (?<![\w.]) 保证只匹配真正的点开头 token（`.env.example`），不吃 `com.example`。
_DOTFILE_TOKEN = re.compile(r"(?<![\w.])\.[A-Za-z][A-Za-z0-9_.\-/]*")

# 仓库级否定强断言：P1.5 无 inventory，一律降级为条件式（规格 §5）
_REPO_LEVEL_NEGATION = (
    "不存在",
    "并未存在",
    "没有实现",
    "未实现",
    "无此",
    "查无",
    "没有该文件",
    "无该文件",
    "无相关实现",
    "均未提供",
    "从未提供",
    "完全缺失",
)
# 穷举量词：与缺失表述同现即构成"全库范围"断言（"未找到任何其他 Controller"），
# 这在 P1.5 同样无从证明（需 inventory，属 P1.6）→ 降级为"当前证据不足以穷举确认"。
_EXHAUSTIVE_QUANTIFIERS = ("任何", "所有", "全部", "一切", "每个", "各个")
# 只在穷举量词同现时才当否定看的弱否定词：单独出现（"OrderService 中没有事务注解"）
# 是可由证据支撑的具体结论，不属于全库断言，不改写。
_SOFT_NEGATION = ("没有", "未", "无", "缺")
# 改写后残留的悬空前缀（"当前证据未覆盖 X" 去标记后剩 "当前证据 X"）：封闭表，只清首部
_ORPHAN_PREFIXES = ("当前证据", "证据中", "证据里", "源码中", "代码中", "仓库中", "项目中")
# 证据范围内的缺失表述：本身合法，但若所指对象确实出现在证据/索引中即为事实错误
_ABSENCE_MARKERS = (
    "未找到",
    "没有找到",
    "找不到",
    "未提供",
    "未给出",
    "缺少",
    "缺失",
    "未召回",
    "未覆盖",
    "未包含",
    "未出现",
)

_PUNCT_STRIP = re.compile(r"[\s。．.，,；;：:、！!？?（）()【】\[\]\"'`]+")
_MAX_SUBJECT_CHARS = 60
_ANCHOR_TRIM = " \t的了：:，,。、；;和与及或\n"


class NotFoundDetail(BaseModel):
    """一条 not_found 的分类与事实校验记录（Answer JSON 平行字段 not_found_details）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    text: str
    category: NotFoundCategory
    source: NotFoundSource
    basis: FactCheckBasis
    refs: tuple[str, ...] = ()
    original_text: str | None = None


@dataclass(frozen=True)
class CorpusProfile:
    """本项目已摄取语料的只读快照（documents 表 active 路径）。

    ``known=False`` 表示未取得快照（离线图测试/加载失败）：此时与索引有关的一切判断
    fail-closed，既不声称"已索引"也不声称"未索引"。**"未命中"永不作为结论依据**，
    因此 ``truncated`` 也不会带来错误断言。
    """

    indexed_paths: frozenset[str] = frozenset()
    known: bool = False
    truncated: bool = False

    @classmethod
    def unknown(cls) -> CorpusProfile:
        return cls()

    @classmethod
    def from_paths(cls, paths: Iterable[str], *, limit: int = MAX_CORPUS_PATHS) -> CorpusProfile:
        collected = list(dict.fromkeys(paths))
        return cls(
            indexed_paths=frozenset(collected[:limit]),
            known=True,
            truncated=len(collected) > limit,
        )

    @property
    def indexed_suffixes(self) -> frozenset[str]:
        return frozenset(
            "." + path.rsplit(".", 1)[-1].lower() for path in self.indexed_paths if "." in path
        )

    def matching_paths(self, token: str) -> tuple[str, ...]:
        return tuple(sorted(path for path in self.indexed_paths if path_matches_token(path, token)))

    def ingests_suffix(self, suffix: str) -> bool:
        """该后缀是否属于摄取范围（静态规则；索引中确实存在同后缀文档时同样算摄取）。"""
        return suffix.lower() in SUPPORTED_SUFFIXES or suffix.lower() in self.indexed_suffixes


@dataclass(frozen=True)
class NotFoundInput:
    text: str
    source: NotFoundSource


@dataclass(frozen=True)
class NotFoundCalibration:
    texts: tuple[str, ...]
    details: tuple[NotFoundDetail, ...]
    warnings: tuple[str, ...]


def coverage_disclosure() -> str:
    """静态摄取覆盖披露（后缀级，与 SUPPORTED_SUFFIXES 同源）。"""
    return "本项目当前摄取的文件类型：" + "、".join(sorted(SUPPORTED_SUFFIXES))


def _normalize(text: str) -> str:
    return _PUNCT_STRIP.sub("", text)


def _strip_absence_markers(text: str) -> str:
    """去掉缺失/否定标记，只留"是哪个方面"。

    改写时用它作主语：既让被校准的条目不再带"未找到/不存在"字样（Gate 明确要求
    已召回或已索引的路径不得被写成未找到/不存在），又保住"哪个方法/字段没覆盖"的
    信息——直接换成模板会把方法级缺口整条吃掉。
    """
    stripped = text
    for marker in (*_REPO_LEVEL_NEGATION, *_ABSENCE_MARKERS):
        stripped = stripped.replace(marker, "")
    stripped = " ".join(stripped.split()).strip(_ANCHOR_TRIM)
    changed = True
    while changed:  # 去标记后可能剩下悬空前缀（"当前证据 X"）
        changed = False
        for prefix in _ORPHAN_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip(_ANCHOR_TRIM)
                changed = True
    return stripped[:_MAX_SUBJECT_CHARS]


def _subject(text: str, tokens: Sequence[str]) -> str:
    aspect = _strip_absence_markers(text)
    if aspect:
        return aspect
    if tokens:
        return "、".join(tokens[:3])
    return "该方面"


def _suffix_of(token: str) -> str:
    base = token.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _term_signals(text: str, corpus: CorpusProfile) -> list[str]:
    haystack = text.lower()
    return [
        suffix
        for term, mapped in TERM_SUFFIX_HINTS.items()
        if term in haystack and all(not corpus.ingests_suffix(s) for s in mapped)
        for suffix in mapped
    ]


def _uningested_signals(text: str, corpus: CorpusProfile) -> tuple[str, ...]:
    """缺口条目**自身**给出的未摄取信号：显式路径后缀 + 隐藏文件 + 封闭技术词表。"""
    suffixes = [
        suffix
        for token in iter_path_tokens(text)
        if (suffix := _suffix_of(token)) in RECOGNIZED_FILE_EXTS
        and not corpus.ingests_suffix(suffix)
    ]
    suffixes.extend(
        match.group(0).split("/", 1)[0]
        for match in _DOTFILE_TOKEN.finditer(text)
        if not corpus.matching_paths(match.group(0))
    )
    suffixes.extend(_term_signals(text, corpus))
    return tuple(sorted(dict.fromkeys(suffixes)))


def _seen_paths(token: str, paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path for path in paths if path_matches_token(path, token)))


def calibrate_not_found(
    items: Sequence[NotFoundInput],
    *,
    evidence_paths: Sequence[str],
    corpus: CorpusProfile,
    supported_aspects_history: Sequence[str] = (),
) -> NotFoundCalibration:
    """确定性分类 + 事实校验：返回用户可见文案、平行明细与告警。

    ``evidence_paths`` 必须是**全部检索轮**出现过的证据路径（不只是最后一轮），
    ``supported_aspects_history`` 是**前若干轮** evaluate 判为已支持的方面。
    分类只看条目自身文本（见 ``TERM_SUFFIX_HINTS`` 注释），不按整题技术词一刀切。
    """
    supported = {_normalize(aspect) for aspect in supported_aspects_history if aspect.strip()}
    texts: list[str] = []
    details: list[NotFoundDetail] = []
    warnings: list[str] = []
    dropped: list[str] = []
    uningested_all: set[str] = set()
    weakened = 0

    for item in items:
        raw = item.text.strip()
        if not raw:
            continue
        if item.source != "deterministic" and _normalize(raw) in supported:
            # 前轮已支持的方面不得凭空升级为缺失（原证据被证伪才可退化，P1.5 无证伪机制）
            dropped.append(raw)
            continue

        tokens = [*iter_path_tokens(raw), *iter_symbol_tokens(raw)]
        rewritable = item.source != "deterministic"
        has_absence = any(marker in raw for marker in (*_ABSENCE_MARKERS, *_REPO_LEVEL_NEGATION))
        exhaustive = any(q in raw for q in _EXHAUSTIVE_QUANTIFIERS) and (
            has_absence or any(n in raw for n in _SOFT_NEGATION)
        )
        # 全局否定 = 仓库级断言：无 inventory 时同样只能降级（"未找到任何其他 Controller"）
        has_repo_negation = exhaustive or any(marker in raw for marker in _REPO_LEVEL_NEGATION)

        in_evidence: list[str] = []
        in_index: list[str] = []
        matched_tokens: list[str] = []
        for token in tokens:
            seen = _seen_paths(token, evidence_paths)
            indexed = corpus.matching_paths(token) if corpus.known else ()
            if seen or indexed:
                matched_tokens.append(token)
            in_evidence.extend(seen)
            in_index.extend(path for path in indexed if path not in seen)
        in_evidence = list(dict.fromkeys(in_evidence))
        in_index = list(dict.fromkeys(in_index))

        uningested = _uningested_signals(raw, corpus)
        category: NotFoundCategory = (
            "unsupported_or_not_ingested" if uningested else "missing_from_current_evidence"
        )

        clauses: list[str] = []
        basis: FactCheckBasis = "no_conflict_found"
        refs: tuple[str, ...] = ()
        if rewritable and has_absence and in_evidence:
            clauses.append(
                f"该目标已出现在本次检索证据中（{'、'.join(in_evidence[:3])}），"
                "属当前证据未展开的部分；当前证据不足以支撑该方面的完整结论"
            )
            basis = "evidence_history"
            refs = tuple(in_evidence[:3])
        elif rewritable and has_absence and in_index:
            clauses.append(
                f"该路径已在当前项目索引中（{'、'.join(in_index[:3])}，状态 active），"
                "本轮未被召回，属当前证据缺口"
            )
            basis = "corpus_index"
            refs = tuple(in_index[:3])
        if uningested:
            uningested_all.update(uningested)
            clauses.append(
                f"{'、'.join(uningested)} 不在当前摄取范围、未纳入本项目索引，"
                "因此当前证据中不会出现；无法据此确认仓库是否包含此类文件"
            )
            if basis == "no_conflict_found":
                basis = "static_suffix_rule"
                refs = uningested
        if (
            rewritable
            and has_repo_negation
            and basis in ("no_conflict_found", "static_suffix_rule")
        ):
            # 仓库级否定断言在 P1.5 一律无法证明（无 inventory）：降级为条件式
            clauses.append(
                ("当前证据不足以穷举确认" if exhaustive else "当前证据不足以确认该结论")
                + "；是否在仓库中存在需仓库级核验，P1.5 不做此判定"
            )
            weakened += 1
            if basis == "no_conflict_found":
                basis = "unverifiable_assertion"

        # 改写口径：**本轮证据里出现过、或被仓库级否定断言点到的条目整条改写**——
        # Gate 明确要求已召回/已索引路径不得被写成"未找到/不存在"，故这两类的缺失字样
        # 必须消失（主语用去标记后的方面名，保住"哪个方法/字段"）。其余（已索引未召回、
        # 格式未摄取）原文成立，只追加确定性说明。
        replace = rewritable and (
            bool(has_absence and (in_evidence or in_index)) or has_repo_negation
        )
        if clauses and replace:
            text = f"{_subject(raw, matched_tokens or tokens)}：" + "；".join(clauses)
            original: str | None = raw
        elif clauses:
            text = f"{raw}（{'；'.join(clauses)}）"
            original = raw
        else:
            text = raw
            original = None
        if text in texts:
            continue
        texts.append(text)
        details.append(
            NotFoundDetail(
                text=text,
                category=category,
                source=item.source,
                basis=basis,
                refs=refs,
                original_text=original,
            )
        )

    if dropped:
        warnings.append(
            "finalize: 前轮已支持方面被后轮报为缺失，已剔除（" + "；".join(dropped) + "）"
        )
    for basis_key, label in (
        ("evidence_history", "全轮证据中出现过的路径被写成缺失"),
        ("corpus_index", "已索引 active 路径被写成缺失"),
    ):
        hits = list(
            dict.fromkeys(
                ref for detail in details if detail.basis == basis_key for ref in detail.refs
            )
        )
        if hits:
            warnings.append(f"finalize: not_found 事实校验修正——{label}（" + ",".join(hits) + "）")
    if weakened:
        warnings.append(
            f"finalize: not_found 含无证据支撑的仓库级否定断言，已降级为条件式（{weakened} 条）"
        )
    if uningested_all:
        warnings.append(
            "finalize: 存在未摄取格式导致的缺口（" + ",".join(sorted(uningested_all)) + "）"
        )
    return NotFoundCalibration(texts=tuple(texts), details=tuple(details), warnings=tuple(warnings))
