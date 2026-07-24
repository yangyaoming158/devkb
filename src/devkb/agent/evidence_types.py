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

_CLAUSE_SPLIT = re.compile(r"[。；;.!？?！\n，,]")
_NEG_TRIGGERS = ("不要", "请勿", "不得", "禁止", "勿使用", "勿引用")
# 注意不加"别用/别引用"（撞"分别引用"）、"不应用"（撞"应用/application"）
_NEG_SOFT = ("不能用", "不能引用", "不应引用")
_SUPPLEMENT = ("只能作为补充", "仅作补充", "只作补充", "只能补充", "作为补充")
_CLASS = re.compile(r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*")
_PATHISH = re.compile(
    r"[\w./\-]+\.(?:java|py|go|kt|sql|yml|yaml|properties|vue|ts|tsx|jsx|md)",
    re.IGNORECASE,
)
_SYMBOL_STOP = frozenset({"Java", "JavaScript", "TypeScript", "README", "PROGRESS", "GraphRAG"})
_TEST_SUFFIX = ("Test", "Tests", "IT", "ITs", "Spec")


def _is_forbidding(seg: str) -> bool:
    if any(t in seg for t in _NEG_TRIGGERS):
        return True
    return any(t in seg for t in _NEG_SOFT)


def _is_supplement(seg: str) -> bool:
    return any(t in seg for t in _SUPPLEMENT)


def _detect_types(seg: str) -> set[EvidenceType]:
    s = seg
    sl = seg.lower()
    types: set[EvidenceType] = set()
    if "设计文档" in s or "设计说明" in s:
        types.add("design_doc")
    if (
        "数据库迁移" in s
        or "迁移文件" in s
        or "flyway" in sl
        or "migration" in sl
        or ("迁移" in s and any(w in s for w in ("数据库", "schema", "sql", "SQL", ".sql")))
    ):
        types.add("migration")
    if (
        "配置文件" in s
        or "application.yml" in sl
        or "application.yaml" in sl
        or "docker-compose" in sl
        or "compose" in sl
    ):
        types.add("production_config")
    if (
        any(w in s for w in ("Vue", "TypeScript", "前端源码", "前端组件"))
        or ".vue" in sl
        or ".tsx" in sl
    ):
        types.add("frontend_source")
    if "计划文档" in s or "规划文档" in s or "路线图" in s:
        types.add("historical_plan")
    if "测试" in s:
        types.add("test")
    if "dev-log" in sl or "devlog" in sl or "开发日志" in s:
        types.add("dev_log")
    if (
        (
            "生产" in s
            and any(
                w in s
                for w in ("源码", "实现", "代码", "Java", "Repository", "Controller", "方法", "类")
            )
        )
        or (("Java" in s or "java" in sl) and any(w in s for w in ("实现", "源码", "代码")))
        or "src/main" in sl
        or any(w in s for w in ("生产源码", "生产实现", "生产代码", "实现代码"))
    ):
        types.add("production_source")
    if "README" in s or "readme" in sl or "PROGRESS" in s:
        types.add("current_doc")
    return types


def _looks_like_symbol(token: str) -> bool:
    return token not in _SYMBOL_STOP and _CLASS.fullmatch(token) is not None


def _infer_symbol_type(symbol: str, seg_types: set[EvidenceType]) -> EvidenceType:
    if symbol.endswith(_TEST_SUFFIX):
        return "test"
    production: list[EvidenceType] = [t for t in seg_types if t in PRODUCTION_TYPES]
    if len(production) == 1:
        return production[0]
    return "production_source"


def _detect_named(
    seg: str, seg_types: set[EvidenceType]
) -> list[tuple[EvidenceType, str, str | None, str | None]]:
    """返回 (type, anchor, path, symbol)。"""
    named: list[tuple[EvidenceType, str, str | None, str | None]] = []
    seen: set[str] = set()
    for path in _PATHISH.findall(seg):
        if path in seen:
            continue
        seen.add(path)
        named.append((classify_path(path), path, path, None))
    for token in _CLASS.findall(seg):
        if not _looks_like_symbol(token) or token in seen:
            continue
        seen.add(token)
        named.append((_infer_symbol_type(token, seg_types), token, None, token))
    return named


def parse_required_evidence(question: str) -> RequiredEvidence:
    """确定性解析 → 权威 RequiredEvidenceItem[] + 禁止替代类型。"""
    forbidden: set[EvidenceType] = set()
    raw_items: list[tuple[EvidenceType, str, str | None, str | None]] = []

    for segment in _CLAUSE_SPLIT.split(question):
        seg = segment.strip()
        if not seg:
            continue
        seg_types = _detect_types(seg)
        if _is_forbidding(seg) or _is_supplement(seg):
            forbidden |= seg_types
            continue
        named = _detect_named(seg, seg_types)
        named_types = {t for t, *_ in named}
        raw_items.extend(named)
        # 无点名符号的类型 → 一条 type-only 项（anchor=类型词）
        for t in seg_types:
            if t not in named_types:
                raw_items.append((t, TYPE_LABELS[t], None, None))

    # 全局去重 + 禁止类型剔除 + 有点名时丢弃同类型 type-only 项
    typed_named = {t for t, _a, _p, sym in raw_items if sym or (_p and t in PRODUCTION_TYPES)}
    ids = count(1)
    seen_key: set[tuple[EvidenceType, str | None, str | None]] = set()
    items: list[RequiredEvidenceItem] = []
    for t, anchor, path, symbol in raw_items:
        if t in forbidden:
            continue
        if symbol is None and path is None and t in typed_named:
            continue  # 该类型已有具体点名，丢弃 type-only
        key = (t, path, symbol)
        if key in seen_key:
            continue
        seen_key.add(key)
        items.append(
            RequiredEvidenceItem(
                item_id=f"R{next(ids)}", type=t, anchor=anchor, path=path, symbol=symbol
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
    base = rel_path.rsplit("/", 1)[-1].lower()
    stem = base.rsplit(".", 1)[0]
    if item.symbol is not None:
        # 精确文件名匹配（Java 惯例：public 类与文件同名）。用子串会让
        # ReorderServiceLog.java 误配 OrderService，制造新的错误 full；从严更安全。
        return item.symbol.lower() == stem
    if item.path is not None:
        return item.path.lower() in rel_path.lower()
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
