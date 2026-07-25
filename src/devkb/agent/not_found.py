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
_PUNCT_STRIP = re.compile(r"[\s。．.，,；;：:、！!？?（）()【】\[\]\"'`]+")
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
    stripped = _trim_connectors(" ".join(stripped.split()).strip(_EDGE_TRIM))
    changed = True
    while changed:  # 去标记后可能剩下悬空前缀（"当前证据 X"）
        changed = False
        for prefix in _ORPHAN_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip(_EDGE_TRIM)
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


@dataclass(frozen=True)
class _Assessment:
    """一个目标段的确定性结论：**恰好一个原因**（category + basis + refs）。"""

    segment: str
    category: NotFoundCategory
    basis: FactCheckBasis
    refs: tuple[str, ...]
    replace: bool
    weakened: bool

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


def _clause(basis: FactCheckBasis, refs: Sequence[str], *, exhaustive: bool) -> str:
    """由**合并后的**事实来源渲染确定性说明；空串表示无需说明。"""
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
    clause = _clause(assessment.basis, refs, exhaustive=exhaustive)
    if not clause:
        return text
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

    uningested = _uningested_signals(segment, corpus)
    category: NotFoundCategory = "missing_from_current_evidence"
    basis: FactCheckBasis = "no_conflict_found"
    refs: tuple[str, ...] = ()
    weakened = False

    # 优先级即"这一段的原因是什么"：证据事实 > 索引事实 > 摄取范围 > 无从证明的强断言。
    # 一段只取一个，混合原因由调用方拆成多条（用户裁决 2026-07-25：一条一原因）。
    if markers.rewritable and markers.has_absence and in_evidence:
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
        replace=markers.rewritable
        and (bool(markers.has_absence and (in_evidence or in_index)) or markers.has_repo_negation),
        weakened=weakened,
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
    supported = {_normalize(aspect) for aspect in supported_aspects_history if aspect.strip()}
    texts: list[str] = []
    details: list[NotFoundDetail] = []
    warnings: list[str] = []
    dropped: list[str] = []
    uningested_all: set[str] = set()
    unsplit: set[str] = set()
    weakened = 0

    for item in items:
        raw = item.text.strip()
        if not raw:
            continue
        if item.source != "deterministic" and _normalize(raw) in supported:
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
    if unsplit - uningested_all:
        warnings.append(
            "finalize: not_found 条目混合未摄取格式信号但无法确定性拆分，已按更高优先级原因"
            "归类（" + ",".join(sorted(unsplit - uningested_all)) + "）"
        )
    return NotFoundCalibration(texts=tuple(texts), details=tuple(details), warnings=tuple(warnings))
