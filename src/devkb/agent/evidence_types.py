"""P1.5 T22：证据类型分层、逐项 required-evidence 解析与确定性 coverage（RT-01/05）。

裁决语义：A 的 schema + B 的确定性裁决（2026-07-24）；**结构性 fail-closed**（2026-07-25，
第四轮复审裁决）。

错误 full 的共同根因是"用户给出显式证据约束，但确定性解析器漏掉全部或部分目标 →
空/不完整 required 清单使 full 门错误放行"。故引入**确定性三态**：

- ``RequiredEvidence.status``：``none``（无显式证据约束）/ ``complete``（显式约束全部
  被确定性解析）/ ``ambiguous``（至少一个目标未解析或只解析了一部分）。
- 已解析的 ``items`` 走 ``compute_coverage`` 逐项匹配；未解析/部分解析的目标进入
  ``unresolved_constraints``（原文 anchor + 固定 reason 枚举），**永不进入 coverage、
  也不用 type=other 伪造可被任意文件满足的 item**。
- ``full`` 的必要条件 = 每个 resolved item 被直接引用覆盖 **且** ``unresolved_constraints``
  为空。route/finalize 把 unresolved 视作覆盖缺口（见 graph/nodes）。

解析设计（防过拟合、防组合错误 full）：

- **路径遮蔽**：先用有明确 ASCII 边界的 file-token 扫描器把路径遮蔽成无点占位符，
  再按句界（含英文 ``.``）分句——无空格中文不被吞、否定不跨句、显式路径不被截断。
- **指令作用域**：点名/类型硬约束只在带证据指令的子句生效；强指令（引用/参见/参照）
  直接激活，弱指令（根据/依据/结合/列出…）仅在邻接路径/证据类型词/角色词/"X 类"时激活。
- **枚举列表**：句号/分号/换行是强句界；逗号/顿号是同一指令下的列表连接符（后续项
  作为延续解析，不要求重复指令），避免"引用 A，B"丢掉 B。
- **集合/类别/模糊/未知语法** → unresolved，而非静默空 required。

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

# required-path lexer 认识的后缀。已摄取集合必须与 ingest.SUPPORTED_SUFFIXES 对齐
# （有 test_ingested_ext_lexer_matches_supported_suffixes 防漂移）；另加 P1.5 识别但
# 当前未摄取的显式后缀（.sql/.vue/.ts/.tsx 属 P1.6 摄取范围）。
_INGESTED_EXTS: frozenset[str] = frozenset({"md", "txt", "java", "yml", "yaml", "properties"})
_RECOGNIZED_UNINGESTED_EXTS: frozenset[str] = frozenset({"sql", "vue", "ts", "tsx", "jsx"})
_KNOWN_EXTS: frozenset[str] = _INGESTED_EXTS | _RECOGNIZED_UNINGESTED_EXTS


def classify_path(rel_path: str) -> EvidenceType:
    """rel_path → 证据类型。可信目录优先、生产目录优先于文件名约定、Test 约定大小写敏感。"""
    lower = rel_path.lower()
    segs = lower.strip("/").split("/")
    base = segs[-1] if segs else lower
    orig_base = rel_path.strip("/").split("/")[-1] if rel_path.strip("/") else rel_path
    stem = orig_base.rsplit(".", 1)[0]  # 原始大小写，用于 Test 约定判断

    # 1) 可信（非生产）目录优先——目录语义强于文件名约定
    if "test" in segs or "tests" in segs:
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

    # 2) 生产目录（src/main）优先于 *Test 命名约定：main 下的 OrderTest.java 仍是生产源码
    in_production_dir = lower.startswith("src/main/") or "/src/main/" in ("/" + lower)

    # 3) 命名约定 test：仅在非生产目录、且大小写敏感——不把 Contest/Latest 当 Test
    if not in_production_dir and (
        stem.endswith(("Test", "Tests", "IT", "ITs", "Spec"))
        or stem.startswith("Test")
        or ".test." in orig_base
        or ".spec." in orig_base
    ):
        return "test"

    # 4) 生产目录内 / 通用扩展名识别
    if lower.endswith(".sql") or "/migration/" in lower or "flyway" in segs:
        return "migration"
    if lower.endswith(_FRONTEND_EXTS):
        return "frontend_source"
    # application.* 只按配置扩展名（.yml/.yaml/.properties）计入，不用裸前缀（三审发现3）
    if (
        lower.endswith(_CONFIG_EXTS)
        or "docker-compose" in base
        or base == ".env"
        or base.startswith(".env.")
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
    """一条**已解析**的必需证据要求（确定性解析产出，不由 LLM 覆盖）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    item_id: str
    type: EvidenceType
    anchor: str
    path: str | None = None
    symbol: str | None = None


class UnresolvedConstraint(BaseModel):
    """一条**未解析/部分解析**的显式证据约束——存在但无法确定性定位到具体文件。

    只要非空，整体 status 即 ambiguous、full 门必然关闭；absolutely 不进入 coverage。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    anchor: str
    reason: UnresolvedReason


class RequiredEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    items: tuple[RequiredEvidenceItem, ...] = ()
    forbidden_substitute_types: tuple[EvidenceType, ...] = ()
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
        return bool(self.items) or bool(self.unresolved_constraints)

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

# 句界含英文 "."，但 file-token 会先被遮蔽再分句；不切顿号"、"，让枚举留同句。
_SENTENCE_SPLIT = re.compile(r"[。；;！？!?.\n]")
_SUB_SPLIT = re.compile(r"[，,、]")
_NEG_TRIGGERS = ("不要", "请勿", "不得", "禁止", "勿使用", "勿引用")
_NEG_SOFT = ("不能用", "不能引用", "不应引用")
_SUPPLEMENT = ("只能作为补充", "仅作补充", "只作补充", "只能补充", "作为补充")
_SUBSTITUTE_MARKERS = ("代替", "替代", "冒充", "顶替", "充当", "当作", "当成")
_STRONG_DIRECTIVES = ("引用", "参见", "参照", "援引")
_WEAK_DIRECTIVES = ("根据", "依据", "结合", "基于", "列出", "枚举", "罗列", "给出")

# ASCII 边界的 file-token：起始必须是 ASCII 字母/数字/下划线（不吞中文），扩展名 2-11 位。
_FILE_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\-]*\.[A-Za-z][A-Za-z0-9]{1,10}")
# 类名/标识符：首字母大写 + 至少一个小写（PascalCase），排除 ALL_CAPS 常量（NO_ANSWER 等）。
_IDENT_SYMBOL = re.compile(r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*")
# "X 类" / "X 文件" → 明确点名（即便 X 是角色词，如 DTO 类）。
_CLASS_WORD = re.compile(r"([A-Za-z][A-Za-z0-9]*)\s*(?:类|文件)")
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
)
_ROLE_WORD = re.compile(r"(?<![A-Za-z])(?:" + "|".join(_ROLE_WORDS) + r")(?![A-Za-z])")
# 集合/枚举量词 → partial_enumeration；模糊指代 → unparsed_target。
_COLLECTIVE = re.compile(
    r"(?:每个|每一个|所有|全部|各个|逐个|列出|枚举|穷举|罗列)[^，。；、\n]{0,12}"
)
_VAGUE = re.compile(r"(?:某个|某些|某一个|某几个|某项)[^，。；、\n]{0,12}")
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
_TEST_SUFFIX = ("Test", "Tests", "IT", "ITs", "Spec")

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


def _has_evidence_context(seg: str) -> bool:
    """弱指令/延续项是否邻接可解析的证据信号（路径/类型词/角色词/X 类）。"""
    return bool(
        _FILE_TOKEN.search(seg)
        or _detect_type_hits(seg)
        or _ROLE_WORD.search(seg)
        or _CLASS_WORD.search(seg)
    )


def _strong_directive_pos(seg: str) -> int:
    positions = [seg.find(d) for d in _STRONG_DIRECTIVES if d in seg]
    return min(positions) if positions else -1


def _is_activated(seg: str) -> bool:
    if _strong_directive_pos(seg) != -1:
        return True
    return any(d in seg for d in _WEAK_DIRECTIVES) and _has_evidence_context(seg)


def _looks_like_list_item(seg: str) -> bool:
    """延续子句是否像列表项（连接词/空白后紧跟 ASCII 标识符或路径）。"""
    return re.match(r"^(?:和|与|及|以及|或|、|\s)*[A-Za-z0-9_]", seg) is not None


def _infer_symbol_type(symbol: str, seg_types: set[EvidenceType]) -> EvidenceType:
    """点名类默认生产源码；测试后缀归 test；明显前端上下文归前端。绝不判 migration/config。"""
    if symbol.endswith(_TEST_SUFFIX):
        return "test"
    if "frontend_source" in seg_types and "production_source" not in seg_types:
        return "frontend_source"
    return "production_source"


_RawItem = tuple[tuple[int, ...], EvidenceType, str, str | None, str | None]


def _extract(
    key: tuple[int, ...],
    span: str,
    seg_types: set[EvidenceType],
    raw: list[_RawItem],
    unresolved: list[UnresolvedConstraint],
    seen_unresolved: set[str],
) -> None:
    """从已激活子句提取 resolved items 与 unresolved 约束（key 前缀用于稳定排序）。"""

    def add_unresolved(anchor: str, reason: UnresolvedReason) -> None:
        anchor = anchor.strip()
        if anchor and anchor not in seen_unresolved:
            seen_unresolved.add(anchor)
            unresolved.append(UnresolvedConstraint(anchor=anchor, reason=reason))

    produced = False
    consumed_spans: list[tuple[int, int]] = []

    # 1) file tokens：已知扩展名 → resolved path；未知扩展名 → unsupported_syntax
    for match in _FILE_TOKEN.finditer(span):
        token = match.group()
        consumed_spans.append((match.start(), match.end()))
        produced = True
        ext = token.rsplit(".", 1)[-1].lower()
        if ext in _KNOWN_EXTS:
            raw.append(((*key, match.start()), classify_path(token), token, token, None))
        else:
            add_unresolved(token, "unsupported_syntax")

    # 2) "X 类 / X 文件" → 明确点名 symbol（含 DTO 类等缩写）
    for match in _CLASS_WORD.finditer(span):
        if any(s <= match.start() < e for s, e in consumed_spans):
            continue
        consumed_spans.append((match.start(), match.end()))
        symbol = match.group(1)
        if symbol in _SYMBOL_STOP:
            continue
        produced = True
        raw.append(
            ((*key, match.start()), _infer_symbol_type(symbol, seg_types), symbol, None, symbol)
        )

    # 3) 独立角色词（Repository/Controller/DTO…）→ 集合/类别 → unresolved
    #    标记已消费，避免 step 4 又把它当作已解析 symbol。
    for match in _ROLE_WORD.finditer(span):
        if any(s <= match.start() < e for s, e in consumed_spans):
            continue
        consumed_spans.append((match.start(), match.end()))
        produced = True
        add_unresolved(match.group(), "unparsed_target")

    # 4) PascalCase 标识符 symbol（排除已消费片段与 stop 词）
    for match in _IDENT_SYMBOL.finditer(span):
        token = match.group()
        if any(s <= match.start() < e for s, e in consumed_spans):
            continue
        if token in _SYMBOL_STOP:
            continue
        consumed_spans.append((match.start(), match.end()))
        produced = True
        raw.append(
            ((*key, match.start()), _infer_symbol_type(token, seg_types), token, None, token)
        )

    # 5) 集合/枚举量词 → partial_enumeration；模糊指代 → unparsed_target
    for match in _COLLECTIVE.finditer(span):
        produced = True
        add_unresolved(match.group(), "partial_enumeration")
    for match in _VAGUE.finditer(span):
        produced = True
        add_unresolved(match.group(), "unparsed_target")

    # 6) 类型词 → type-only item
    for local_span, etype, anchor in _detect_type_hits(span):
        produced = True
        raw.append(((*key, local_span), etype, anchor, None, None))

    # 7) fail-closed 兜底：强指令激活但完全没解析出任何目标 → 视为未解析目标
    if not produced:
        pos = _strong_directive_pos(span)
        if pos != -1:
            obj = span[pos:]
            for d in _STRONG_DIRECTIVES:
                if span.startswith(d, pos):
                    obj = span[pos + len(d) :]
                    break
            add_unresolved(obj.strip(" 的了：:，,。")[:30], "unparsed_target")


def parse_required_evidence(question: str) -> RequiredEvidence:
    """确定性解析 → 权威 items + 禁止替代类型 + unresolved 约束（三态 fail-closed）。"""
    forbidden: list[EvidenceType] = []
    raw: list[_RawItem] = []
    unresolved: list[UnresolvedConstraint] = []
    seen_unresolved: set[str] = set()

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
        mode_positive = False
        for bi, raw_sub in enumerate(_SUB_SPLIT.split(sentence)):
            sub = _restore(raw_sub).strip()
            if not sub:
                continue
            seg_types: set[EvidenceType] = {t for _s, t, _a in _detect_type_hits(sub)}
            if _is_forbidding(sub):
                mode_positive = False
                x_side, y_side = _split_substitution(sub)
                for _s, etype, _a in _detect_type_hits(x_side):
                    if etype not in forbidden:
                        forbidden.append(etype)
                if y_side is not None:  # 被保护侧是肯定必需（替代结构即隐含引用要求）
                    y_types: set[EvidenceType] = {t for _s, t, _a in _detect_type_hits(y_side)}
                    _extract((si, bi), y_side, y_types, raw, unresolved, seen_unresolved)
                continue
            if _is_supplement(sub):
                mode_positive = False
                for _s, etype, _a in _detect_type_hits(sub):
                    if etype not in forbidden:
                        forbidden.append(etype)
                continue
            if _is_activated(sub):
                mode_positive = True
                _extract((si, bi), sub, seg_types, raw, unresolved, seen_unresolved)
            elif mode_positive and (_has_evidence_context(sub) or _looks_like_list_item(sub)):
                _extract((si, bi), sub, seg_types, raw, unresolved, seen_unresolved)
            # 否则：无指令、非延续 → 不产生约束

    raw.sort(key=lambda item: item[0])
    named_types = {
        etype for _k, etype, _a, path, symbol in raw if path is not None or symbol is not None
    }
    ids = count(1)
    seen: set[tuple[EvidenceType, str | None, str | None]] = set()
    items: list[RequiredEvidenceItem] = []
    for _key, etype, anchor, path, symbol in raw:
        if symbol is None and path is None and etype in named_types:
            continue  # 该类型已有具体点名，丢弃 type-only
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
        unresolved_constraints=tuple(unresolved),
    )


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


def required_satisfied(required: RequiredEvidence, coverage: tuple[CoverageEntry, ...]) -> bool:
    """full 的必要条件：resolved 全覆盖 **且** 无 unresolved（结构性 fail-closed）。"""
    return not required.unresolved_constraints and all_required_covered(coverage)
