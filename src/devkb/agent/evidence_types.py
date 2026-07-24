"""P1.5 T22：证据类型分层、逐项 required-evidence 解析与确定性 coverage（RT-01/05）。

裁决语义（用户 2026-07-24 冻结）：A 的 schema + B 的确定性裁决。

- ``classify_path``：rel_path → 证据类型。**可信目录优先**（test/design/plans/
  review·audit/dev-log 先判），再在生产目录内识别 source/config/migration；
  避免 ``src/test/**.sql``、``docs/**flyway*.md`` 冒充生产 migration。
- ``parse_required_evidence``：按**子句 + 否定作用域**确定性解析出权威
  ``RequiredEvidenceItem[]``（type + 原文 anchor + 可选 path/symbol，同类型多个点名
  各自成项、不合并）与禁止替代类型。这是唯一权威来源。
- ``compute_coverage``：确定性 matcher，按真实引用逐项算覆盖矩阵。full 门、refine
  缺口、finalize 只读这里的结果；LLM 的覆盖报告只作诊断（见 nodes）。

第二轮复审整改（2026-07-24）：
- 子句切分不再切英文 ``.``，显式路径（``a/b/Foo.java``）不被截断；路径匹配改按
  完整路径后缀 / 裸文件名 basename 全等，杜绝 ``Foo.java`` 子串误配 ``NotFoo.java``。
- 否定作用域改子句内 "用 X 代替 Y"：X→禁止替代、Y→肯定必需；禁止类型不再删除
  其它子句显式产生的必需项。
- 类型检测按**原文位置排序**产出 (type, 原文 anchor, span)，item_id 依位置稳定，
  不再用无序 set 决定顺序；点名类默认 production_source（永不因同句"数据库迁移"
  被误判 migration）。

全部零 LLM、仓库无关、可单测。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import count
from typing import Literal

from pydantic import BaseModel, ConfigDict

EvidenceType = Literal[
    "production_source",
    "production_config",
    "migration",
    "design_doc",
    "current_doc",
    "test",
    "historical_plan",
    "dev_log",
    "frontend_source",
    "other",
]

# 可满足"必需生产证据"的类型；测试/设计/历史计划/dev-log 不在其中（权威性分层）。
PRODUCTION_TYPES: frozenset[EvidenceType] = frozenset(
    {"production_source", "production_config", "migration"}
)

TYPE_LABELS: dict[EvidenceType, str] = {
    "production_source": "生产源码",
    "production_config": "生产配置",
    "migration": "数据库迁移",
    "design_doc": "设计文档",
    "current_doc": "当前文档",
    "test": "测试",
    "historical_plan": "历史计划",
    "dev_log": "dev-log",
    "frontend_source": "前端源码",
    "other": "其他",
}

_SOURCE_EXTS = (
    ".java",
    ".py",
    ".go",
    ".kt",
    ".kts",
    ".rb",
    ".js",
    ".rs",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".php",
    ".scala",
)
_CONFIG_EXTS = (".yml", ".yaml", ".properties", ".toml")
_FRONTEND_EXTS = (".vue", ".ts", ".tsx", ".jsx")


def classify_path(rel_path: str) -> EvidenceType:
    """rel_path → 证据类型。可信目录优先，再在生产目录内识别。"""
    lower = rel_path.lower()
    segs = lower.strip("/").split("/")
    base = segs[-1] if segs else lower

    # 1) 可信（非生产）目录/命名优先——先于 .sql/config 关键词
    if (
        "test" in segs
        or "tests" in segs
        or base.endswith(("test.java", "tests.java"))
        or base.startswith("test_")
        or ".test." in base
        or ".spec." in base
        or "_test." in base
    ):
        return "test"
    if "dev-log" in lower or "devlog" in lower or base.startswith("changelog"):
        return "dev_log"
    if "design" in segs:
        return "design_doc"
    if (
        "plans" in segs
        or "plan" in segs
        or base.startswith("roadmap")
        or "phase-" in base
        or "规划" in rel_path
        or "计划" in rel_path
        or "路线图" in rel_path
    ):
        return "historical_plan"
    if "review" in segs or "audit" in segs or "audits" in base or base.startswith("audit"):
        return "historical_plan"
    if "docs" in segs and lower.endswith(".md"):
        return "current_doc"

    # 2) 生产目录内识别
    if lower.endswith(".sql") or "/migration/" in lower or "flyway" in segs:
        return "migration"
    if lower.endswith(_FRONTEND_EXTS):
        return "frontend_source"
    if (
        lower.endswith(_CONFIG_EXTS)
        or "docker-compose" in base
        or base == ".env"
        or base.startswith(".env.")
        or base.startswith("application.")
    ):
        return "production_config"
    if base.startswith("readme") or base.startswith("progress"):
        return "current_doc"
    if lower.endswith(_SOURCE_EXTS) or "/src/main/" in lower:
        return "production_source"
    if lower.endswith(".md"):
        return "current_doc"
    return "other"


class RequiredEvidenceItem(BaseModel):
    """一条权威的必需证据要求（确定性解析产出，不由 LLM 覆盖）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    item_id: str
    type: EvidenceType
    anchor: str
    path: str | None = None
    symbol: str | None = None


class RequiredEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    items: tuple[RequiredEvidenceItem, ...] = ()
    forbidden_substitute_types: tuple[EvidenceType, ...] = ()

    @property
    def has_requirements(self) -> bool:
        return bool(self.items)

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(item.item_id for item in self.items)


class CoverageEntry(BaseModel):
    """某条 required item 的确定性覆盖结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    item_id: str
    covered: bool
    matched_evidence_ids: tuple[str, ...] = ()


# ---- 解析 ----------------------------------------------------------------

# 注意：不切英文 "."，否则 backend/.../Foo.java 会被截成 "Foo" 与 "java"，
# 显式路径退化为 type-only 从而制造错误 full（第二轮复审发现1）。中文问题里
# 句子边界是 。；！？ 与换行、分号、逗号、顿号。
_CLAUSE_SPLIT = re.compile(r"[。；;！？!?、，,\n]")
_NEG_TRIGGERS = ("不要", "请勿", "不得", "禁止", "勿使用", "勿引用")
# 注意不加"别用/别引用"（撞"分别引用"）、"不应用"（撞"应用/application"）
_NEG_SOFT = ("不能用", "不能引用", "不应引用")
_SUPPLEMENT = ("只能作为补充", "仅作补充", "只作补充", "只能补充", "作为补充")
# "不要用 X 代替 Y"：X=禁止替代、Y=被保护（肯定必需）
_SUBSTITUTE_MARKERS = ("代替", "替代", "冒充", "顶替", "充当", "当作", "当成")

_CLASS = re.compile(r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*")
_PATHISH = re.compile(
    r"[\w./\-]+\.(?:java|py|go|kt|sql|yml|yaml|properties|vue|ts|tsx|jsx|md)",
    re.IGNORECASE,
)
_SYMBOL_STOP = frozenset({"Java", "JavaScript", "TypeScript", "README", "PROGRESS", "GraphRAG"})
_TEST_SUFFIX = ("Test", "Tests", "IT", "ITs", "Spec")

# 类型关键词 → 类型；每条取子句内首次出现（span + 原文 anchor）。生产源码用受限
# 的紧邻组合，避免 "生产环境部署" 之类误报。
_TYPE_PATTERNS: list[tuple[re.Pattern[str], EvidenceType]] = [
    (re.compile(r"设计文档|设计说明"), "design_doc"),
    (re.compile(r"数据库迁移|迁移文件|迁移脚本|迁移\s*SQL|[Ff]lyway"), "migration"),
    (re.compile(r"配置文件|application\.ya?ml|docker-compose"), "production_config"),
    (
        re.compile(
            r"前端源码|前端组件|前端代码|TypeScript|\.vue|\.tsx|(?<![A-Za-z])Vue(?![A-Za-z])"
        ),
        "frontend_source",
    ),
    (re.compile(r"计划文档|规划文档|路线图"), "historical_plan"),
    (re.compile(r"开发日志|dev-?log"), "dev_log"),
    (
        re.compile(r"生产[^，。；、\n]{0,8}?(?:源码|实现|代码)|实现代码|Java\s*实现|src/main"),
        "production_source",
    ),
    (re.compile(r"README|PROGRESS|当前文档"), "current_doc"),
    (re.compile(r"测试"), "test"),
]


def _is_forbidding(seg: str) -> bool:
    if any(t in seg for t in _NEG_TRIGGERS):
        return True
    return any(t in seg for t in _NEG_SOFT)


def _is_supplement(seg: str) -> bool:
    return any(t in seg for t in _SUPPLEMENT)


def _split_substitution(seg: str) -> tuple[str, str | None]:
    """禁止子句里的 "X 代替 Y" → (X 段, Y 段)；无替代标记则 (整句, None)。"""
    best: tuple[int, str] | None = None
    for marker in _SUBSTITUTE_MARKERS:
        idx = seg.find(marker)
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, marker)
    if best is None:
        return seg, None
    idx, marker = best
    return seg[:idx], seg[idx + len(marker) :]


def _detect_type_hits(seg: str) -> list[tuple[int, EvidenceType, str]]:
    """子句 → 每类首次出现 (span, type, 原文 anchor)，按原文位置。"""
    hits: list[tuple[int, EvidenceType, str]] = []
    for pattern, etype in _TYPE_PATTERNS:
        match = pattern.search(seg)
        if match is not None:
            hits.append((match.start(), etype, match.group()))
    return hits


def _looks_like_symbol(token: str) -> bool:
    return token not in _SYMBOL_STOP and _CLASS.fullmatch(token) is not None


def _infer_symbol_type(symbol: str, seg_types: set[EvidenceType]) -> EvidenceType:
    """点名类默认按生产源码归类；测试后缀归 test，明显前端上下文归前端。

    绝不把驼峰类名判成 migration/config（那是文件级类型）——修复同句出现
    "数据库迁移" 时 DocumentService 被误判 migration 的缺陷（复审发现3）。
    """
    if symbol.endswith(_TEST_SUFFIX):
        return "test"
    if "frontend_source" in seg_types and "production_source" not in seg_types:
        return "frontend_source"
    return "production_source"


def _detect_named_hits(
    seg: str, seg_types: set[EvidenceType]
) -> list[tuple[int, EvidenceType, str, str | None, str | None]]:
    """返回 (span, type, anchor, path, symbol)。路径优先，类名跳过路径内部片段。"""
    named: list[tuple[int, EvidenceType, str, str | None, str | None]] = []
    path_spans: list[tuple[int, int]] = []
    seen_paths: set[str] = set()
    for match in _PATHISH.finditer(seg):
        path = match.group()
        path_spans.append((match.start(), match.end()))
        if path in seen_paths:
            continue
        seen_paths.add(path)
        named.append((match.start(), classify_path(path), path, path, None))
    seen_syms: set[str] = set()
    for match in _CLASS.finditer(seg):
        token = match.group()
        if any(start <= match.start() < end for start, end in path_spans):
            continue  # 属于已捕获路径的一部分（如 Foo.java 里的 Foo）
        if not _looks_like_symbol(token) or token in seen_syms:
            continue
        seen_syms.add(token)
        named.append((match.start(), _infer_symbol_type(token, seg_types), token, None, token))
    return named


def _collect_positive(
    clause_index: int,
    seg: str,
    raw: list[tuple[tuple[int, int], EvidenceType, str, str | None, str | None]],
) -> None:
    """肯定子句 → 追加点名项 + type-only 项（带 (子句序, 局部 span) 排序键）。"""
    seg_types: set[EvidenceType] = {t for _sp, t, _a in _detect_type_hits(seg)}
    for span, etype, anchor, path, symbol in _detect_named_hits(seg, seg_types):
        raw.append(((clause_index, span), etype, anchor, path, symbol))
    for span, etype, anchor in _detect_type_hits(seg):
        raw.append(((clause_index, span), etype, anchor, None, None))


def parse_required_evidence(question: str) -> RequiredEvidence:
    """确定性解析 → 权威 RequiredEvidenceItem[] + 禁止替代类型。"""
    forbidden: list[EvidenceType] = []
    raw: list[tuple[tuple[int, int], EvidenceType, str, str | None, str | None]] = []

    for clause_index, segment in enumerate(_CLAUSE_SPLIT.split(question)):
        seg = segment.strip()
        if not seg:
            continue
        if _is_forbidding(seg):
            x_side, y_side = _split_substitution(seg)
            for _sp, etype, _a in _detect_type_hits(x_side):
                if etype not in forbidden:
                    forbidden.append(etype)
            if y_side is not None:  # 被保护的一侧是肯定必需
                _collect_positive(clause_index, y_side, raw)
            continue
        if _is_supplement(seg):
            for _sp, etype, _a in _detect_type_hits(seg):
                if etype not in forbidden:
                    forbidden.append(etype)
            continue
        _collect_positive(clause_index, seg, raw)

    raw.sort(key=lambda item: item[0])
    # 有具体点名（路径/符号）的类型 → 丢弃该类型的 type-only 项，避免重复覆盖计数。
    named_types = {
        etype for _k, etype, _a, path, symbol in raw if path is not None or symbol is not None
    }
    ids = count(1)
    seen: set[tuple[EvidenceType, str | None, str | None]] = set()
    items: list[RequiredEvidenceItem] = []
    for _key, etype, anchor, path, symbol in raw:
        if symbol is None and path is None and etype in named_types:
            continue
        dedup_key = (etype, path, symbol)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        items.append(
            RequiredEvidenceItem(
                item_id=f"R{next(ids)}", type=etype, anchor=anchor, path=path, symbol=symbol
            )
        )
    return RequiredEvidence(
        items=tuple(items),
        forbidden_substitute_types=tuple(dict.fromkeys(forbidden)),
    )


# ---- 确定性覆盖 matcher --------------------------------------------------


def _item_matches(item: RequiredEvidenceItem, rel_path: str) -> bool:
    if classify_path(rel_path) != item.type:
        return False
    lower = rel_path.lower()
    base = lower.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    if item.symbol is not None:
        # 精确文件名匹配（Java 惯例：public 类与文件同名）。用子串会让
        # ReorderServiceLog.java 误配 OrderService，制造新的错误 full；从严更安全。
        return item.symbol.lower() == stem
    if item.path is not None:
        wanted = item.path.lower()
        if "/" in wanted:
            # 含目录的显式路径：完整路径或按目录边界的后缀匹配
            return lower == wanted or lower.endswith("/" + wanted)
        # 裸文件名：basename 全等（杜绝 foo.java ⊂ notfoo.java 的子串误配）
        return base == wanted
    return True  # type-only：类型已匹配即可


def compute_coverage(
    required: RequiredEvidence,
    cited: Iterable[tuple[str, str]],
) -> tuple[CoverageEntry, ...]:
    """按真实引用 (evidence_id, rel_path) 逐项算权威覆盖矩阵。"""
    cited_list = list(cited)
    entries: list[CoverageEntry] = []
    for item in required.items:
        matched = tuple(eid for eid, path in cited_list if _item_matches(item, path))
        entries.append(
            CoverageEntry(item_id=item.item_id, covered=bool(matched), matched_evidence_ids=matched)
        )
    return tuple(entries)


def uncovered_items(
    required: RequiredEvidence,
    coverage: tuple[CoverageEntry, ...],
) -> list[RequiredEvidenceItem]:
    uncovered_ids = {entry.item_id for entry in coverage if not entry.covered}
    return [item for item in required.items if item.item_id in uncovered_ids]


def all_required_covered(coverage: tuple[CoverageEntry, ...]) -> bool:
    return all(entry.covered for entry in coverage)
