"""U2.2 人工复核材料包：把 5 条代表性真实 run 的逐引用证据摊开成可核对清单。

只读脚本：不调用任何模型，不写数据库；数据全部来自已提交的 dev agentic 报告、
数据库中的 run 轨迹，以及 mini-mall 源仓库的真实文件。

机械校验（脚本可判定，结论客观）：
  ① 引用文件存在，且 sha256 与报告 manifest 一致（自摄取以来未被改动）；
  ② start_line/end_line 在文件内可解析，区间原文与该 chunk 落库正文一致；
  ③ 每条 quote 是否逐字命中其绑定证据（L1 口径）。
语义判定（必须人工，脚本留空）：引用是否恰当、claim 是否被原文真正支持。

用法：uv run python benchmarks/p1_manual_check_packet.py > evalsets/reports/p1-manual-check.md
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from devkb.agent.service import get_run_trace
from devkb.agent.verification import quote_matches_evidence
from devkb.config import get_settings
from devkb.db import create_engine, create_session_factory
from devkb.repositories import ChunkRepo, ProjectRepo

REPORT = Path("evalsets/reports/p1-dev-agentic-20260719T230855+0800.json")
SOURCE_REPO = Path("/home/oslab/projects/mini-mall-order")
PROJECT_SLUG = "mini-mall"
EXCERPT_MAX_LINES = 30

# 选样理由随样本落盘：覆盖 U2.2 五条路径，并优先选 proxy 未命中的题，
# 让人工判定正面回答"引用质量"这个被 T20.4 裁决移交过来的问题。
SAMPLES: list[tuple[str, str, str]] = [
    ("q09", "① 首轮充分 → full", "单轮检索直接生成，proxy 命中；验证正常路径的引用质量"),
    (
        "q10",
        "② refine 后 full",
        "唯一一个补检后转充分的 run（检索 2 轮），验证补检证据是否真被用上",
    ),
    ("q07", "③ partial", "proxy 未命中的 partial：人工判定是引用错，还是标注锚点外的等价证据"),
    ("u01", "④ refusal（正确拒答）", "不可答题，expected=refusal；验证拒答理由是否成立、有无编造"),
    (
        "q12",
        "⑤ L0/L1 失败 → 重生成一次后通过",
        "generate×2 且 proxy 未命中：同时覆盖重生成与引用质量",
    ),
]
SUPPLEMENTARY: list[tuple[str, str, str]] = [
    (
        "q02",
        "⑥ 二次验证仍失败 → 确定性降级",
        "可答题被误拒（§5.2 允许 ≤1/17）+ 两次 L0/L1 失败降级，已知弱点",
    ),
    ("u04", "⑦ expected=partial 实得 refusal", "T20.4 已记录的拒答偏差，供裁定是否可接受"),
]

# 机器预检结论：脚本作者（Claude）对上述材料的判断，**不是人工复核结论**，
# 供复核人对照或推翻；勾选 U2.2 必须以人工裁定栏为准。
PRE_ASSESSMENT = """\
1. **14/14 引用的路径、行号与内容对应关系机械校验全过**，未发现指向不存在文件、
   越界行号或与落库正文不符的引用；18/18 条 quote 逐字命中其绑定证据。就"有没有
   编造引用"这个问题，本轮样本上未见证据。
2. **proxy 未命中不等于引用错误**。q07（proxy=False）的 E2 直接命中
   `docs/messaging/payment-success-event.md > Consumer Rules`，正文逐字包含
   "Consumers must be idempotent because RabbitMQ delivery is at-least-once."，
   与问题完全对应——未命中来自人工标注锚点选取，而非引用质量。这与 T20.4
   "6/9 未命中属检索天花板/等价证据"的归因一致，建议复核人重点裁定这一条。
3. **两条拒答路径行为符合设计**：u01 先 refine 再拒答、not_found 具体到
   "生产环境 MySQL 连接池的最大连接数配置"；q02 两次 L0/L1 失败后移除 2 个未通过
   验证的 claim 并降级为 refusal——宁可拒答也不输出未经验证的引用，误拒是严格
   验证的代价而非缺陷。
4. **发现一个待裁定的质量疑点（不影响任何硬 Gate）**：8 个 partial 中有 **2 个**
   （q05、q07）是被无意义的 not_found 条目降级的，本可判 full——
   `finalize` 要求 `not draft.not_found` 才给 full（保守设计，本身正确），
   而 generate 在"没有未覆盖方面"时并没有返回空数组：q07 返回 `["未覆盖方面"]`，
   **与 GENERATE_SYSTEM 的 Schema 示例 `"not_found":["未覆盖方面"]` 逐字相同——
   模型把格式示例当成内容抄了下来**；q05 返回语义相反的"无其他未覆盖的方面。"。
   根因同一个：Schema 示例给了唯一一个"经常合法为空"的字段一个可抄的占位值，
   且 Prompt 从未说明"没有未覆盖方面时返回空数组"。
   影响面：不破坏 §5.2/§7 任何硬项（partial 是合法终态，L0/L1/预算/终态/拒答全不受影响），
   但 mode 分布偏保守（8 full/8 partial → 实际应为 10/6），且用户可见输出会多一行
   无意义的 `not_found 未覆盖方面`。
   修法明确（Schema 示例改 `"not_found":[]` + 补一句空数组指令），但属 Prompt 变更：
   会使 PROMPT_VERSION 递增、冻结失效，须重跑 dev §5.2 Gate 后才能进 holdout。
   建议：记入 backlog，P1 保持现状（见下方"更正"与提交说明中的取舍）。

**更正（2026-07-20）**：本条初稿把 q04 也列为疑点，错误。q04 的 not_found 虽为空，
但它是 1 个 claim 未通过 L1 验证被移除后按 `removed>0 → partial` 降级的，属完全正确的
行为（warnings 与 limitations 均已如实记录移除）。疑点仅 q05、q07 两例。
"""


def _norm(text: str) -> str:
    return "".join(text.split())


def _file_excerpt(path: Path, start: int, end: int) -> tuple[list[str], str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [], f"读取失败：{exc}"
    if start < 1 or end > len(lines) or start > end:
        return [], f"行号区间越界：文件共 {len(lines)} 行，引用 L{start}-L{end}"
    return lines[start - 1 : end], None


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    except OSError:
        return None


def _fmt_excerpt(lines: list[str], start: int) -> list[str]:
    shown = lines[:EXCERPT_MAX_LINES]
    out = [f"{start + i:>6}│ {line}" for i, line in enumerate(shown)]
    if len(lines) > EXCERPT_MAX_LINES:
        out.append(f"      │ …（共 {len(lines)} 行，此处截断 {len(lines) - EXCERPT_MAX_LINES} 行）")
    return out


async def _collect(
    rows: list[dict[str, Any]], manifest: dict[str, str], partial_not_found: list[Any]
) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in rows}
    engine = create_engine(get_settings().database_url)
    collected: list[dict[str, Any]] = []
    try:
        async with create_session_factory(engine)() as session:
            project = await ProjectRepo(session).get_by_slug(PROJECT_SLUG)
            if project is None:
                raise SystemExit(f"项目 {PROJECT_SLUG} 不存在")
            chunk_repo = ChunkRepo(session, project.id)
            for row in rows:
                if row.get("status") == "succeeded" and row["mode"] == "partial":
                    trace = await get_run_trace(session, project.id, UUID(row["run_id"]))
                    partial_not_found.append(
                        (row["id"], trace["run"]["answer"].get("not_found") or [])
                    )
            for question_id, path_label, rationale in [*SAMPLES, *SUPPLEMENTARY]:
                row = by_id[question_id]
                trace = await get_run_trace(session, project.id, UUID(row["run_id"]))
                answer = trace["run"]["answer"]
                citations = answer["citations"]
                contents = await chunk_repo.get_contents([UUID(c["chunk_id"]) for c in citations])
                checks = []
                for citation in citations:
                    source = SOURCE_REPO / citation["rel_path"]
                    excerpt, error = _file_excerpt(
                        source, citation["start_line"], citation["end_line"]
                    )
                    chunk_text = contents.get(UUID(citation["chunk_id"]), "")
                    file_text = "\n".join(excerpt)
                    if error:
                        alignment = f"❌ {error}"
                    elif _norm(chunk_text) == _norm(file_text):
                        alignment = "✅ 逐字一致"
                    elif _norm(chunk_text) in _norm(file_text):
                        alignment = "✅ chunk 正文包含于该行区间"
                    elif _norm(file_text) in _norm(chunk_text):
                        alignment = "⚠️ 行区间是 chunk 正文的子集（chunk 跨更大范围）"
                    else:
                        alignment = "❌ 行区间原文与 chunk 落库正文不一致"
                    checks.append(
                        {
                            "citation": citation,
                            "source": source,
                            "excerpt": excerpt,
                            "alignment": alignment,
                            "hash_ok": _sha256(source) == manifest.get(citation["rel_path"]),
                            "chunk_text": chunk_text,
                        }
                    )
                quote_checks = []
                for index, claim in enumerate(answer.get("claims", [])):
                    bound = [
                        contents.get(UUID(c["chunk_id"]), "")
                        for c in citations
                        if c["evidence_id"] in claim.get("evidence_ids", [])
                    ]
                    for quote in claim.get("quotes", []):
                        quote_checks.append(
                            {
                                "claim_index": index,
                                "quote": quote,
                                "verbatim": any(quote_matches_evidence(quote, t) for t in bound),
                            }
                        )
                collected.append(
                    {
                        "row": row,
                        "path_label": path_label,
                        "rationale": rationale,
                        "trace": trace,
                        "answer": answer,
                        "checks": checks,
                        "quote_checks": quote_checks,
                        "supplementary": question_id in {s[0] for s in SUPPLEMENTARY},
                    }
                )
    finally:
        await engine.dispose()
    return collected


def _render(collected: list[dict[str, Any]], report: dict[str, Any]) -> str:
    corpus = report["corpus"]
    config = report["config"]
    lines = [
        "# P1 人工复核报告（U2.2）",
        "",
        "> 状态：**材料已备齐，等待人工裁定**——本文件由 "
        "`benchmarks/p1_manual_check_packet.py` 生成，",
        "> 机械校验部分（文件是否存在、sha256 是否与摄取时一致、行号区间与 chunk 正文是否对应、"
        "quote 是否逐字命中）由脚本判定；",
        "> **引用是否恰当、claim 是否被原文真正支持属语义判断，必须由人工填写下方裁定栏**。",
        "> 未经人工裁定不得据此勾选 U2.2。",
        "",
        f"> 样本来源：`{REPORT.name}`（真实 DeepSeek + Qwen，未接触 holdout）",
        f"> 语料：{corpus['project']} · {corpus['document_count']} 文档 / "
        f"{corpus['chunk_count']} chunks ｜ manifest sha256 `{corpus['sha256'][:16]}…`",
        f"> 模型：embedding=`{config['embedding_model_id']}` llm=`{config['llm_model']}` "
        f"prompt=`{config['prompt_version']}`",
        f"> 源仓库（逐引用打开的真实文件）：`{SOURCE_REPO}`",
        "",
        "## 选样与路径覆盖",
        "",
        "《P1任务清单》U2.2 要求至少覆盖 full、refine 后 full、partial、refusal、L1 重生成各一种。",
        "",
        "| # | 题 | 路径 | 实得 mode | 检索轮 | generate | 选样理由 |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for item in collected:
        row, trace = item["row"], item["trace"]
        nodes = [step["node"] for step in trace["steps"]]
        lines.append(
            f"| {'补充' if item['supplementary'] else '必需'} | {row['id']} | "
            f"{item['path_label']} | {row['mode']} | {nodes.count('retrieve')} | "
            f"{nodes.count('generate')} | {item['rationale']} |"
        )

    total = sum(len(item["checks"]) for item in collected)
    hash_ok = sum(1 for item in collected for c in item["checks"] if c["hash_ok"])
    aligned = sum(
        1 for item in collected for c in item["checks"] if c["alignment"].startswith("✅")
    )
    quotes = [q for item in collected for q in item["quote_checks"]]
    lines += [
        "",
        "## 机械校验汇总（脚本判定）",
        "",
        f"- 引用总数：**{total}**",
        f"- 源文件 sha256 与摄取快照一致：**{hash_ok}/{total}**"
        "（不一致说明文件在摄取后被改动，行号可能已漂移）",
        f"- 行号区间原文与 chunk 落库正文对应：**{aligned}/{total}**",
        f"- claim quotes 逐字命中绑定证据（L1 口径）：**{sum(q['verbatim'] for q in quotes)}/"
        f"{len(quotes)}**",
        "",
        "## 人工裁定汇总（**待填写**）",
        "",
        "| 题 | 引用数 | 路径行号真实 | 内容支持论断 | 是否发现编造 | 结论 |",
        "|---|---:|---|---|---|---|",
    ]
    for item in collected:
        lines.append(
            f"| {item['row']['id']} | {len(item['checks'])} | ☐ 　/ 　 | ☐ 　/ 　 | "
            "☐ 无 ☐ 有 | ☐ 通过 ☐ 不通过 |"
        )
    lines += [
        "",
        "> 人工引用正确率（U2.2 兜底指标，替代已降为记录义务的 citation proxy）："
        "____ / ____ = ____%",
        "> 总体结论：☐ U2.2 通过　☐ 不通过（原因：____）　复核人：____　日期：____",
        "",
        "## 机器预检结论（**非人工复核结论**，供对照或推翻）",
        "",
        PRE_ASSESSMENT,
        "## 附：全部 partial 题的 not_found（第 4 条疑点的完整证据）",
        "",
        "| 题 | not_found |",
        "|---|---|",
    ]
    for question_id, not_found in report.get("_partial_not_found", []):
        rendered = "；".join(not_found) if not_found else "**（空）**"
        lines.append(f"| {question_id} | {rendered} |")
    lines += [
        "",
        "---",
        "",
        "## 逐题材料",
        "",
    ]

    for item in collected:
        row, answer, trace = item["row"], item["answer"], item["trace"]
        nodes = [step["node"] for step in trace["steps"]]
        usage = trace["run"]["usage"] or {}
        l0l1_verdict = "通过" if not row.get("l0_errors") and not row.get("l1_errors") else "未通过"
        lines += [
            f"### {row['id']}　{row['question']}",
            "",
            f"- 路径：{item['path_label']}　｜　实得 mode：**{row['mode']}**"
            + (f"　｜　期望 mode：{row['expected_mode']}" if row.get("expected_mode") else ""),
            f"- run_id：`{row['run_id']}`（回放：`devkb runs replay {row['run_id']} "
            f"--project {PROJECT_SLUG}`）",
            f"- 节点序列：`{'>'.join(nodes)}`",
            f"- 检索 {nodes.count('retrieve')} 轮 ｜ LLM 请求 {usage.get('llm_calls', '-')}"
            f"（重试 {usage.get('llm_retries', 0)}）｜ tokens "
            f"{trace['run']['tokens_in']}+{trace['run']['tokens_out']} ｜ "
            f"latency {trace['run']['latency_ms']}ms",
            f"- L0/L1 终检：{l0l1_verdict}"
            f"　｜　citation-to-anchor proxy 命中：{row.get('citation_hit')}",
        ]
        if answer["warnings"]:
            lines.append(f"- warnings：{answer['warnings']}")
        lines += [
            "",
            "**回答正文**",
            "",
            "```text",
            answer["answer_text"].strip() or "(空)",
            "```",
            "",
        ]
        if answer.get("not_found"):
            lines += ["**not_found**：" + "；".join(answer["not_found"]), ""]
        if answer.get("limitations"):
            lines += ["**limitations**：" + "；".join(answer["limitations"]), ""]

        if answer.get("claims"):
            lines += ["**claims 与 quotes**", ""]
            for index, claim in enumerate(answer["claims"]):
                lines.append(f"{index + 1}. {claim['text']}　→ 证据 {claim['evidence_ids']}")
                for quote in claim.get("quotes", []):
                    verdict = next(
                        (
                            q["verbatim"]
                            for q in item["quote_checks"]
                            if q["claim_index"] == index and q["quote"] == quote
                        ),
                        None,
                    )
                    mark = "✅逐字命中" if verdict else "❌未逐字命中"
                    lines.append(f'   - {mark}　"{quote}"')
            lines.append("")

        lines += ["**逐引用核对**", ""]
        if not item["checks"]:
            lines += ["（本 run 无引用——拒答路径下属预期）", ""]
        for check in item["checks"]:
            citation = check["citation"]
            lines += [
                f"#### {citation['evidence_id']}　"
                f"`{citation['rel_path']}:L{citation['start_line']}-L{citation['end_line']}`",
                "",
                f"- 标题路径：{citation['title_path']}　｜　score {citation['score']:.3f}",
                f"- 源文件 sha256 与摄取快照："
                f"{'✅ 一致' if check['hash_ok'] else '❌ 不一致（文件已改动，行号可能漂移）'}",
                f"- 行号区间 vs chunk 落库正文：{check['alignment']}",
                "",
                "真实文件原文（供人工核对是否支持上述论断）：",
                "",
                "```text",
                *(_fmt_excerpt(check["excerpt"], citation["start_line"]) or ["(无法读取)"]),
                "```",
                "",
                "> 人工裁定：☐ 路径行号真实　☐ 内容支持所标注论断　☐ 存在问题（说明：____）",
                "",
            ]
        lines += ["---", ""]
    return "\n".join(lines)


def main() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    manifest = {d["rel_path"]: d["content_hash"] for d in report["corpus"]["manifest"]}
    partial_not_found: list[Any] = []
    collected = asyncio.run(_collect(report["agentic"]["questions"], manifest, partial_not_found))
    report["_partial_not_found"] = partial_not_found
    print(_render(collected, report))


if __name__ == "__main__":
    main()
