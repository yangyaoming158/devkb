"""P1.5 T22：证据类型分层与用户指定必需证据的确定性解析（RT-01/05）。

三件事，全部零 LLM、可单测、仓库无关：

- ``classify_path``：rel_path → 证据类型启发式（生产源码/配置/迁移/设计/当前文档/
  测试/历史计划/dev-log/前端/其他）。用于权威性分层与 full 硬约束。
- ``parse_required_evidence``：从用户问题**确定性**提取被明确要求的证据类型、
  "不要用 X 代替 Y"式的禁止替代类型，以及点名的符号。这是权威来源；plan 的 LLM
  回显只作确认，不得新增/伪造（见 ``nodes.AgentNodes.plan``）。
- ``unmet_required_types``：full 硬约束——每个必需类型都须有同类型直接引用；
  测试/设计/历史计划/dev-log 不能替代必需的生产证据（它们是不同类型）。

设计取向：解析对显式、通用的证据类型词汇生效，对自然问法优雅降级为空约束
（宁可回退到 P1 行为，不过度约束）。over-detect 使 full 更保守（诚实方向），
under-detect 不劣于 P1，两侧都安全。不为回归集单句写关键词特判。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

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

# 生产证据类型：这些才能满足"必需生产证据"；其余（测试/设计/历史计划/dev-log）
# 只能作补充，不能替代（权威性分层，RT-05）。
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

_MIGRATION_VERSION = re.compile(r"/v\d+__", re.IGNORECASE)
_SYMBOL = re.compile(r"[A-Z][A-Za-z0-9]{2,}")


def classify_path(rel_path: str) -> EvidenceType:
    """rel_path → 证据类型。顺序敏感：test 先于 production_source。"""
    lower = rel_path.lower()
    base = lower.rsplit("/", 1)[-1]

    if (
        lower.endswith(".sql")
        or "/migration/" in lower
        or "flyway" in lower
        or _MIGRATION_VERSION.search(lower)
    ):
        return "migration"
    if (
        "/test/" in lower
        or "/tests/" in lower
        or base.endswith("test.java")
        or base.endswith("tests.java")
        or base.startswith("test_")
        or ".test." in base
        or ".spec." in base
        or "_test." in base
    ):
        return "test"
    if lower.endswith((".vue", ".ts", ".tsx", ".jsx")):
        return "frontend_source"
    if (
        lower.endswith((".yml", ".yaml", ".properties", ".toml"))
        or "docker-compose" in base
        or base == ".env"
        or base.startswith(".env.")
        or "application" in base
    ):
        return "production_config"
    if "dev-log" in lower or "devlog" in lower or base.startswith("changelog"):
        return "dev_log"
    if "/design/" in lower or (base.startswith("design") and lower.endswith(".md")):
        return "design_doc"
    if (
        "/plans/" in lower
        or "/plan/" in lower
        or "phase-" in base
        or base.startswith("roadmap")
        or "规划" in rel_path
        or "计划" in rel_path
        or "路线图" in rel_path
    ):
        return "historical_plan"
    if base.startswith("readme") or base.startswith("progress"):
        return "current_doc"
    if (
        lower.endswith(
            (
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
        )
        or "/src/main/" in lower
    ):
        return "production_source"
    if lower.endswith(".md"):
        return "current_doc"
    return "other"


def covered_types(rel_paths: Iterable[str]) -> set[EvidenceType]:
    return {classify_path(path) for path in rel_paths}


@dataclass(frozen=True)
class RequiredEvidence:
    """确定性解析出的用户明确要求（权威来源，不由 LLM 覆盖）。"""

    required_types: tuple[EvidenceType, ...] = ()
    forbidden_substitute_types: tuple[EvidenceType, ...] = ()
    named_symbols: tuple[str, ...] = ()

    @property
    def has_requirements(self) -> bool:
        return bool(self.required_types)


def _any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def parse_required_evidence(question: str) -> RequiredEvidence:
    """从问题确定性提取必需证据类型 / 禁止替代类型 / 点名符号。"""
    q = question
    ql = question.lower()

    # 1) 禁止替代句式（先解析，用于抑制"不要只引用 X"里的 X 被误当必需）
    forbidden: list[EvidenceType] = []
    if _any(
        q,
        (
            "不要用测试",
            "测试只能作为补充",
            "测试不能",
            "不要用测试或规划",
            "架构文档或测试代替",
            "不要用测试、",
            "不能用测试",
        ),
    ):
        forbidden.append("test")
    if _any(q, ("dev-log", "dev log", "devlog")) and _any(
        q, ("不要用", "不要使用", "代替", "不能")
    ):
        forbidden.append("dev_log")
    if _any(
        q,
        (
            "不要把规划",
            "不要用规划",
            "规划文档当作实现",
            "规划文档代替",
            "不要用测试或规划",
            "不要只依据 README 或规划",
            "不要只依据README或规划",
            "计划文档",
        ),
    ):
        forbidden.append("historical_plan")
    design_forbidden = _any(q, ("不要只引用设计文档", "不要只依据设计", "不能只用设计"))
    if design_forbidden:
        forbidden.append("design_doc")
    readme_present = _any(q, ("README", "readme"))
    readme_ban = _any(q, ("不要只依据 README", "不要只依据README", "不要只用 README")) or (
        readme_present and _any(q, ("代替", "代替实现")) and _any(q, ("不要", "不能", "别"))
    )
    if readme_ban:
        forbidden.append("current_doc")

    # 2) 必需证据类型
    required: list[EvidenceType] = []

    def demand(t: EvidenceType) -> None:
        if t not in required:
            required.append(t)

    production = (
        (
            "生产" in q
            and _any(q, ("源码", "实现", "代码", "Java", "Repository", "Controller", "方法", "类"))
        )
        or (("Java" in q or "java" in ql) and _any(q, ("实现", "源码", "代码")))
        or "src/main" in ql
        or _any(q, ("生产源码", "生产实现", "生产代码"))
    )
    if production:
        demand("production_source")
    if _any(q, ("设计文档", "设计说明")) and not design_forbidden:
        demand("design_doc")
    migration = (
        _any(q, ("数据库迁移", "迁移文件"))
        or _any(ql, ("migration", "flyway"))
        or ("迁移" in q and _any(q, ("数据库", "schema", "sql", "SQL", ".sql")))
    )
    if migration:
        demand("migration")
    if (
        _any(q, ("配置文件", "application.yml", "application.yaml", "docker-compose"))
        or "compose" in ql
    ):
        demand("production_config")
    if readme_present and not readme_ban:
        demand("current_doc")
    if _any(ql, (".vue", ".tsx")) or _any(q, ("Vue", "TypeScript", "前端源码", "前端组件")):
        demand("frontend_source")

    # 3) 点名符号（大写驼峰标识符），仅供覆盖报告，不作硬门
    named = tuple(dict.fromkeys(_SYMBOL.findall(q)))

    # 禁止替代的类型不应同时是必需类型
    required = [t for t in required if t not in forbidden]
    return RequiredEvidence(
        required_types=tuple(required),
        forbidden_substitute_types=tuple(dict.fromkeys(forbidden)),
        named_symbols=named,
    )


def unmet_required_types(
    required: RequiredEvidence,
    cited_rel_paths: Iterable[str],
) -> list[EvidenceType]:
    """返回未被同类型直接引用覆盖的必需证据类型（升序稳定）。

    full 硬约束：空列表才允许 full。补充类型（测试/设计/历史计划/dev-log）因类型
    不同，天然无法覆盖必需的生产类型，故"测试不能替代生产实现"由类型系统保证。
    """
    if not required.required_types:
        return []
    covered = covered_types(cited_rel_paths)
    return [t for t in required.required_types if t not in covered]
