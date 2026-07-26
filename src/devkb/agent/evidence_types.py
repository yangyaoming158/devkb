"""P1.5 T22：证据类型分层、逐项 required-evidence 解析与确定性 coverage（RT-01/05）。

裁决语义：A 的 schema + B 的确定性裁决（2026-07-24）；**结构性 fail-closed**（2026-07-25
第四轮复审）；**约束跨度账本**（2026-07-25 第五轮复审裁决）。

第五轮复审确认："三态 + unresolved 硬门"方向对，但"解析漏检必进 unresolved"尚未成立——
旧 ``_extract`` 以整段为单位判"是否解析出目标"，只要一个目标命中，同一义务下的其余目标
就静默消失（``请引用 Foo.java 和关键实现`` → 只剩 Foo.java 且 status=complete）。故改为
**约束跨度账本（constraint span ledger）**：

1. **义务跨度独立检测**（``_detect_obligations``）：证据义务与"目标是否解析成功"解耦。
   三类义务标记——强指令（引用/参见/参照/援引，对象在标记之后）、弱指令（根据/依据/
   列出…，须邻接证据信号）、核验比较谓词（声称/是否一致/是否支持…，**对象在标记之前**，
   覆盖 c02 这类"文档声称 X，实现是否支持"表达）。
2. **逐目标段结算**（``_resolve_region`` / ``_resolve_one``）：义务对象按列表连接词拆成
   目标段，每段各自结算为 resolved item / unresolved 约束 / 明确的非目标尾语（整段不含
   任何目标名词），不存在"整段 produced=True"。
3. **身份优先**：裸 ``README``/``PROGRESS`` 等规范文档名保留身份（不退化为 type-only，
   否则任意 current_doc 都能顶替）；``相关 Java 配置`` 这类无身份类别直接 unresolved。
4. **分类先目录后扩展名**（``classify_path``）：audit/changelog/plan 等文件名约定只作用于
   文档扩展名，生产源码不再被误判；``Test*`` 用词边界规则（Testing/Testimony/Contest 不是
   测试），大小写敏感。
5. **两类否定分开执行**：``forbidden_substitute_types``（不能替代必需证据、可作补充）与
   ``forbidden_citation_types``（不得直接引用，出现即不得 full）。"只能作为补充"、
   "不要只引用 X"、"不要用 X 代替 Y"都属前者，"不要引用 X"属后者。
6. resolved 项数超过 LLM schema 上限 ``MAX_REQUIRED_ITEMS`` 时记 ``partial_enumeration``，
   不得仍报 complete。

三态与硬门（第四轮裁决，保留）：``RequiredEvidence.status`` = ``none``/``complete``/
``ambiguous``；``full`` 的必要条件 = 每个 resolved item 被直接引用覆盖 **且** 无
``unresolved_constraints`` **且** 无禁止引用类型被引用。unresolved 永不进入 coverage、
也不用 type=other 伪造可被任意文件满足的 item。

全部零 LLM、仓库无关、可单测；大小写敏感（ext4）。
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
UnresolvedReason = Literal["unparsed_target", "partial_enumeration", "unsupported_syntax"]

# resolved required 项的硬上限：plan/evaluate 的结构化 schema 只能逐项回显这么多条
# （见 state.PlanOutput.required_evidence / EvaluateOutput.coverage，有防漂移单测）。
# 超出部分记 partial_enumeration，绝不静默丢弃后仍报 complete。
MAX_REQUIRED_ITEMS = 10

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
# 文档扩展名：只有文档才应用 audit/changelog/plan 等**文件名**约定（三/五审发现3）
_DOC_EXTS = (".md", ".txt", ".rst", ".adoc")

# required-path lexer 认识的后缀。已摄取集合必须与 ingest.SUPPORTED_SUFFIXES 对齐
# （有 test_ingested_ext_lexer_matches_supported_suffixes 防漂移）；另加 P1.5 识别但
# 当前未摄取的显式后缀（.sql/.vue/.ts/.tsx 属 P1.6 摄取范围）。
_INGESTED_EXTS: frozenset[str] = frozenset({"md", "txt", "java", "yml", "yaml", "properties"})
_RECOGNIZED_UNINGESTED_EXTS: frozenset[str] = frozenset({"sql", "vue", "ts", "tsx", "jsx"})
_KNOWN_EXTS: frozenset[str] = _INGESTED_EXTS | _RECOGNIZED_UNINGESTED_EXTS

# *Test 命名约定的词边界规则（大小写敏感）：
# - 前缀 Test 后不得紧跟小写字母 → Testing/Testimony 不是测试；
# - 后缀 Test/Tests/Spec 前须是小写或数字 → Contest/Latest（结尾 test 小写）本就不命中；
# - 后缀 IT/ITs 前不得是大写 → SPLIT 不是集成测试。
_TEST_STEM_PREFIX = re.compile(r"^Test(?![a-z])")
_TEST_STEM_SUFFIX = re.compile(r"(?:[a-z0-9](?:Tests?|Spec)|(?<![A-Z])ITs?)$")


def _is_test_stem(stem: str) -> bool:
    return bool(_TEST_STEM_PREFIX.search(stem) or _TEST_STEM_SUFFIX.search(stem))


def classify_path(rel_path: str) -> EvidenceType:
    """rel_path → 证据类型。测试目录 > 文档名约定（仅文档扩展名） > 扩展名/生产目录大类。"""
    lower = rel_path.lower()
    segs = [seg for seg in lower.strip("/").split("/") if seg]
    base = segs[-1] if segs else lower
    orig_base = rel_path.strip("/").split("/")[-1] if rel_path.strip("/") else rel_path
    stem = orig_base.rsplit(".", 1)[0]  # 原始大小写，用于 Test 约定判断
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""

    # 1) 测试目录：目录语义最强（src/test 下的 .sql fixture 也不是生产 migration）
    if "test" in segs or "tests" in segs:
        return "test"

    in_production_dir = lower.startswith("src/main/") or "/src/main/" in "/" + lower

    # 2) 文档专属目录/文件名约定——只对文档扩展名生效，避免 AuditService.java /
    #    ChangelogService.java 这类生产源码被文件名规则吞成非生产证据（五审发现3）
    if ext in _DOC_EXTS:
        if "dev-log" in segs or "devlog" in segs or base.startswith(("dev-log", "devlog")):
            return "dev_log"
        if base.startswith("changelog"):
            return "dev_log"
        if "design" in segs:
            return "design_doc"
        if (
            "plans" in segs
            or "plan" in segs
            or "review" in segs
            or "reviews" in segs
            or "audit" in segs
            or "audits" in segs
            or base.startswith(("roadmap", "audit"))
            or "audit" in base
            or "phase-" in base
            or "规划" in rel_path
            or "计划" in rel_path
            or "路线图" in rel_path
        ):
            return "historical_plan"
        if base.startswith(("readme", "progress")):
            return "current_doc"
        if "docs" in segs and ext == ".md":
            return "current_doc"

    # 3) 精确扩展名/目录定大类
    if ext == ".sql" or "migration" in segs or "migrations" in segs or "flyway" in segs:
        return "migration"
    if ext in _FRONTEND_EXTS:
        return "frontend_source"
    # application.* 只按配置扩展名（.yml/.yaml/.properties/.toml）计入，不用裸前缀（三审发现3）
    if (
        ext in _CONFIG_EXTS
        or "docker-compose" in base
        or base == ".env"
        or base.startswith(".env.")
    ):
        return "production_config"

    # 4) 命名约定 test：仅在非生产目录、词边界、大小写敏感（四/五审发现5）
    if not in_production_dir and (
        _is_test_stem(stem) or ".test." in orig_base or ".spec." in orig_base
    ):
        return "test"

    if ext in _SOURCE_EXTS or in_production_dir:
        return "production_source"
    if ext == ".md":
        return "current_doc"
    return "other"


class RequiredEvidenceItem(BaseModel):
    """一条**已解析**的必需证据要求（确定性解析产出，不由 LLM 覆盖）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    item_id: str
    type: EvidenceType
    anchor: str
    path: str | None = None
    symbol: str | None = None


class UnresolvedConstraint(BaseModel):
    """一条**未解析/部分解析**的显式证据约束——存在但无法确定性定位到具体文件。

    只要非空，整体 status 即 ambiguous、full 门必然关闭；绝不进入 coverage。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    anchor: str
    reason: UnresolvedReason


class RequiredEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    items: tuple[RequiredEvidenceItem, ...] = ()
    # 不能替代必需证据，但允许作为补充引用（"只能作为补充"/"不要用 X 代替 Y"/"不要只引用 X"）
    forbidden_substitute_types: tuple[EvidenceType, ...] = ()
    # 不得直接引用（"不要引用 X"/"不得引用 X"）；被引用即不得 full
    forbidden_citation_types: tuple[EvidenceType, ...] = ()
    unresolved_constraints: tuple[UnresolvedConstraint, ...] = ()

    @property
    def status(self) -> Literal["none", "complete", "ambiguous"]:
        if self.unresolved_constraints:
            return "ambiguous"
        if self.items:
            return "complete"
        return "none"

    @property
    def has_requirements(self) -> bool:
        return bool(self.items or self.unresolved_constraints or self.forbidden_citation_types)

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(item.item_id for item in self.items)

    @property
    def non_substitutable_types(self) -> frozenset[EvidenceType]:
        """不得占用必需证据预算的类型：禁止引用蕴含禁止替代。"""
        return frozenset(self.forbidden_substitute_types) | frozenset(self.forbidden_citation_types)


class CoverageEntry(BaseModel):
    """某条 required item 的确定性覆盖结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    item_id: str
    covered: bool
    matched_evidence_ids: tuple[str, ...] = ()


# ---- 解析：词法与标记 ----------------------------------------------------

# 句界含英文 "."，但 file-token 会先被遮蔽再分句；不切顿号"、"，让枚举留同句。
_SENTENCE_SPLIT = re.compile(r"[。；;！？!?.\n]")
# 逗号/顿号：同一义务下的列表连接符（后续项按延续解析）
_SUB_SPLIT = re.compile(r"[，,、]")
# 义务对象内部的目标段连接词
_TARGET_SPLIT = re.compile(r"以及|和|与|及|或")

_NEG_TRIGGERS = ("不要", "请勿", "不得", "禁止", "勿使用", "勿引用")
_NEG_SOFT = ("不能用", "不能引用", "不应引用")
_SUPPLEMENT = ("只能作为补充", "仅作补充", "只作补充", "只能补充", "作为补充")
_SUBSTITUTE_MARKERS = ("代替", "替代", "冒充", "顶替", "充当", "当作", "当成")
# "不要只引用 X"/"不要仅依据 X"：X 不能独立支撑，但并非禁止引用
_ONLY_MARKER = re.compile(r"[只仅]")

_STRONG_DIRECTIVES = ("引用", "参见", "参照", "援引")
# 祈使前缀：强指令只有在祈使位置（"请分别引用…""并引用…"）才无条件成为义务；
# 名词用法（"历史会话中的引用是否还能显示""非法引用编号"）退回弱指令口径，须邻接证据信号。
_IMPERATIVE_PREFIX = re.compile(
    r"^(?:请|麻烦|务必|需要|需|须|必须|应该|应|要|你|您|能否|可否|帮我|帮忙"
    r"|并|且|也|同时|再|还|另外|然后|最后|分别|逐一|逐个|具体|实际|直接|一并|都|各|尽量"
    r"|\s|:|：|-|\*)*$"
)
_WEAK_DIRECTIVES = ("根据", "依据", "结合", "基于", "按照", "列出", "枚举", "罗列", "给出")
# 核验/比较谓词：其**左侧**是被要求核对的证据对象（"README 声称…""Java 配置…是否支持"）
_VERIFY_MARKERS = re.compile(
    r"是否(?:支持|一致|相符|符合|构成|属实|真|已|矛盾|冲突)"
    r"|声称|自称|宣称|号称|核对|核验|求证|对比|不一致|一致吗|矛盾吗"
)

# ASCII 边界的 file-token：起始必须是 ASCII 字母/数字/下划线（不吞中文），扩展名 2-11 位。
_FILE_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\-]*\.[A-Za-z][A-Za-z0-9]{1,10}")
# 类名/标识符：首字母大写 + 至少一个小写（PascalCase），排除 ALL_CAPS 常量（NO_ANSWER 等）。
_IDENT_SYMBOL = re.compile(r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*")
# "X 类" / "X 文件" → 明确点名（即便 X 是角色词，如 DTO 类）。
_CLASS_WORD = re.compile(r"([A-Za-z][A-Za-z0-9]*)\s*(?:类|文件)")
# 仓库约定的规范文档名（裸写即有身份，不退化为 type-only）；类型由 classify_path 推出。
_CANONICAL_DOCS = (
    "README",
    "PROGRESS",
    "CHANGELOG",
    "CONTRIBUTING",
    "LICENSE",
    "ROADMAP",
    "SECURITY",
)
_CANONICAL_DOC = re.compile(r"(?<![A-Za-z])(?:" + "|".join(_CANONICAL_DOCS) + r")(?![A-Za-z])")
# 泛化角色词（standalone）→ 集合/类别，无法定位具体文件 → unresolved。
_ROLE_WORDS = (
    "Repository",
    "Controller",
    "Service",
    "Mapper",
    "Entity",
    "DAO",
    "DTO",
    "Component",
    "Configuration",
    "Handler",
    "Resolver",
    "Filter",
    "Interceptor",
    "Listener",
    "Endpoint",
    "Provider",
    "Factory",
    "Manager",
    "Runner",
    "Worker",
    "Adapter",
    "Validator",
)
_ROLE_WORD = re.compile(r"(?<![A-Za-z])(?:" + "|".join(_ROLE_WORDS) + r")(?![A-Za-z])")
# 集合/枚举量词 → partial_enumeration；模糊指代 → unparsed_target。
_COLLECTIVE = re.compile(
    r"(?:每个|每一个|所有|全部|各个|逐个|列出|枚举|穷举|罗列)[^，。；、\n]{0,12}[A-Za-z0-9_]*"
)
_VAGUE = re.compile(r"(?:某个|某些|某一个|某几个|某项)[^，。；、\n]{0,12}")
# 目标名词：段内出现即"这是一个证据目标"，解析不出具体文件就必须记 unresolved；
# 完全不含目标名词的段是非目标尾语（谓语/说明部分），忽略。
_TARGET_NOUNS = (
    "实现",
    "源码",
    "代码",
    "文件",
    "配置",
    "文档",
    "测试",
    "迁移",
    "脚本",
    "类",
    "接口",
    "方法",
    "注解",
    "模块",
    "组件",
    "映射",
    "日志",
    "SQL",
    "schema",
)
_SYMBOL_STOP = frozenset(
    {
        "Java",
        "JavaScript",
        "TypeScript",
        "README",
        "PROGRESS",
        "GraphRAG",
        "Vue",
        "Spring",
        "Boot",
        "Mock",
    }
)

_TYPE_PATTERNS: list[tuple[re.Pattern[str], EvidenceType]] = [
    (re.compile(r"设计文档|设计说明"), "design_doc"),
    (re.compile(r"数据库迁移|迁移文件|迁移脚本|迁移\s*SQL|[Ff]lyway"), "migration"),
    (re.compile(r"生产配置|配置文件|application\.ya?ml|docker-compose"), "production_config"),
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

_ANCHOR_TRIM = " \t的了：:，,。、；;和与及或\n"
_MAX_ANCHOR_CHARS = 30


def _is_forbidding(seg: str) -> bool:
    return any(t in seg for t in _NEG_TRIGGERS) or any(t in seg for t in _NEG_SOFT)


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


def _has_target_signal(seg: str) -> bool:
    """是否邻接可解析的证据信号（路径/类型词/角色词/X 类/规范文档名）。

    只用于**弱指令与核验谓词的激活判定**：不含目标名词，避免"结合 RabbitMQ 实现异步"
    这类自然问法被当成证据约束（三审发现4）。
    """
    return bool(
        _FILE_TOKEN.search(seg)
        or _detect_type_hits(seg)
        or _ROLE_WORD.search(seg)
        or _CLASS_WORD.search(seg)
        or _CANONICAL_DOC.search(seg)
    )


def _strong_directive_pos(seg: str) -> int:
    positions = [seg.find(d) for d in _STRONG_DIRECTIVES if d in seg]
    return min(positions) if positions else -1


def _detect_obligations(part: str) -> list[tuple[int, str]]:
    """检测证据义务跨度 → [(对象在 part 中的起点, 对象文本)]。

    与"目标能否解析"完全解耦（五审路线裁决1）：祈使位置的强指令无条件激活，
    非祈使强指令（"…的引用是否还能显示"的名词用法）与弱指令/核验谓词一样须邻接证据
    信号。核验谓词的对象在标记**左侧**（"Java 配置和 Provider 实现是否支持…"）。
    """
    pos = _strong_directive_pos(part)
    if pos != -1 and (_IMPERATIVE_PREFIX.match(part[:pos]) or _has_target_signal(part)):
        start = pos
        for directive in _STRONG_DIRECTIVES:
            if part.startswith(directive, pos):
                start = pos + len(directive)
                break
        return [(start, part[start:])]
    weak_hits = [(part.find(d), len(d)) for d in _WEAK_DIRECTIVES if d in part]
    if weak_hits and _has_target_signal(part):
        index, length = min(weak_hits)
        return [(index + length, part[index + length :])]
    verify = _VERIFY_MARKERS.search(part)
    if verify is not None and _has_target_signal(part[: verify.start()]):
        return [(0, part[: verify.start()])]
    return []


def _split_targets(region: str) -> list[tuple[int, str]]:
    """义务对象 → [(段起点, 段文本)]；按列表连接词逐段结算（五审路线裁决2）。"""
    segments: list[tuple[int, str]] = []
    pos = 0
    for match in _TARGET_SPLIT.finditer(region):
        segments.append((pos, region[pos : match.start()]))
        pos = match.end()
    segments.append((pos, region[pos:]))
    return segments


def _infer_symbol_type(
    symbol: str, seg_types: set[EvidenceType], sentence_types: set[EvidenceType]
) -> EvidenceType:
    """点名类默认生产源码；测试命名约定归 test，但**显式生产语境优先**（五审发现2）。

    "请引用 OrderTest 的生产实现" 要求的是生产文件 OrderTest.java，不是同名测试；
    命名约定与 classify_path 共用词边界规则，保证解析与分类口径一致。
    """
    explicit_production = "production_source" in seg_types or "production_source" in sentence_types
    if _is_test_stem(symbol) and not explicit_production:
        return "test"
    if "frontend_source" in seg_types and "production_source" not in seg_types:
        return "frontend_source"
    return "production_source"


_RawItem = tuple[tuple[int, ...], EvidenceType, str, str | None, str | None]


class _Ledger:
    """约束跨度账本：resolved items + unresolved 约束 + 两类否定，按原文位置稳定排序。"""

    def __init__(self) -> None:
        self.raw: list[_RawItem] = []
        self.unresolved: list[UnresolvedConstraint] = []
        self.forbidden_substitute: list[EvidenceType] = []
        self.forbidden_citation: list[EvidenceType] = []
        self._seen_anchors: set[str] = set()

    def add_item(
        self,
        key: tuple[int, ...],
        etype: EvidenceType,
        anchor: str,
        *,
        path: str | None = None,
        symbol: str | None = None,
    ) -> None:
        self.raw.append((key, etype, anchor, path, symbol))

    def add_unresolved(self, anchor: str, reason: UnresolvedReason) -> None:
        anchor = anchor.strip(_ANCHOR_TRIM)[:_MAX_ANCHOR_CHARS].strip(_ANCHOR_TRIM)
        if anchor and anchor not in self._seen_anchors:
            self._seen_anchors.add(anchor)
            self.unresolved.append(UnresolvedConstraint(anchor=anchor, reason=reason))

    def forbid_substitute(self, etype: EvidenceType) -> None:
        if etype not in self.forbidden_substitute:
            self.forbidden_substitute.append(etype)

    def forbid_citation(self, etype: EvidenceType) -> None:
        if etype not in self.forbidden_citation:
            self.forbidden_citation.append(etype)


def _resolve_one(
    key: tuple[int, ...],
    seg: str,
    ledger: _Ledger,
    sentence_types: set[EvidenceType],
) -> bool:
    """结算单个目标段：resolved / unresolved / 非目标尾语。返回是否产出约束。"""
    consumed: list[tuple[int, int]] = []
    seg_types: set[EvidenceType] = {t for _s, t, _a in _detect_type_hits(seg)}
    produced = False

    def _free(match: re.Match[str]) -> bool:
        return not any(start <= match.start() < end for start, end in consumed)

    # 1) 显式路径/文件名：已知扩展名 → resolved path；未知扩展名 → unsupported_syntax
    for match in _FILE_TOKEN.finditer(seg):
        token = match.group()
        consumed.append(match.span())
        produced = True
        ext = token.rsplit(".", 1)[-1].lower()
        if ext in _KNOWN_EXTS:
            ledger.add_item((*key, match.start()), classify_path(token), token, path=token)
        else:
            ledger.add_unresolved(token, "unsupported_syntax")

    # 2) 规范文档名（裸 README/PROGRESS…）保留身份，不退化为 type-only（五审发现2）
    for match in _CANONICAL_DOC.finditer(seg):
        if not _free(match):
            continue
        consumed.append(match.span())
        produced = True
        name = match.group()
        ledger.add_item((*key, match.start()), classify_path(f"{name}.md"), name, symbol=name)

    # 3) "X 类 / X 文件" → 明确点名 symbol（含 DTO 类等缩写）
    for match in _CLASS_WORD.finditer(seg):
        if not _free(match):
            continue
        symbol = match.group(1)
        if symbol in _SYMBOL_STOP:
            continue
        consumed.append(match.span())
        produced = True
        ledger.add_item(
            (*key, match.start()),
            _infer_symbol_type(symbol, seg_types, sentence_types),
            symbol,
            symbol=symbol,
        )

    # 4) 独立角色词（Repository/Controller/Provider…）→ 集合/类别 → unresolved
    for match in _ROLE_WORD.finditer(seg):
        if not _free(match):
            continue
        consumed.append(match.span())
        produced = True
        ledger.add_unresolved(match.group(), "unparsed_target")

    # 5) PascalCase 标识符 symbol（排除已消费片段与 stop 词）
    for match in _IDENT_SYMBOL.finditer(seg):
        token = match.group()
        if not _free(match) or token in _SYMBOL_STOP:
            continue
        consumed.append(match.span())
        produced = True
        ledger.add_item(
            (*key, match.start()),
            _infer_symbol_type(token, seg_types, sentence_types),
            token,
            symbol=token,
        )

    # 6) 集合/枚举量词 → partial_enumeration；模糊指代 → unparsed_target
    for match in _COLLECTIVE.finditer(seg):
        produced = True
        ledger.add_unresolved(match.group(), "partial_enumeration")
    for match in _VAGUE.finditer(seg):
        produced = True
        ledger.add_unresolved(match.group(), "unparsed_target")

    # 7) 类型词 → type-only item
    for local_span, etype, anchor in _detect_type_hits(seg):
        produced = True
        ledger.add_item((*key, local_span), etype, anchor)

    # 8) 段结算兜底：含目标名词却无法定位 → unresolved（"关键实现""相关 Java 配置"）；
    #    完全不含目标名词 → 非目标尾语（"说明订单如何校验"），不产生约束。
    if not produced and any(noun in seg for noun in _TARGET_NOUNS):
        ledger.add_unresolved(seg, "unparsed_target")
        produced = True
    return produced


def _handle_forbidding(
    key: tuple[int, ...],
    part: str,
    ledger: _Ledger,
    sentence_types: set[EvidenceType],
) -> None:
    """否定子句：区分"不能替代"与"不得引用"，并保护替代结构的 Y 侧（隐含必需）。"""
    x_side, y_side = _split_substitution(part)
    substitute_only = y_side is not None or _ONLY_MARKER.search(part) is not None
    for _span, etype, _anchor in _detect_type_hits(x_side):
        if substitute_only:
            ledger.forbid_substitute(etype)
        else:
            ledger.forbid_citation(etype)
    if y_side is not None:
        _resolve_region((*key, len(x_side)), y_side, ledger, sentence_types, strict=False)


def _resolve_region(
    key: tuple[int, ...],
    region: str,
    ledger: _Ledger,
    sentence_types: set[EvidenceType],
    *,
    strict: bool,
) -> None:
    """逐目标段结算义务对象；strict（强/弱指令义务）下整段零产出也必须记 unresolved。"""
    handled = False
    for offset, segment in _split_targets(region):
        seg = segment.strip()
        if not seg:
            continue
        if _is_forbidding(seg):
            _handle_forbidding((*key, offset), seg, ledger, sentence_types)
            handled = True
            continue
        if _is_supplement(seg):
            for _span, etype, _anchor in _detect_type_hits(seg):
                ledger.forbid_substitute(etype)
            handled = True
            continue
        if _resolve_one((*key, offset), seg, ledger, sentence_types):
            handled = True
    if strict and not handled:
        ledger.add_unresolved(region, "unparsed_target")


def parse_required_evidence(question: str) -> RequiredEvidence:
    """确定性解析 → 权威 items + 两类禁止类型 + unresolved 约束（三态 fail-closed）。"""
    ledger = _Ledger()

    # 先遮蔽 file-token（无点占位符），保护显式路径不被句点截断、否定不跨句。
    placeholders: dict[str, str] = {}

    def _mask(match: re.Match[str]) -> str:
        key = f"\x00{len(placeholders)}\x00"
        placeholders[key] = match.group()
        return key

    masked = _FILE_TOKEN.sub(_mask, question)

    def _restore(text: str) -> str:
        for placeholder, value in placeholders.items():
            if placeholder in text:
                text = text.replace(placeholder, value)
        return text

    for si, sentence in enumerate(_SENTENCE_SPLIT.split(masked)):
        # 类型语境按整句取（枚举项常把"的生产实现"留在最后一段）
        sentence_types: set[EvidenceType] = {
            t for _s, t, _a in _detect_type_hits(_restore(sentence))
        }
        spans = _split_clauses(sentence)
        parts = [_restore(sentence[s:e]).strip() for s, e in spans]
        in_obligation = False
        bi = 0
        while bi < len(parts):
            part = parts[bi]
            if not part:
                bi += 1
                continue
            if _is_forbidding(part):
                in_obligation = False
                end = _forbidding_scope_end(parts, bi)
                # 用原文区间切片，保证 anchor 仍是问题原文子串（分隔符原样保留）
                scope = _restore(sentence[spans[bi][0] : spans[end - 1][1]]).strip()
                _handle_forbidding((si, bi), scope, ledger, sentence_types)
                bi = end
                continue
            if _is_supplement(part):
                in_obligation = False
                for _span, etype, _anchor in _detect_type_hits(part):
                    ledger.forbid_substitute(etype)
                bi += 1
                continue
            obligations = _detect_obligations(part)
            if obligations:
                in_obligation = True
                for start, region in obligations:
                    _resolve_region((si, bi, start), region, ledger, sentence_types, strict=True)
            elif in_obligation:
                # 逗号/顿号列表的延续项：同一义务下的后续目标段，不要求重复指令
                _resolve_region((si, bi, 0), part, ledger, sentence_types, strict=False)
            # 否则：无义务、非延续 → 不产生约束
            bi += 1

    return _build(ledger)


def _split_clauses(sentence: str) -> list[tuple[int, int]]:
    """句内按逗号/顿号切子句 → [(start, end)]（保留区间以便原文切片）。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _SUB_SPLIT.finditer(sentence):
        spans.append((pos, match.start()))
        pos = match.end()
    spans.append((pos, len(sentence)))
    return spans


def _forbidding_scope_end(parts: list[str], start: int) -> int:
    """否定子句的作用范围终点（不含）：向后吞掉不开启新义务的列表续段。

    否定的列表与替代结构常被逗号/顿号切开（"不要用 README、架构文档或测试代替实现"）；
    按 clause 逐段处理会把 X 侧截断成"不要用 README"、丢掉后续禁止类型，还会把
    "代替 Y" 结构误判成禁止引用。故把续段合回同一否定跨度。
    """
    index = start + 1
    while index < len(parts):
        nxt = parts[index]
        if not nxt:
            index += 1
            continue
        if _is_forbidding(nxt) or _is_supplement(nxt) or _detect_obligations(nxt):
            break
        index += 1
        if any(marker in nxt for marker in _SUBSTITUTE_MARKERS):
            break  # 替代结构已闭合，Y 侧到此为止
    return index


def _build(ledger: _Ledger) -> RequiredEvidence:
    ledger.raw.sort(key=lambda item: item[0])
    named_types = {
        etype
        for _k, etype, _a, path, symbol in ledger.raw
        if path is not None or symbol is not None
    }
    ids = count(1)
    seen: set[tuple[EvidenceType, str | None, str | None]] = set()
    items: list[RequiredEvidenceItem] = []
    overflow_anchor: str | None = None
    for _key, etype, anchor, path, symbol in ledger.raw:
        if symbol is None and path is None and etype in named_types:
            continue  # 该类型已有具体点名，丢弃 type-only
        dedup_key = (etype, path, symbol)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        if len(items) >= MAX_REQUIRED_ITEMS:
            # 超出 plan/evaluate schema 上限：记 partial_enumeration，不得仍报 complete
            overflow_anchor = overflow_anchor or anchor
            continue
        items.append(
            RequiredEvidenceItem(
                item_id=f"R{next(ids)}", type=etype, anchor=anchor, path=path, symbol=symbol
            )
        )
    if overflow_anchor is not None:
        ledger.add_unresolved(overflow_anchor, "partial_enumeration")
    return RequiredEvidence(
        items=tuple(items),
        forbidden_substitute_types=tuple(ledger.forbidden_substitute),
        forbidden_citation_types=tuple(ledger.forbidden_citation),
        unresolved_constraints=tuple(ledger.unresolved),
    )


# ---- 公共 token 词法（T23 事实校验复用，避免第二套 lexer 漂移） ---------------


def iter_path_tokens(text: str) -> list[str]:
    """文本中的显式文件 token（ASCII 边界、含扩展名），按出现顺序去重。

    与 required-evidence 解析共用 ``_FILE_TOKEN``：not_found 事实校验不得另建一套
    路径词法，否则两处对"什么算路径"的理解会随修复漂移。
    """
    return list(dict.fromkeys(match.group(0) for match in _FILE_TOKEN.finditer(text)))


def iter_symbol_tokens(text: str) -> list[str]:
    """文本中可用于身份比对的符号 token：PascalCase 类名 + 规范文档名。

    泛化角色词（Repository/Service…）与语言/框架停用词（Java/Vue…）被排除：它们能
    匹配上任意文件，用来做"该文件出现过"的事实校验只会制造假命中。**规范文档名
    （README/PROGRESS…）不受停用词表约束**——它们在 T22 里就是保留身份的点名对象，
    漏掉它们会让"已召回的 README 被写成未找到"逃过事实校验（c02）。
    """
    tokens = [
        match.group(0)
        for match in _IDENT_SYMBOL.finditer(text)
        if match.group(0) not in _SYMBOL_STOP and not _ROLE_WORD.fullmatch(match.group(0))
    ]
    tokens += [match.group(0) for match in _CANONICAL_DOC.finditer(text)]
    return list(dict.fromkeys(tokens))


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """区间并集（按起点排序，相接/相交即合并）：同一 token 被多条正则命中时只留一段。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def iter_target_spans(text: str) -> list[tuple[int, int]]:
    """可识别证据目标（路径 token + 符号 token）在文本中的位置，去重合并后按序返回。

    与 ``iter_path_tokens``/``iter_symbol_tokens`` 同口径、同一套正则：T23 的目标切分
    靠"连接词两侧是不是目标"来定位连接处，必须复用这里的词法，不得另建一套。
    """
    spans = [match.span() for match in _FILE_TOKEN.finditer(text)]
    spans += [
        match.span()
        for match in _IDENT_SYMBOL.finditer(text)
        if match.group(0) not in _SYMBOL_STOP and not _ROLE_WORD.fullmatch(match.group(0))
    ]
    spans += [match.span() for match in _CANONICAL_DOC.finditer(text)]
    return merge_spans(spans)


def path_matches_token(rel_path: str, token: str) -> bool:
    """token（显式路径 / 裸文件名 / 符号名）是否指向该 rel_path（大小写敏感）。

    身份口径与 ``_item_matches`` 一致：带 ``/`` 按完整路径后缀、带 ``.`` 按 basename
    全等、无扩展名按 stem 全等（Java 惯例：public 类与文件同名）。
    """
    base = rel_path.rsplit("/", 1)[-1]
    if "/" in token:
        return rel_path == token or rel_path.endswith("/" + token)
    if "." in token:
        return base == token
    return base.rsplit(".", 1)[0] == token


# ---- 确定性覆盖 matcher（只处理 resolved items；大小写敏感） -----------------


def _item_matches(item: RequiredEvidenceItem, rel_path: str) -> bool:
    if classify_path(rel_path) != item.type:
        return False
    base = rel_path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    if item.symbol is not None:
        return item.symbol == stem  # 大小写敏感（Java 惯例：public 类与文件同名）
    if item.path is not None:
        if "/" in item.path:
            return rel_path == item.path or rel_path.endswith("/" + item.path)
        return base == item.path  # 裸文件名 basename 全等（大小写敏感）
    return True  # type-only：类型已匹配即可


def _item_matches_token(item: RequiredEvidenceItem, token: str) -> bool:
    """token（缺口文本里的路径/符号）是否指向这条 required item。

    type-only 项（既无 path 也无 symbol）没有具体身份，永不参与绑定——否则"生产源码"
    这类泛化说法会把任意缺口都吸附过去。
    """
    if item.symbol is not None:
        return token.rsplit("/", 1)[-1].rsplit(".", 1)[0] == item.symbol
    if item.path is not None:
        return path_matches_token(item.path, token)
    return False


def bound_required_ids(required: RequiredEvidence, text: str) -> tuple[str, ...]:
    """文本里的路径/符号 token 指向哪些 required item（与覆盖 matcher 同一身份口径）。

    T24 二审发现3 用它做**跨轨目标身份**：LLM 把"RagService 生产源码"报成缺口时，
    仅靠归一化全等比较无法与确定性轨的 ``RagService`` 对上（限定词一变就失效），
    于是同一方面会既出现原始缺失项、又出现确定性三态说明，自相矛盾。
    """
    tokens = [*iter_path_tokens(text), *iter_symbol_tokens(text)]
    if not tokens:
        return ()
    return tuple(
        item.item_id
        for item in required.items
        if any(_item_matches_token(item, token) for token in tokens)
    )


def target_tokens_all_match(item: RequiredEvidenceItem, text: str) -> bool:
    """文本里的**每一个**目标 token 是否都指向这条 item（与绑定/覆盖同一身份口径）。

    ``bound_required_ids`` 只回答"有没有 token 指向它"；判"整条缺口能不能交给这条 item
    的确定性说明"还需要反向条件——没有别的目标。且不能靠数 ``iter_target_spans``：
    相邻/相接的跨度会被 ``merge_spans`` 合成一段（``RagService.javaOrderService`` 这类
    无分隔符拼接只剩一段），只数跨度会漏掉第二个目标，故按 token 逐个核对。
    """
    tokens = [*iter_path_tokens(text), *iter_symbol_tokens(text)]
    return bool(tokens) and all(_item_matches_token(item, token) for token in tokens)


def matching_item_ids(required: RequiredEvidence, rel_path: str) -> tuple[str, ...]:
    """该路径直接命中的 required item_id（与 ``compute_coverage`` 同一 matcher）。

    T24 的跨轮保留要按"这条证据锚定了哪些方面"排序，必须复用同一身份口径：
    另建一套匹配会让"保留下来的证据"与"覆盖矩阵认可的证据"随修复漂移。
    """
    return tuple(item.item_id for item in required.items if _item_matches(item, rel_path))


def compute_coverage(
    required: RequiredEvidence,
    cited: Iterable[tuple[str, str]],
) -> tuple[CoverageEntry, ...]:
    """按真实引用 (evidence_id, rel_path) 逐项算权威覆盖矩阵（不含 unresolved）。"""
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


def forbidden_citation_hits(
    required: RequiredEvidence,
    cited: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str, EvidenceType], ...]:
    """用户明确禁止直接引用的类型却出现在最终引用中 → (evidence_id, rel_path, type)。"""
    banned = set(required.forbidden_citation_types)
    if not banned:
        return ()
    hits: list[tuple[str, str, EvidenceType]] = []
    for evidence_id, rel_path in cited:
        etype = classify_path(rel_path)
        if etype in banned:
            hits.append((evidence_id, rel_path, etype))
    return tuple(hits)


def required_satisfied(
    required: RequiredEvidence,
    coverage: tuple[CoverageEntry, ...],
    *,
    cited: Iterable[tuple[str, str]],
) -> bool:
    """full 的必要条件：resolved 全覆盖、无 unresolved、无禁止引用类型被引用。"""
    return (
        not required.unresolved_constraints
        and all_required_covered(coverage)
        and not forbidden_citation_hits(required, cited)
    )
