"""T26.3 配置类问题的确定性五维校验（规格 §8 第 3 句，RT-19 / 案例八）。

四条纪律（全部确定性、零 LLM 调用）：

1. **五维分立呈现，不合并**。``scope`` / ``fallback`` / ``required·non-empty`` /
   ``length·strength`` / ``runtime-validation`` 各占一个小节，任一维命中**推不出**其余
   维成立。案例八的原始失败正是把"Compose 必填"读成"secret 强度足够、生产环境安全"
   （《P1后真实仓库可用性测试问题记录》§11.4 人工判定逐字："把'非空/特定工作流'扩大
   为'强度/生产环境安全'"）。
2. **错误消息与注释被结构性屏蔽，不是靠措辞约束**。``mask_advisory`` 在关键词扫描前
   删掉整个 ``${…}`` 片段与各类注释；插值**形态**判定（``_interp_form``）独立地只看
   操作符前缀、从不读消息体。两条通路无输入交叉，故
   ``${RAG_JWT_SECRET:?...generate 32+ random chars}`` 里的 "32+" 不可能被
   ``length·strength`` 扫到——这正是判据第 2 句"错误消息或文档建议不得冒充代码强制
   条件"的执行点。
3. **Compose 与 Spring 必须分方言**。``${VAR:?err}`` 在 Compose 是"未设置或为空即报错"，
   在 Spring 里冒号后**整段都是默认值**。同一串字符在两个载体里语义相反，只做一套解析
   必然在其中一侧产出错误结论。
4. **只说片段内、只说检出**。未检出恒写"未检出"，推不出"该约束不存在"（片段可能被
   截断，约束也可能在别处）；命中恒限定"本次交付引用片段内"，推不出"该配置最终生效"
   （需 profile/运行时分析，P1.5 无方法级 chunk 也无调用图）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from devkb.agent.evidence_types import classify_path
from devkb.agent.not_found import _render_refs

# 表 C · 配置问题触发词（**有序**闭合表；每一词条的逐字出处见任务合同，扩充需新任务）。
# 出处仅限 contract_dev(c02/c08)、contract_dev_extended(e02/e03/e05)、规格 §8 第 3 句、
# 问题记录 §11——**无任何词条的出处需引用 holdout**。
# 被词法包含的候选（配置项 ⊂ 配置、docker-compose ⊂ compose）不重复登记；`.env` 已排除，
# 理由见合同「表 C」：其配置语境出处已被 `配置` 覆盖，留着只会额外命中 c13 注入题。
CONFIG_TERMS: tuple[str, ...] = (
    "配置",
    "compose",
    "application.yml",
    "环境变量",
    "默认值",
    "secret",
)

# 表 D · 载体档位。`other` 不参与任何强制条件维度，只计入披露分句。
Carrier = Literal["compose", "app_config", "env_file", "source", "other"]

_COMPOSE_BASENAMES = frozenset({"compose.yml", "compose.yaml"})
_APP_CONFIG_EXTS = frozenset({".yml", ".yaml", ".properties", ".toml"})
_ENFORCING: frozenset[str] = frozenset({"compose", "app_config", "env_file", "source"})
# 插值方言：env_file（dotenv）与 other 不判定插值语义，故不在两表内。
_COMPOSE_DIALECT: frozenset[str] = frozenset({"compose"})
_SPRING_DIALECT: frozenset[str] = frozenset({"app_config", "source"})

# 表 E · 关键词（闭合）。全部作用于 **mask_advisory 之后**的文本。
# @NotNull 只禁 null、不禁空串，标签"非空注解"比它宽一档——已登记 backlog T263-P3-01，
# 影响面由 U7b 的 @NotNull-only 形态断言固定住。
_NONEMPTY_ANNOTATIONS = ("@NotBlank", "@NotEmpty", "@NotNull")
_LENGTH_ANNOTATIONS = ("@Size", "@Length", "@Min", "@Max", "@Pattern", "@Digits")
_RUNTIME_TOKENS = (
    "@Validated",
    "@Valid",
    "Assert.notNull",
    "Assert.hasText",
    "Assert.isTrue",
    "throw new IllegalStateException",
    "throw new IllegalArgumentException",
)
_LENGTH_CALL = ".length()"
_COMPARISONS = ("<", ">", "==", "!=")

# 变量名允许点号（Spring 的 `${rag.jwt.secret:default}` 是 property 路径而非环境变量名），
# 但**不允许连字符**——Compose 的 `${VAR-default}` 里 `-` 是默认值操作符，放进名字里会把
# 该形态整个吞掉。代价：kebab-case 的 Spring key（`${a.b-c:x}`）解析到 `a.b` 后 rest 以
# `-` 开头，在 Spring 方言下判 unknown → 不计入任何维度，属安全方向的假阴性。
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)([^}]*)\}")
_ANY_INTERPOLATION = re.compile(r"\$\{[^}]*\}")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# `(?<!:)` 让 http:// 不被当成注释；`#` 仅在行首或空白之后才算注释，避免吃掉串内 `#`。
_SLASH_COMMENT = re.compile(r"(?<!:)//[^\n]*")
_HASH_COMMENT = re.compile(r"(?m)(?:^|(?<=[ \t]))#[^\n]*")
_COMMENT_PATTERNS = (_BLOCK_COMMENT, _SLASH_COMMENT, _HASH_COMMENT)

_PREFIX = "（本次配置分层校验："
_SCOPE_DECL = "不同载体的插值由不同工具解析，本阶段不判定某条配置在某个启动路径下最终是否生效。"
# 末句刻意避开 T26.1 的闭合全称词表（_UNIVERSAL_TERMS）与本任务表 F：写"未覆盖…其余
# 配置行"而不是"不代表全部配置"，写"以上未计入"而不是"均未计入"（"均"在全称词表内）。
_TAIL = "本校验只覆盖上述引用片段，未覆盖同一文件中未被引用的其余配置行。）"

_CARRIER_LABELS: tuple[tuple[str, str], ...] = (
    ("compose", "Compose 文件"),
    ("app_config", "应用配置文件"),
    ("env_file", "环境变量文件"),
    ("source", "生产源码"),
    ("other", "其他证据"),
)


def is_config_question(question: str) -> tuple[str, ...]:
    """问题命中了表 C 的哪几个触发词（按表声明顺序，去重）。

    只是**词法事实**。推不出"这是配置问题"，也推不出"这不是配置问题"——表外的问法
    （例如只说"JWT 的 key 是从哪里来的"）不命中，命中也可能只是巧合。调用方据此决定
    是否输出五维段，漏判的后果是**整段省略**（退化为 T26.1 的范围句），而不是输出
    错误分层。
    """
    normalized = unicodedata.normalize("NFKC", question).lower()
    return tuple(term for term in CONFIG_TERMS if term in normalized)


def carrier_of(rel_path: str) -> Carrier:
    """rel_path → 表 D 载体档位。只在 ``classify_path`` 的**结果之上**再分档。

    ``classify_path``（T22 六审冻结）负责"是不是生产配置/生产源码"，本函数只负责"是哪
    一种配置载体"——两者职责不重叠，故本任务零修改 ``evidence_types``。
    """
    kind = classify_path(rel_path)
    if kind == "production_source":
        return "source"
    if kind != "production_config":
        return "other"
    base = rel_path.strip("/").split("/")[-1].lower()
    if "docker-compose" in base or base in _COMPOSE_BASENAMES:
        return "compose"
    if base == ".env" or base.startswith(".env."):
        return "env_file"
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    return "app_config" if ext in _APP_CONFIG_EXTS else "other"


def mask_advisory(text: str) -> str:
    """删除**建议性文本**：块注释、行注释、以及整个 ``${…}`` 片段（连同错误消息）。

    判据第 2 句的结构性执行点。只供关键词扫描使用——插值**形态**判定读的是原文的操作符
    前缀，与本函数无输入关系，所以屏蔽不会让必填形态漏判。

    块注释按其行数替换为等量换行，避免把跨行内容并到一行——``_length_call_constrained``
    是按行判定的，塌行会让上一行的比较运算符和下一行的 ``.length()`` 假性同行。
    """
    masked = _BLOCK_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    masked = _SLASH_COMMENT.sub("", masked)
    masked = _HASH_COMMENT.sub("", masked)
    return _ANY_INTERPOLATION.sub(" ", masked)


def _comment_lines(text: str) -> int:
    """含注释的行数（按行号去重；块注释按其跨越的行数计）。仅用于披露计数。"""
    touched: set[int] = set()
    for pattern in _COMMENT_PATTERNS:
        for match in pattern.finditer(text):
            first = text.count("\n", 0, match.start())
            touched.update(range(first, first + match.group(0).count("\n") + 1))
    return len(touched)


def _interp_form(rest: str, carrier: Carrier) -> str:
    """插值操作符前缀 → 形态。``rest`` 是变量名之后、``}`` 之前的全部字符。

    **只读前缀，不读消息体**。Spring 档的 ``:?`` 明确返回 ``unknown``——Spring 把冒号后
    整段当默认值，把它认作必填就是判据第 2 句要防的错误；fail-closed 宁可不计。
    """
    if carrier in _COMPOSE_DIALECT:
        if rest.startswith(":?"):
            return "required_nonempty"
        if rest.startswith("?"):
            return "required_set"
        if rest.startswith((":-", "-")):
            return "fallback"
        return "unknown"
    if carrier in _SPRING_DIALECT and rest.startswith(":") and not rest.startswith(":?"):
        return "fallback"
    return "unknown"


def _has_message(rest: str) -> bool:
    """插值里是否带错误消息文本。只用于披露计数，对任何维度判定**无输入路径**。"""
    if rest.startswith(":?"):
        return bool(rest[2:].strip())
    return bool(rest[1:].strip()) if rest.startswith("?") else False


@dataclass(frozen=True)
class FragmentFacts:
    """一条交付引用片段的五维词法事实。每个布尔只表示"本片段内检出/未检出"。"""

    fallback: bool = False
    required_compose_nonempty: bool = False
    required_compose_set: bool = False
    required_annotation: bool = False
    length_strength: bool = False
    runtime_validation: bool = False
    message_count: int = 0
    comment_lines: int = 0


def scan_fragment(content: str, carrier: Carrier) -> FragmentFacts:
    """按表 D 方言 + 表 E 关键词扫描单个片段。``other`` 档恒返回全空（不参与维度）。"""
    if carrier not in _ENFORCING:
        return FragmentFacts()
    fallback = required_nonempty = required_set = False
    messages = 0
    for match in _INTERPOLATION.finditer(content):
        rest = match.group(2)
        form = _interp_form(rest, carrier)
        if form == "fallback":
            fallback = True
        elif form == "required_nonempty":
            required_nonempty = True
        elif form == "required_set":
            required_set = True
        if _has_message(rest):
            messages += 1
    masked = mask_advisory(content)
    return FragmentFacts(
        fallback=fallback,
        required_compose_nonempty=required_nonempty,
        required_compose_set=required_set,
        required_annotation=any(token in masked for token in _NONEMPTY_ANNOTATIONS),
        length_strength=(
            any(token in masked for token in _LENGTH_ANNOTATIONS)
            or _length_call_constrained(masked)
        ),
        runtime_validation=any(token in masked for token in _RUNTIME_TOKENS),
        message_count=messages,
        comment_lines=_comment_lines(content),
    )


def _length_call_constrained(masked: str) -> bool:
    """同一行内 ``.length()`` **之后**出现比较运算符。

    位置序是刻意的：``log.info("len={}", secret.length())`` 之后没有比较运算符，判 False。
    ``if (32 < secret.length())`` 这类比较在前的写法同样判 False——**假阴性是安全方向**
    （少报一个维度只会让文案更保守），而假阳性会凭空造出一条"检出长度约束"，正是案例八
    要防的过度声称。
    """
    for line in masked.splitlines():
        index = line.find(_LENGTH_CALL)
        if index >= 0 and any(op in line[index + len(_LENGTH_CALL) :] for op in _COMPARISONS):
            return True
    return False


def _dimension(title: str, labels: Sequence[str], noun: str) -> str:
    if labels:
        return f"{title}：{len(labels)} 条片段内检出{noun}（{_render_refs(labels)}）。"
    return f"{title}：本次交付引用片段内未检出{noun}。"


def config_layer_note(rows: Sequence[tuple[str, str, str]]) -> str:
    """渲染五维段；``rows`` 为 ``(evidence_id, rel_path, content)``。

    行身份是 ``(evidence_id, rel_path)`` 而**不是**路径：同一文件的两个 chunk 在 Answer
    ``citations`` 里就是两条（沿用 T26.2 PG-03 的口径）。行标签渲染为
    ``rel_path(evidence_id)``——圆括号而非方括号，避免撞上对 ``[E#]`` 的 Answer L0 扫描。

    ``rows`` 为空返回空串（无可分层对象）；非空时恒返回一段，即使五维全部"未检出"——
    按用户裁决 H3=A，"本次没验证到强度/运行时校验"正是案例八最需要被说出来的事实。
    """
    if not rows:
        return ""
    buckets: dict[str, list[str]] = {name: [] for name, _label in _CARRIER_LABELS}
    fallback: list[str] = []
    required_nonempty: list[str] = []
    required_set: list[str] = []
    required_annotation: list[str] = []
    length: list[str] = []
    runtime: list[str] = []
    messages = comments = 0

    for evidence_id, rel_path, content in rows:
        carrier = carrier_of(rel_path)
        label = f"{rel_path}({evidence_id})"
        buckets[carrier].append(label)
        facts = scan_fragment(content, carrier)
        for hit, sink in (
            (facts.fallback, fallback),
            (facts.required_compose_nonempty, required_nonempty),
            (facts.required_compose_set, required_set),
            (facts.required_annotation, required_annotation),
            (facts.length_strength, length),
            (facts.runtime_validation, runtime),
        ):
            if hit:
                sink.append(label)
        messages += facts.message_count
        comments += facts.comment_lines

    clauses = [
        # `other` 不列行标签——列出来会暗示它们参与了维度判定
        f"{label} {len(labels)} 条"
        if name == "other"
        else f"{label} {len(labels)} 条（{_render_refs(labels)}）"
        for name, label in _CARRIER_LABELS
        if (labels := buckets[name])
    ]
    scope = f"交付引用按载体分为 {'、'.join(clauses)}；{_SCOPE_DECL}"

    # 三个分句的措辞逐字取自合同「五维段渲染」的冻结模板（含 "检出 Compose" 之间的空格）。
    # 不得从实现回抄到测试，也不得反过来按实现改预期——T26.2-CR-02 就栽在这一步。
    required_parts = [
        f"{len(labels)} 条片段内{phrase}（{_render_refs(labels)}）"
        for labels, phrase in (
            (required_nonempty, "检出 Compose 必填插值，该形态只要求变量已设置且非空"),
            (required_set, "检出 Compose 必填插值，该形态只要求变量已设置"),
            (required_annotation, "检出非空注解"),
        )
        if labels
    ]
    required_clause = (
        f"必填非空：{'；'.join(required_parts)}。"
        if required_parts
        else "必填非空：本次交付引用片段内未检出必填约束。"
    )

    disclosed = [
        f"{count} {unit}"
        for count, unit in (
            (messages, "处插值错误消息"),
            (comments, "行配置注释"),
            (len(buckets["other"]), "条文档或测试类引用"),
        )
        if count
    ]
    disclosure = f"另检出 {'、'.join(disclosed)}，以上未计入上述强制条件维度。" if disclosed else ""

    return (
        f"{_PREFIX}{scope}"
        f"{_dimension('默认值回退', fallback, '默认值插值')}"
        f"{required_clause}"
        f"{_dimension('长度与强度', length, '长度或形态约束')}"
        f"{_dimension('运行时校验', runtime, '运行时校验触发器')}"
        f"{disclosure}{_TAIL}"
    )
