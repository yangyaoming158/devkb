"""T26.3 配置类五维校验的纯函数矩阵（规格 §8 第 3 句，RT-19 / 案例八）。

冻结测试 U1–U5、U7–U10b、U13；图级行为在 tests/unit/test_agent_graph.py。

**冻结模板的预期字符串一律从 `docs/tasks/T26.3-config-layers.md` 复制粘贴**，
不得从实现回抄——T26.2-CR-02 正是"实现写了中文弯引号、U2 照着实现抄"，
于是"逐字等于冻结模板"变成自我循环。
"""

from __future__ import annotations

import random
import re

import pytest

from devkb.agent.config_layers import (
    CONFIG_TERMS,
    FragmentFacts,
    carrier_of,
    config_layer_note,
    is_config_question,
    mask_advisory,
    scan_fragment,
)

# T26.1/T26.2 的闭合词表在图级测试里维护；此处只引入本任务需要的三张表。
T261_UNIVERSAL = (
    "任何",
    "所有",
    "全部",
    "一切",
    "每个",
    "各个",
    "均",
    "都",
    "一律",
    "无一",
    "毫无",
    "从不",
)
# 表 F · 本任务专属禁用措辞（合同「表 F」逐字）
T263_FORBIDDEN = (
    "强度保证",
    "secret 强度",
    "生产安全",
    "已校验长度",
    "无弱默认值",
    "配置无风险",
    "双重保障",
    "强制非空即安全",
    "已通过安全校验",
    "最终生效",
    "实际生效",
    "该文件所有配置",
    "配置矛盾",
)
_MARK = re.compile(r"\[E(\d+)\]")

_COMPOSE = "docker-compose.yml"
_APP_YML = "backend/src/main/resources/application.yml"
_JWT_PROPS = "backend/src/main/java/com/ragdocs/config/JwtProperties.java"

# 案例八三条真实形态，取自《P1后真实仓库可用性测试问题记录》§11.1 / §11.3
_CASE8_COMPOSE = (
    "    environment:\n"
    "      RAG_JWT_SECRET: ${RAG_JWT_SECRET:?RAG_JWT_SECRET is required, "
    "generate 32+ random chars}\n"
)
_CASE8_APP_YML = (
    "rag:\n  jwt:\n    # Local JVM default only\n"
    "    secret: ${RAG_JWT_SECRET:devdocs-rag-change-me-please-32-bytes-min}\n"
)
_CASE8_PROPS = (
    '@ConfigurationProperties(prefix = "rag.jwt")\n'
    "public class JwtProperties {\n    private String secret;\n}\n"
)
_CASE8_ROWS = (
    ("E1", _COMPOSE, _CASE8_COMPOSE),
    ("E2", _APP_YML, _CASE8_APP_YML),
    ("E3", _JWT_PROPS, _CASE8_PROPS),
)

# ---- U2 冻结模板（逐字复制自 packet「五维段渲染」，仅去掉为可读而加的折行）----
FROZEN_NOTE = (
    "（本次配置分层校验：交付引用按载体分为 Compose 文件 1 条（docker-compose.yml(E1)）、"
    f"应用配置文件 1 条（{_APP_YML}(E2)）、"
    f"生产源码 1 条（{_JWT_PROPS}(E3)）；"
    "不同载体的插值由不同工具解析，本阶段不判定某条配置在某个启动路径下最终是否生效。"
    f"默认值回退：1 条片段内检出默认值插值（{_APP_YML}(E2)）。"
    "必填非空：1 条片段内检出 Compose 必填插值，该形态只要求变量已设置且非空"
    "（docker-compose.yml(E1)）。"
    "长度与强度：本次交付引用片段内未检出长度或形态约束。"
    "运行时校验：本次交付引用片段内未检出运行时校验触发器。"
    "另检出 1 处插值错误消息、1 行配置注释，以上未计入上述强制条件维度。"
    "本校验只覆盖上述引用片段，未覆盖同一文件中未被引用的其余配置行。）"
)


# ---- U1 / U1b 表 C 触发词 ----


@pytest.mark.parametrize(
    ("term", "question"),
    [
        ("配置", "这个项目的配置放在哪里？"),
        ("compose", "Docker Compose 启动时怎么注入变量？"),
        ("application.yml", "application.yml 里那一行是什么意思？"),
        ("环境变量", "服务读哪些环境变量？"),
        ("默认值", "没设置的时候默认值是什么？"),
        ("secret", "JWT secret 从哪里读？"),
    ],
)
def test_t263_u1_each_trigger_term_is_detected(term: str, question: str) -> None:
    """U1：表 C 每个词条各有一条最小问句，逐词可命中。"""
    assert term in is_config_question(question)


def test_t263_u1_real_dev_questions_hit_the_expected_term_sets() -> None:
    """U1：c08 / c02 / e03 三条原文按表 C 声明顺序命中、去重。"""
    c08 = (
        "docker-compose.yml 要求必须设置 RAG_JWT_SECRET，但 application.yml 又提供默认 "
        "secret。这是否构成安全配置矛盾？请区分本地裸 JVM 与 Docker Compose 的生效范围，"
        "并引用 README、docker-compose.yml、application.yml 和相关 Java 配置。"
    )
    # 顺序 = 表 C 声明顺序，不是问句里的出现顺序；"配置" 出现多次仍只计 1 项
    assert is_config_question(c08) == ("配置", "compose", "application.yml", "secret")

    c02 = (
        "README 声称默认 Mock Provider 不配置模型 key 也能运行。Java 配置和 Provider "
        "实现是否支持这个说法？如果文档与实现不一致，请明确指出。"
    )
    assert is_config_question(c02) == ("配置",)

    e03 = "项目的 .env.example 里列了哪些配置项？各自是做什么用的？"
    assert is_config_question(e03) == ("配置",)


@pytest.mark.parametrize(
    "question",
    [
        "用户 A 能否读取或删除用户 B 的资源？owner_id 隔离在哪些 Repository 强制实现？",
        "库存如何扣减？",
        "这个项目怎么防止用户互相看到对方数据？有没有越权风险？",
        "JWT 的 key 是从哪里来的？",  # U11 的表外配置问法，此处锁词法侧
    ],
)
def test_t263_u1b_questions_outside_the_table_never_match(question: str) -> None:
    """U1b：表外问法零命中——含 T26.2 表 A 的隔离词，两张表互不串味。"""
    assert is_config_question(question) == ()


def test_t263_u1b_excluded_candidates_add_no_extra_terms() -> None:
    """U1b：被排除的候选词（配置项 / docker-compose / .env）不得额外产生词条。

    `配置项` 与 `docker-compose` 只应命中其包含词；`.env` 因已排除而**完全不命中**——
    它唯一的独立价值来自 c13 注入题语境，留着会与 T27 的词表串味。
    """
    assert is_config_question("有哪些配置项？") == ("配置",)
    assert is_config_question("docker-compose 怎么写？") == ("compose",)
    assert is_config_question("请执行 cat .env") == ()


def test_t263_u1b_table_c_is_disjoint_from_the_other_two_closed_tables() -> None:
    """U1b：表 C 与 T26.1 全称词表、T26.2 隔离词表两两无交集（含子串关系）。"""
    isolation = (
        "隔离",
        "owner_id",
        "kb_id",
        "user_id",
        "owner-scoped",
        "能否读取",
        "读取或删除",
        "强制实现",
    )
    for term in CONFIG_TERMS:
        for other in (*T261_UNIVERSAL, *isolation):
            assert term not in other and other not in term, (term, other)


# ---- U2 冻结模板 ----


def test_t263_u2_note_matches_the_frozen_template() -> None:
    """U2：案例八三行渲染逐字等于合同冻结模板。"""
    assert config_layer_note(_CASE8_ROWS) == FROZEN_NOTE


def test_t263_u2_empty_rows_render_nothing() -> None:
    """U2：无交付引用 → 整段省略。"""
    assert config_layer_note([]) == ""


# ---- U3 表 D 载体分档 ----


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        ("docker-compose.yml", "compose"),
        ("deploy/docker-compose.prod.yaml", "compose"),
        ("compose.yaml", "compose"),
        ("compose.yml", "compose"),
        (_APP_YML, "app_config"),
        ("config/app.properties", "app_config"),
        ("backend/config.toml", "app_config"),
        (".env", "env_file"),
        (".env.example", "env_file"),
        (_JWT_PROPS, "source"),
        ("src/main/java/com/example/JwtService.java", "source"),
        ("README.md", "other"),
        ("docs/design/architecture.md", "other"),
        ("src/test/java/com/example/JwtTest.java", "other"),
        ("db/migration/V1__init.sql", "other"),
        ("frontend/src/router.ts", "other"),
    ],
)
def test_t263_u3_carrier_matrix_matches_the_frozen_table(rel_path: str, expected: str) -> None:
    """U3：表 D 载体分档矩阵逐条锁定；文档/测试/迁移/前端一律 `other`。"""
    assert carrier_of(rel_path) == expected


def test_t263_u3_other_carrier_never_contributes_to_any_dimension() -> None:
    """U3：`other` 档即便内容里有注解，也恒返回全空事实（结构性不参与）。"""
    loaded = "@Validated @NotBlank @Size(min = 32) ${VAR:?must be set}"
    assert scan_fragment(loaded, "other") == FragmentFacts()


# ---- U4 插值方言矩阵 ----


@pytest.mark.parametrize(
    ("content", "carrier", "field"),
    [
        # Compose 方言
        ("A: ${V:?must be set}", "compose", "required_compose_nonempty"),
        ("A: ${V?must be set}", "compose", "required_compose_set"),
        ("A: ${V:-fallback}", "compose", "fallback"),
        ("A: ${V-fallback}", "compose", "fallback"),
        # Spring 方言
        ("a: ${V:fallback}", "app_config", "fallback"),
        ("a: ${V:}", "app_config", "fallback"),
        # 点分 property 路径——Spring 的真实写法；首版正则只认环境变量名，实测漏判
        ('@Value("${rag.jwt.secret:changeme}")', "source", "fallback"),
        ("rag:\n  jwt:\n    secret: ${rag.jwt.secret:d}", "app_config", "fallback"),
    ],
)
def test_t263_u4_interpolation_dialect_hits(content: str, carrier: str, field: str) -> None:
    """U4：每种方言各自认得自己的操作符。"""
    facts = scan_fragment(content, carrier)  # type: ignore[arg-type]
    assert getattr(facts, field) is True


@pytest.mark.parametrize(
    ("content", "carrier"),
    [
        # **方言分立的核心**：同一串 `${V:?msg}` 在 Spring 档里冒号后整段都是默认值，
        # 认作必填即复现判据第 2 句要防的病 → fail-closed，不计入任何维度
        ("a: ${V:?must be set}", "app_config"),
        ('@Value("${rag.jwt.secret:?required}")', "source"),
        # Compose 没有 `${V:x}` 这种默认值语法
        ("A: ${V:plain}", "compose"),
        # 无操作符的纯插值
        ("A: ${V}", "compose"),
        ("a: ${V}", "app_config"),
        # dotenv 不判定插值语义
        ("V=${OTHER:?must be set}", "env_file"),
        # kebab-case Spring key：`-` 不进变量名（否则会吞掉 Compose 的 `${V-d}` 操作符），
        # 于是 rest 以 `-` 开头 → Spring 方言判 unknown。安全方向的假阴性，明示锁定
        ("a: ${rag.jwt.secret-key:d}", "app_config"),
        # 畸形插值（无闭合 `}`）不匹配也不报错
        ("A: ${V:?unterminated", "compose"),
    ],
)
def test_t263_u4_interpolation_forms_that_must_not_count(content: str, carrier: str) -> None:
    """U4（fail-closed）：不可证的插值形态一律不计入任何维度。"""
    facts = scan_fragment(content, carrier)  # type: ignore[arg-type]
    assert not facts.fallback
    assert not facts.required_compose_nonempty
    assert not facts.required_compose_set


# ---- U5 行身份与截断 ----


def test_t263_u5_more_than_three_rows_use_the_frozen_truncation() -> None:
    """U5：行标签超 3 条沿用 `_render_refs` 的"前 3 条 + 等 N 条"。"""
    rows = [(f"E{i}", f"conf/a{i}.yml", f"k: ${{V{i}:d{i}}}") for i in range(1, 6)]
    note = config_layer_note(rows)
    assert "conf/a1.yml(E1)、conf/a2.yml(E2)、conf/a3.yml(E3) 等 5 条" in note
    assert "conf/a4.yml(E4)" not in note


def test_t263_u5_same_path_different_evidence_ids_stay_two_rows() -> None:
    """U5：同一 rel_path 的两个 chunk 计为**两行**，不被路径去重合并。"""
    rows = [
        ("E1", _APP_YML, "a: ${V:one}"),
        ("E2", _APP_YML, "b: plain"),
    ]
    note = config_layer_note(rows)
    assert f"应用配置文件 2 条（{_APP_YML}(E1)、{_APP_YML}(E2)）" in note
    # 只有 E1 有默认值插值，E2 不得被同路径带上
    assert f"默认值回退：1 条片段内检出默认值插值（{_APP_YML}(E1)）。" in note


def test_t263_u5_zero_buckets_are_not_rendered() -> None:
    """U5：载体桶为零不渲染；`other` 桶不列行标签（列出会暗示它参与了维度）。"""
    note = config_layer_note([("E1", "README.md", "见文档建议")])
    assert "交付引用按载体分为 其他证据 1 条；" in note
    assert "README.md(E1)" not in note
    for absent in ("Compose 文件", "应用配置文件", "环境变量文件", "生产源码"):
        assert absent not in note


# ---- U7 / U7b 判据可证边界 #3：三维不得互相顶替 ----


def test_t263_u7_required_hit_never_implies_length_or_runtime() -> None:
    """U7（**本任务核心断言**，判据第 2/3 句）：案例八的 Compose 行。

    `${RAG_JWT_SECRET:?...generate 32+ random chars}` 使"必填非空"命中，而错误消息里的
    "32+" 被 `mask_advisory` 结构性屏蔽，故"长度与强度"仍报未检出。
    """
    facts = scan_fragment(_CASE8_COMPOSE, "compose")
    assert facts.required_compose_nonempty is True
    assert facts.length_strength is False, "错误消息里的 32+ 不得冒充长度校验"
    assert facts.runtime_validation is False
    # 屏蔽是结构性的：消息体在关键词扫描前就不存在了
    assert "32" not in mask_advisory(_CASE8_COMPOSE)
    assert "generate" not in mask_advisory(_CASE8_COMPOSE)


@pytest.mark.parametrize(
    "annotation",
    ["@NotBlank", "@NotEmpty", "@NotNull"],
)
def test_t263_u7b_nonempty_annotation_never_implies_a_runtime_trigger(annotation: str) -> None:
    """U7b：非空注解命中不代表运行时会触发校验——缺 `@Validated` 时该维仍未检出。

    `@NotNull` 一例是 PG-T263-02 的护栏（backlog T263-P3-01）：它只禁 null、允许空串，
    此处锁定它**仅**落 `required·non-empty`，不外溢到另外两维。
    """
    facts = scan_fragment(
        f"public class P {{\n    {annotation}\n    private String s;\n}}", "source"
    )
    assert facts.required_annotation is True
    assert facts.runtime_validation is False, "注解是声明，触发器是另一回事"
    assert facts.length_strength is False


def test_t263_u7b_validated_is_what_makes_runtime_validation_visible() -> None:
    """U7b：加上 `@Validated` 后运行时维才命中——两维确实分立、不是同一个信号。"""
    facts = scan_fragment("@Validated\n@ConfigurationProperties\nclass P {}", "source")
    assert facts.runtime_validation is True
    assert facts.required_annotation is False


# ---- U8 / U9 / U10 措辞边界 ----


def test_t263_u8_positive_wording_is_scoped_to_the_delivered_fragment() -> None:
    """U8（边界 #1）：命中分句限定"片段内检出"，不得声称最终生效或覆盖整个文件。"""
    note = config_layer_note([("E1", _COMPOSE, "A: ${V:?err}")])
    assert "条片段内检出" in note
    for word in ("最终生效", "实际生效", "该文件所有配置", "该文件全部配置"):
        assert word not in note


def test_t263_u9_negative_wording_never_claims_absence() -> None:
    """U9（边界 #2）：未命中分句只说"未检出"，不得说"没有/缺少/不存在/不安全"。"""
    note = config_layer_note([("E1", _JWT_PROPS, "class P {}")])
    assert "本次交付引用片段内未检出长度或形态约束。" in note
    assert "本次交付引用片段内未检出运行时校验触发器。" in note
    assert "必填非空：本次交付引用片段内未检出必填约束。" in note
    for word in ("没有", "缺少", "未做", "不存在", "不安全", "无校验"):
        assert word not in note


def test_t263_u10_scope_dimension_never_judges_effectiveness() -> None:
    """U10（边界 #4）：scope 维恒声明未判定生效，且不出现任何肯定/否定生效的措辞。"""
    for rows in (_CASE8_ROWS, [("E1", "README.md", "x")], [("E1", ".env.example", "V=1")]):
        note = config_layer_note(rows)
        assert "本阶段不判定某条配置在某个启动路径下最终是否生效。" in note
        for word in ("会生效", "不会生效", "已生效", "未生效", "优先级更高"):
            assert word not in note


# ---- U10b 屏蔽规则矩阵 ----


@pytest.mark.parametrize(
    ("text", "survives"),
    [
        ("# @Size(min = 32)\nkeep", False),  # 行首 #
        ("secret: v  # @Size(min = 32)", False),  # 空白之后的 #
        ("url: https://a#b/@Size(min = 32)", True),  # 串内 # 不是注释，不删
        ("// @Size(min = 32)\nkeep", False),  # 行注释
        ('url = "http://x"; @Size(min = 32)', True),  # http:// 不被当注释
        ("/* @Size(min = 32) */\nkeep", False),  # 块注释
        ("@Size(min = 32)", True),  # 裸注解必须留下
    ],
)
def test_t263_u10b_masking_removes_only_advisory_text(text: str, survives: bool) -> None:
    """U10b：注释与插值被删、真实代码留下。`survives` 指 `@Size` 是否还在。"""
    assert ("@Size" in mask_advisory(text)) is survives


def test_t263_u10b_interpolation_payload_is_always_removed() -> None:
    """U10b：整个 `${…}` 片段（连同消息体）恒被删除。"""
    assert "@Validated" not in mask_advisory("a: ${V:?please add @Validated and @Size}")


def test_t263_u10b_block_comment_removal_preserves_line_structure() -> None:
    """U10b：块注释按行数替换为换行——塌行会让上一行的比较运算符与下一行的 `.length()`
    假性同行，凭空造出一条"检出长度约束"。"""
    text = "if (x < 1) {}\n/* note\nnote */\nlog.info(s.length());"
    assert scan_fragment(text, "source").length_strength is False


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("if (secret.length() < 32) { throw x; }", True),
        ("if (secret.length() >= 32) {}", True),
        ('log.info("len={}", secret.length());', False),  # 无比较运算符
        ("if (32 < secret.length()) {}", False),  # 比较在前：假阴性是安全方向
    ],
)
def test_t263_u10b_length_call_requires_a_following_comparison(line: str, expected: bool) -> None:
    """U10b：`.length()` 须在同行**之后**出现比较运算符才算长度约束。

    位置序刻意保留假阴性：少报一维只让文案更保守，而假阳性会凭空造出"检出长度约束"，
    正是案例八要防的过度声称。
    """
    assert scan_fragment(line, "source").length_strength is expected


def test_t263_u10b_comment_lines_are_counted_for_disclosure() -> None:
    """U10b：注释行进入披露计数，且逐字声明未计入强制条件维度。"""
    note = config_layer_note([("E1", _APP_YML, "# one\n# two\nsecret: v\n")])
    assert "另检出 2 行配置注释，以上未计入上述强制条件维度。" in note


def test_t263_u10b_disclosure_is_omitted_when_nothing_was_excluded() -> None:
    """U10b：三个计数全为零时整句省略（没有可声明未计入的东西）。"""
    note = config_layer_note([("E1", _APP_YML, "secret: v\n")])
    assert "另检出" not in note


# ---- U13 组合/变形不变量 ----


def _random_rows(rng: random.Random) -> list[tuple[str, str, str]]:
    paths = [_COMPOSE, _APP_YML, ".env.example", _JWT_PROPS, "README.md", "src/test/a/BTest.java"]
    bodies = [
        "A: ${V:?must be set}",
        "A: ${V:-d}",
        "a: ${V:d}",
        "@NotBlank private String s;",
        "@Validated class P {}",
        "@Size(min = 32) private String s;",
        "if (s.length() < 32) {}",
        "# just a comment\nplain: 1",
        "",
    ]
    count = rng.randint(0, 6)
    rows = []
    for index in range(count):
        path = rng.choice(paths)
        # 刻意允许同路径重复，以覆盖"两个 chunk 不被路径去重"
        rows.append((f"E{index + 1}", path, rng.choice(bodies)))
    return rows


def test_t263_u13_note_is_a_pure_function_of_the_rows() -> None:
    """U13：300 组随机行的四条不变量。"""
    rng = random.Random(20260729)
    seen_empty = seen_nonempty = seen_other = 0
    for _ in range(300):
        rows = _random_rows(rng)
        note = config_layer_note(rows)

        # ① 五维段存在 ⟺ 有交付行
        assert bool(note) is bool(rows)
        if not rows:
            seen_empty += 1
            continue
        seen_nonempty += 1

        # ② 各载体桶计数之和 == 行数（不是路径数）
        counts = [int(m) for m in re.findall(r"(?:文件|配置文件|源码|证据) (\d+) 条", note)]
        assert sum(counts) == len(rows), (note, rows)

        # ③ 三张禁用/全称词表零命中
        for word in (*T261_UNIVERSAL, *T263_FORBIDDEN):
            assert word not in note, (word, note)

        # ④ 恒不含 [E#]（否则会被 Answer L0 扫描当成引用标记）
        assert not _MARK.search(note)

        # ⑤ 五个维度小节恒各出现 1 次，顺序固定、不因输入重排
        titles = [
            "交付引用按载体分为",
            "默认值回退：",
            "必填非空：",
            "长度与强度：",
            "运行时校验：",
        ]
        positions = [note.index(title) for title in titles]
        assert all(note.count(title) == 1 for title in titles), note
        assert positions == sorted(positions), note

        if "其他证据" in note:
            seen_other += 1

    # 覆盖哨兵：三种关键状态都真被触达过（T26.1-CR-01 的教训——断言与实现同构时
    # 一整类状态可能从未进入过循环体）
    assert seen_empty and seen_nonempty and seen_other, (seen_empty, seen_nonempty, seen_other)


def test_t263_u13_note_is_deterministic_across_runs() -> None:
    """U13：同一输入两次渲染逐字相同（无集合/字典序泄漏）。"""
    assert config_layer_note(_CASE8_ROWS) == config_layer_note(_CASE8_ROWS)
