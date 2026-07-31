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

一条 not_found **只能有一个原因**（用户裁决 2026-07-25）。LLM 把两类缺口写进一句时
按连接词确定性拆分，拆分口径见 ``_cut_between``：切点只可能落在**两个可识别目标之间**，
且只认括号外的标点、或"整个间隙就是一个连接词"这两种可证情形——中文里"及/与/或/和"
同时是"涉及/提及/参与/与否/或者说/和谐"的组成部分，靠屏蔽词表穷举必然漏词（第三轮
复审已证实），故其余一律不切：同条内按优先级取唯一原因、原文完整保留，被压住的未摄取
信号进 warnings，绝不静默消失。

被改写的原文保留在 ``NotFoundDetail.original_text`` 里，事实校验来源保留在
``basis``/``refs``，使每一次改写都可事后审计。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict

from devkb.agent.aspects import normalize_aspect_label
from devkb.agent.evidence_types import (
    iter_path_tokens,
    iter_symbol_tokens,
    iter_target_spans,
    merge_spans,
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

# 技术词的统一词法：ASCII 词加英文词边界（避免 revue 命中 vue），中文词直接匹配。
# 摄取覆盖判定与目标切分共用它，保证"算不算一个目标"两处口径一致。
_TERM_TARGET = re.compile(
    "|".join(
        (rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])" if term.isascii() else re.escape(term))
        for term in sorted(TERM_SUFFIX_HINTS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)

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

# T28.1 静态覆盖披露里举例的"不摄取后缀"（封闭表，仅用于文案）。两条纪律由单测常驻
# 守着：与 SUPPORTED_SUFFIXES 不相交（扩摄取范围时**转红**，而不是让文案静默变假）、
# 且 ⊆ RECOGNIZED_FILE_EXTS（披露说不摄取的后缀，T23 必须也能判成未摄取，两处同口径）。
# 选词只收 dev 语料真实出现且已被冻结预期点名的格式（.vue/.ts/.tsx/.sql，见 c03/c10/e05）
# 加四个常见前后端格式作泛化示例；文案逐字带"等"，不把这张表当成穷举清单。
UNINGESTED_EXAMPLES: tuple[str, ...] = (
    ".vue",
    ".ts",
    ".tsx",
    ".sql",
    ".js",
    ".py",
    ".html",
    ".css",
)
COVERAGE_DISCLOSURE_PREFIX = "（本项目摄取范围："

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

# 目标段连接词（封闭表）：只用于"一条一原因"的确定性拆分。
# 中文标点不可能出现在词内，故括号外的标点是**无歧义**的连接处；其余连接词同时都是
# 常见复合词的组成部分（涉及/提及/普及/参与/与否/与此/或者说/和谐…），无法靠"哪些词
# 不能切"的屏蔽表穷举——第三轮复审已证明该路线会持续漏词。故改为**两侧目标信号**定位：
# 只有当连接词位于两个可识别目标之间、且它自己就是这两个目标之间的全部内容时，才算
# 连接处；否则保守不切（宁可少拆一条，也不切碎原文）。
_PUNCT_CONNECTORS = ("、", "，", ",", "；", ";")
_WORD_CONNECTORS = ("以及", "或者", "与", "及", "或", "和")
# 主语渲染长度上限：**只作用于用户可见文案**，不得用在分类/安全判据上（四审 P1）
_MAX_SUBJECT_CHARS = 60
# 边界修剪字符集：只含空白/标点/结构助词，**不含连接词**——把"与/及/或/和"当边界字符
# 盲删会把"与此同时 X"削成"此同时 X"（第三轮复审阻断点2 的成因之一）。
_EDGE_TRIM = " \t的了：:，,。、；;\n"


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
    # T25.1：该条目的**全部可解析目标**都落在"本次回答实际交付的引用来源"里时，由
    # finalize 填入命中的 rel_path（E0∩E1，判定见 nodes.citation_scope）。空元组表示
    # 不适用，走 T23 原路径、输出逐字不变。
    cited_paths: tuple[str, ...] = ()
    # 该条目是否只是"目标本身 + 与其类型相容的通用限定词"（E2，见 nodes.is_identity_only_gap）
    identity_only: bool = False


@dataclass(frozen=True)
class NotFoundCalibration:
    texts: tuple[str, ...]
    details: tuple[NotFoundDetail, ...]
    warnings: tuple[str, ...]


def coverage_disclosure() -> str:
    """静态摄取覆盖披露（T28.1，规格 §10）——refusal/partial 正文附带的固定文案。

    目的：让"这个格式没被摄取"与"仓库确实没有这类源码"在用户端可分。T23 的
    ``static_suffix_rule`` 子句只在**某条缺口自身**命中后缀/隐藏文件/技术词时才出现；
    模型换个说法就没有了。本文案与本题命中与否无关，是静态兜底。

    **四句话的论域**（前审 PG-T281-01 划定，越界即为不可证断言）：

    1. 管线支持集——``scan_files`` 逐字按 ``SUPPORTED_SUFFIXES`` 过滤，是代码事实；
    2. 管线不支持示例——同一常量取补集，``UNINGESTED_EXAMPLES`` 是其中的封闭举例；
    3. 条件式（"若未出现在证据中，可能只是…"）——不断言本次证据里有没有；
    4. 免责——P1.5 无 inventory，说不出仓库有没有。

    因此本函数**零参数、零 I/O、零项目查询**：它说不出"本项目索引里有没有 X"这类实例
    事实（那是 ``CorpusProfile`` 的活，且 ``ingests_suffix`` 会把已在索引中的后缀视为
    已摄取），也说不出用户问的是什么语言（逐题推断属 P1.6）。
    """
    ingested = "、".join(sorted(SUPPORTED_SUFFIXES))
    examples = "、".join(sorted(UNINGESTED_EXAMPLES))
    return (
        f"{COVERAGE_DISCLOSURE_PREFIX}自动摄取管线只处理 {ingested}；"
        f"{examples} 等其他类型不在自动摄取范围内。"
        "此类文件若未出现在证据中，可能只是未被摄取，不足以据此确认仓库是否包含此类文件。）"
    )


def strip_absence_markers(text: str) -> str:
    """去掉缺失/否定标记，只留"是哪个方面"。

    改写时用它作主语：既让被校准的条目不再带"未找到/不存在"字样（Gate 明确要求
    已召回或已索引的路径不得被写成未找到/不存在），又保住"哪个方法/字段没覆盖"的
    信息——直接换成模板会把方法级缺口整条吃掉。

    **不截断**：本函数也是安全判据的输入（T24 的"纯身份缺口"判定），读被截短的文本
    会让 500 字缺口条目把方法级语义藏到第 ``_MAX_SUBJECT_CHARS`` 字之后骗过判据
    （四审 P1）。长度上限只属用户可见渲染，落在 ``_subject``。
    """
    stripped = text
    for marker in (*_REPO_LEVEL_NEGATION, *_ABSENCE_MARKERS):
        stripped = stripped.replace(marker, "")
    stripped = _trim_connectors(" ".join(stripped.split()).strip(_EDGE_TRIM))
    changed = True
    while changed:  # 去标记后可能剩下悬空前缀（"当前证据 X"）
        changed = False
        for prefix in _ORPHAN_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip(_EDGE_TRIM)
                changed = True
    return stripped


def _subject(text: str, tokens: Sequence[str]) -> str:
    """用户可见的主语：此处（且只有此处）才按 ``_MAX_SUBJECT_CHARS`` 截断。"""
    aspect = strip_absence_markers(text)[:_MAX_SUBJECT_CHARS]
    if aspect:
        return aspect
    if tokens:
        return "、".join(tokens[:3])
    return "该方面"


def _suffix_of(token: str) -> str:
    base = token.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _term_signals(text: str, corpus: CorpusProfile) -> list[str]:
    hits = {match.group(0).lower() for match in _TERM_TARGET.finditer(text)}
    return [
        suffix
        for term, mapped in TERM_SUFFIX_HINTS.items()
        if term in hits and all(not corpus.ingests_suffix(s) for s in mapped)
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


@dataclass(frozen=True)
class _Markers:
    """整条缺口语句级别的否定/缺失标记；对其下每个目标段一律适用。"""

    rewritable: bool
    has_absence: bool
    has_repo_negation: bool
    exhaustive: bool
    # T25.1：交付引用事实同样是**整条**语句级的。放在这里而不是逐段计算——同一条目会按
    # 目标被切成多段多原因（`RagService 和 DocumentRepository 的实现未找到` 实测切为
    # evidence_history + corpus_index 两段），逐段判定会让"只有部分目标被交付引用"的
    # 条目在第一段误命中（前审 PG-03）。
    cited_paths: tuple[str, ...] = ()
    identity_only: bool = False


@dataclass(frozen=True)
class _Assessment:
    """一个目标段的确定性结论：**恰好一个原因**（category + basis + refs）。"""

    segment: str
    category: NotFoundCategory
    basis: FactCheckBasis
    refs: tuple[str, ...]
    replace: bool
    weakened: bool
    # T25.1：该段目标是本次回答的交付引用来源（比"出现在某轮检索证据里"更强、也更贴近
    # 用户看到的 citations）。只影响文案与告警，不改 category/basis 值域。
    delivered: bool = False
    identity_only: bool = False

    @property
    def reason(self) -> tuple[NotFoundCategory, FactCheckBasis]:
        return (self.category, self.basis)


@dataclass(frozen=True)
class _Run:
    """相邻同原因目标段合并后的一段：区间取自原文，连接词随之原样保留。"""

    assessment: _Assessment
    start: int
    end: int
    refs: tuple[str, ...]


def _render_refs(refs: Sequence[str]) -> str:
    """事实来源在文案里最多列 3 条（超出记"等 N 条"），但 refs 本身**不截断**——
    告警与审计要能看到同一原因下的每一个目标（复审阻断点1）。"""
    shown = "、".join(refs[:3])
    return f"{shown} 等 {len(refs)} 条" if len(refs) > 3 else shown


def _clause(
    basis: FactCheckBasis,
    refs: Sequence[str],
    *,
    exhaustive: bool,
    delivered: bool = False,
    identity_only: bool = False,
) -> str:
    """由**合并后的**事实来源渲染确定性说明；空串表示无需说明。

    T25.1 的两条 delivered 文案**只陈述可证事实**：某文件是本次回答的引用来源之一，
    以及（identity_only 时）本条"未找到"与该交付引用不符。确定性代码无法证明"该缺口
    所述命题已被正文支撑"（`path_matches_token` 只判文件身份、L1 只判引文逐字子串），
    也无法证明"该命题与正文相冲突"（正文可能压根没谈这个命题），故两类结论一律不写。
    """
    if delivered:
        if identity_only:
            # 不回引原来的缺失措辞：交付文案里一旦出现"未找到"字样，扫描式核对
            # （U14/T31）就无法区分"断言缺失"与"引用缺失措辞"，Gate 的可判性会下降
            return (
                f"该文件是本次回答的引用来源之一（{_render_refs(refs)}），"
                "原缺口陈述与交付引用不符，已按引用事实更正"
            )
        return (
            f"该文件是本次回答的引用来源之一（{_render_refs(refs)}）；"
            "本条缺口由生成模型自述，未经确定性核验，不代表该文件未被引用"
        )
    if basis == "evidence_history":
        return (
            f"该目标已出现在本次检索证据中（{_render_refs(refs)}），"
            "属当前证据未展开的部分；当前证据不足以支撑该方面的完整结论"
        )
    if basis == "corpus_index":
        return (
            f"该路径已在当前项目索引中（{_render_refs(refs)}，状态 active），"
            "本轮未被召回，属当前证据缺口"
        )
    if basis == "static_suffix_rule":
        return (
            f"{'、'.join(refs)} 不在当前摄取范围、未纳入本项目索引，"
            "因此当前证据中不会出现；无法据此确认仓库是否包含此类文件"
        )
    if basis == "unverifiable_assertion":
        return ("当前证据不足以穷举确认" if exhaustive else "当前证据不足以确认该结论") + (
            "；是否在仓库中存在需仓库级核验，P1.5 不做此判定"
        )
    return ""


def _render(assessment: _Assessment, text: str, refs: Sequence[str], *, exhaustive: bool) -> str:
    clause = _clause(
        assessment.basis,
        refs,
        exhaustive=exhaustive,
        delivered=assessment.delivered,
        identity_only=assessment.identity_only,
    )
    if not clause:
        return text
    if assessment.delivered and assessment.identity_only:
        # 纯身份陈述被交付引用直接反证：主语换成引用本身，原文只留在 original_text
        return f"{_render_refs(refs)}：{clause}"
    if assessment.replace:
        return f"{_subject(text, ())}：{clause}"
    return f"{text}（{clause}）"


def _assess(
    segment: str,
    markers: _Markers,
    *,
    evidence_paths: Sequence[str],
    corpus: CorpusProfile,
) -> _Assessment:
    """对单个目标段做分类 + 事实校验；只产出一个原因。"""
    tokens = [*iter_path_tokens(segment), *iter_symbol_tokens(segment)]
    in_evidence: list[str] = []
    in_index: list[str] = []
    for token in tokens:
        seen = _seen_paths(token, evidence_paths)
        indexed = corpus.matching_paths(token) if corpus.known else ()
        in_evidence.extend(seen)
        in_index.extend(path for path in indexed if path not in seen)
    in_evidence = list(dict.fromkeys(in_evidence))
    in_index = list(dict.fromkeys(in_index))

    in_citations: list[str] = []
    for token in tokens:
        in_citations.extend(_seen_paths(token, markers.cited_paths))
    in_citations = list(dict.fromkeys(in_citations))

    uningested = _uningested_signals(segment, corpus)
    category: NotFoundCategory = "missing_from_current_evidence"
    basis: FactCheckBasis = "no_conflict_found"
    refs: tuple[str, ...] = ()
    weakened = False
    delivered = False

    # 优先级即"这一段的原因是什么"：交付引用 > 证据事实 > 索引事实 > 摄取范围 >
    # 无从证明的强断言。一段只取一个，混合原因由调用方拆成多条（用户裁决 2026-07-25）。
    # T25.1 把"交付引用"排在最前：它是同类事实里最强的一条——用户看到的 citations
    # 就是它，据此说"未找到"在身份层面直接不符。basis 仍复用 evidence_history，
    # 不扩 FactCheckBasis 值域（Answer 契约不变）。
    if markers.rewritable and markers.has_absence and in_citations:
        delivered = True
        basis = "evidence_history"
        refs = tuple(in_citations)
    elif markers.rewritable and markers.has_absence and in_evidence:
        basis = "evidence_history"
        refs = tuple(in_evidence)
    elif markers.rewritable and markers.has_absence and in_index:
        basis = "corpus_index"
        refs = tuple(in_index)
    elif uningested:
        category = "unsupported_or_not_ingested"
        basis = "static_suffix_rule"
        refs = uningested
    elif markers.rewritable and markers.has_repo_negation:
        basis = "unverifiable_assertion"
        weakened = True

    return _Assessment(
        segment=segment,
        category=category,
        basis=basis,
        refs=refs,
        # Gate 要求已召回/已索引路径不得被写成"未找到/不存在"，故这两类与仓库级否定
        # 整条改写（主语用去标记后的方面名）；格式未摄取原文成立，只追加披露。
        # delivered 无条件改写：交付引用即便因 retrieve 异常未进历史账本，也绝不能
        # 让"未找到 X"原样交付。
        replace=markers.rewritable
        and (
            delivered
            or bool(markers.has_absence and (in_evidence or in_index))
            or markers.has_repo_negation
        ),
        weakened=weakened,
        delivered=delivered,
        identity_only=delivered and markers.identity_only,
    )


def _bracket_depths(text: str) -> list[int]:
    """每个字符所处的括号深度：括号内的内容不参与切分（确定性说明里常有"（A:X、B:Y）"
    这类枚举，在括号内切会切碎原文并留下不配对的括号）。"""
    depths: list[int] = []
    depth = 0
    for char in text:
        if char in "（(【[":
            depth += 1
            depths.append(depth)
        elif char in "）)】]":
            depths.append(depth)
            depth = max(0, depth - 1)
        else:
            depths.append(depth)
    return depths


def _target_spans(text: str) -> list[tuple[int, int]]:
    """文本中可识别目标的位置：路径 token + 符号 token + 隐藏文件 + 封闭技术词。"""
    spans = iter_target_spans(text)
    spans += [match.span() for match in _DOTFILE_TOKEN.finditer(text)]
    spans += [match.span() for match in _TERM_TARGET.finditer(text)]
    return merge_spans(spans)


def _cut_between(text: str, depths: list[int], start: int, end: int) -> tuple[int, int] | None:
    """相邻两个目标之间的间隙 text[start:end] 是否构成连接处；是则返回连接词区间。

    1. 括号外的标点（、，；）**不可能出现在词内** → 取最左一个即可，连接词后面的自然
       措辞（"，与此同时 X"/"，同时，X"）随之留在**后一个**目标那一段，保持原序语义。
    2. 否则只有当整个间隙就是一个连接词（"与"/"以及"/"或者"…）时才切——此时连接词
       两侧紧邻的都是目标，是可证的连接处。
    3. 其余一律不切（"README 提及数据库迁移"的"及"、"…的说明与…"的"与"）：宁可少拆
       一条（同条内按优先级取唯一原因、原文完整保留），也不冒切碎自然措辞的风险。
    """
    for index in range(start, end):
        if depths[index] == 0 and text[index] in _PUNCT_CONNECTORS:
            return (index, index + 1)
    gap = text[start:end]
    token = gap.strip()
    if token in _WORD_CONNECTORS:
        offset = start + gap.index(token)
        if depths[offset] == 0:
            return (offset, offset + len(token))
    return None


def _split_spans(text: str) -> list[tuple[int, int]]:
    """按可证的连接处把一条缺口语句切成目标段区间（确定性、保序、字符不丢）。

    切点只可能出现在**两个目标之间**，因此每个段必含至少一个目标，不会再产生"无信号
    残段"需要事后归位；段区间连续覆盖全文，只有连接词本身被丢弃。
    """
    depths = _bracket_depths(text)
    spans = [span for span in _target_spans(text) if depths[span[0]] == 0]
    cuts = [
        cut
        for (_, left_end), (right_start, _) in pairwise(spans)
        if (cut := _cut_between(text, depths, left_end, right_start)) is not None
    ]
    segments: list[tuple[int, int]] = []
    pos = 0
    for cut_start, cut_end in cuts:
        segments.append((pos, cut_start))
        pos = cut_end
    segments.append((pos, len(text)))
    return [(start, end) for start, end in segments if text[start:end].strip()]


def _split_segments(text: str) -> list[str]:
    return [text[start:end] for start, end in _split_spans(text)]


def _trim_connectors(text: str) -> str:
    """剥掉切分后残留在两端的连接词（"以及数据库迁移" → "数据库迁移"）。

    只有**紧邻目标**的连接词才算残留："与此同时 X" 的"与"是词的一部分，删掉就成了
    "此同时 X"（第三轮复审阻断点2）；无目标可锚定时原样返回。
    """
    spans = _target_spans(text)
    if not spans:
        return text
    head, tail = spans[0][0], spans[-1][1]
    start = head if text[:head].strip(_EDGE_TRIM) in _WORD_CONNECTORS else 0
    stop = tail if text[tail:].strip(_EDGE_TRIM) in _WORD_CONNECTORS else len(text)
    return text[start:stop]


def _segment_text(text: str) -> str:
    return _trim_connectors(text.strip()).strip(_EDGE_TRIM)


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
    supported = {
        normalize_aspect_label(aspect) for aspect in supported_aspects_history if aspect.strip()
    }
    texts: list[str] = []
    details: list[NotFoundDetail] = []
    warnings: list[str] = []
    dropped: list[str] = []
    uningested_all: set[str] = set()
    unsplit: set[str] = set()
    delivered_identity: set[str] = set()
    delivered_residual: set[str] = set()
    weakened = 0

    for item in items:
        raw = item.text.strip()
        if not raw:
            continue
        if item.source != "deterministic" and normalize_aspect_label(raw) in supported:
            # 前轮已支持的方面不得凭空升级为缺失（原证据被证伪才可退化，P1.5 无证伪机制）
            dropped.append(raw)
            continue

        has_absence = any(marker in raw for marker in (*_ABSENCE_MARKERS, *_REPO_LEVEL_NEGATION))
        exhaustive = any(q in raw for q in _EXHAUSTIVE_QUANTIFIERS) and (
            has_absence or any(n in raw for n in _SOFT_NEGATION)
        )
        markers = _Markers(
            rewritable=item.source != "deterministic",
            has_absence=has_absence,
            exhaustive=exhaustive,
            # 全局否定 = 仓库级断言：无 inventory 同样只能降级（"未找到任何其他 Controller"）
            has_repo_negation=exhaustive or any(marker in raw for marker in _REPO_LEVEL_NEGATION),
            cited_paths=item.cited_paths,
            identity_only=item.identity_only,
        )
        assess = partial(_assess, markers=markers, evidence_paths=evidence_paths, corpus=corpus)

        # 一条 not_found 只能有一个原因：LLM 把两类缺口写成一句（"未覆盖数据库迁移与
        # DocumentService 的删除实现"）时确定性拆分，否则会出现 category 与 basis 互相
        # 矛盾的条目（用户裁决 2026-07-25）。同类目标合并成一句时不拆，原文照旧。
        # 确定性条目由 required_evidence_tail 逐类型生成，本就一条一原因；不拆分它，
        # 免得把自己格式化好的说明切碎（括号/分号结构会被破坏）
        spans = _split_spans(raw) if markers.rewritable else []
        segments = [(span, assess(raw[span[0] : span[1]])) for span in spans]
        reasons = list(dict.fromkeys(assessment.reason for _span, assessment in segments))
        if len(reasons) <= 1:
            groups: list[tuple[_Assessment, str, tuple[str, ...]]] = [(assess(raw), raw, ())]
        else:
            # 保序：相邻同原因段按原文区间合并（连接词随原文保留），非相邻的用顿号连接；
            # 同一原因下每个成员的 refs 全量汇总，不得只留第一个（第二轮复审阻断点1）
            runs: list[_Run] = []
            for (start, end), assessment in segments:
                if runs and runs[-1].assessment.reason == assessment.reason:
                    prev = runs[-1]
                    runs[-1] = _Run(
                        prev.assessment, prev.start, end, (*prev.refs, *assessment.refs)
                    )
                else:
                    runs.append(_Run(assessment, start, end, assessment.refs))
            groups = []
            for reason in reasons:
                members = [run for run in runs if run.assessment.reason == reason]
                text = "、".join(
                    part for run in members if (part := _segment_text(raw[run.start : run.end]))
                )
                merged = tuple(dict.fromkeys(ref for run in members for ref in run.refs))
                if text:
                    groups.append((members[0].assessment, text, merged))

        for assessment, segment_text, merged_refs in groups:
            refs = merged_refs or assessment.refs
            text = _render(assessment, segment_text, refs, exhaustive=markers.exhaustive)
            if text in texts:
                continue
            if assessment.delivered:
                target = delivered_identity if assessment.identity_only else delivered_residual
                target.update(refs)
            if assessment.category == "unsupported_or_not_ingested":
                uningested_all.update(refs)
            else:
                # 无法确定性拆分时未摄取信号会被更高优先级原因压住：不改分类（一条一原因），
                # 但必须显式告警，不能让"这条里还有 .sql 缺口"这件事静默消失
                unsplit.update(_uningested_signals(segment_text, corpus))
            weakened += int(assessment.weakened)
            texts.append(text)
            details.append(
                NotFoundDetail(
                    text=text,
                    category=assessment.category,
                    source=item.source,
                    basis=assessment.basis,
                    refs=refs,
                    original_text=raw if text != raw else None,
                )
            )

    if dropped:
        warnings.append(
            "finalize: 前轮已支持方面被后轮报为缺失，已剔除（" + "；".join(dropped) + "）"
        )
    # T25.1 的两条告警只陈述可证事实：①身份级不符（把交付引用写成"未找到"）可证；
    # ②"指向交付引用 + 含未核验命题"是两项事实的合取，**不得**据此断言与正文冲突。
    if delivered_identity:
        warnings.append(
            "finalize: not_found 把本次回答的引用来源写成缺失，已按引用事实更正（"
            + ",".join(sorted(delivered_identity))
            + "）"
        )
    if delivered_residual:
        warnings.append(
            "finalize: not_found 指向本次回答的引用来源，且含未经确定性核验的命题（"
            + ",".join(sorted(delivered_residual))
            + "）"
        )
    delivered_all = delivered_identity | delivered_residual
    for basis_key, label in (
        ("evidence_history", "全轮证据中出现过的路径被写成缺失"),
        ("corpus_index", "已索引 active 路径被写成缺失"),
    ):
        hits = list(
            dict.fromkeys(
                ref
                for detail in details
                if detail.basis == basis_key
                for ref in detail.refs
                # 交付引用已由上面两条更精确地报告；重复计入会把"出现在某轮检索证据里"
                # 说到本就不一定成立的路径上（retrieve 异常时交付引用可能不在历史账本）
                if ref not in delivered_all
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
    if unsplit - uningested_all:
        warnings.append(
            "finalize: not_found 条目混合未摄取格式信号但无法确定性拆分，已按更高优先级原因"
            "归类（" + ",".join(sorted(unsplit - uningested_all)) + "）"
        )
    return NotFoundCalibration(texts=tuple(texts), details=tuple(details), warnings=tuple(warnings))
