"""T20.1 eval harness：报告产出/schema 字段/不覆盖/holdout 确认/模式枚举。

fixture 语料 + Fake 组件 + 真实测试 PG（§6 eval-ci 路径）；迷你题集走
enforce_counts=False，冻结 31 问题量校验由 CLI 真实评测路径承担。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from devkb.config import Settings
from devkb.embedding import FakeEmbedder
from devkb.errors import InvalidInputError
from devkb.evaluation import (
    EVAL_MODES,
    HOLDOUT_LEDGER_NAME,
    expand_modes,
    read_holdout_ledger,
    run_eval,
    write_report,
)
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.llm import LLMResult
from devkb.repositories import ProjectRepo

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)


class _RoutedLLM:
    """按 system prompt 路由，多题顺序评测互不串台。"""

    # T31.2R-a：`_structured_call` 一律传 `json_mode=True`，替身必须接住它，
    # 否则 TypeError 会被当成 request_failed，测试绿着但 agent 已全降级。
    async def complete(self, *, system: str, user: str, json_mode: bool = False) -> LLMResult:
        del json_mode
        if "查询规划器" in system:
            text = PLAN
        elif "充分性评估器" in system:
            text = EVAL_OK
        else:
            text = GEN
        return LLMResult(
            text=text,
            model="deepseek-v4-flash",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )


def _write_mini_evalsets(root: Path) -> Path:
    (root / "v0").mkdir(parents=True)
    (root / "v1").mkdir()
    answerable = [
        {
            "id": "q1",
            "question": "库存怎么保证并发安全？",
            "answerable": True,
            "relevant": [{"rel_path": "zh.md", "anchor": "并发控制"}],
        },
        {
            "id": "q2",
            "question": "订单取消后库存如何回补？（InventoryService）",
            "answerable": True,
            "relevant": [{"rel_path": "zh.md", "anchor": "回补策略"}],
        },
    ]
    unanswerable = [
        {
            "id": "u1",
            "question": "生产环境数据库连接池大小是多少？",
            "answerable": False,
            "relevant": [],
        }
    ]
    holdout_answerable = [
        {
            "id": "hq1",
            "question": "库存怎么保证并发安全？",
            "answerable": True,
            "relevant": [{"rel_path": "zh.md", "anchor": "并发控制"}],
        }
    ]
    holdout_unanswerable = [
        {
            "id": "hu1",
            "question": "生产环境 Redis 版本是多少？",
            "answerable": False,
            "relevant": [],
        }
    ]
    expectations = [
        {"id": "u1", "split": "dev", "expected_mode": "refusal"},
        {"id": "hu1", "split": "holdout", "expected_mode": "refusal"},
    ]
    for name, rows in (
        ("v0/retrieval_dev.jsonl", answerable),
        ("v0/unanswerable_dev.jsonl", unanswerable),
        ("v0/retrieval_holdout.jsonl", holdout_answerable),
        ("v0/unanswerable_holdout.jsonl", holdout_unanswerable),
        ("v1/answer-expectations.jsonl", expectations),
    ):
        (root / name).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
    return root


async def _seed_project(session: AsyncSession, slug: str) -> None:
    project = await ProjectRepo(session).create(slug=slug, name=slug)
    await session.commit()
    report = await ingest_directory(
        session, project.id, CORPUS_MD, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    await session.commit()


async def test_run_eval_all_modes_writes_schema_complete_reports(
    session: AsyncSession, migrated_db_url: str, tmp_path: Path
) -> None:
    slug = f"t20-{uuid.uuid4().hex[:8]}"
    await _seed_project(session, slug)
    evalsets_dir = _write_mini_evalsets(tmp_path / "evalsets")
    output_dir = tmp_path / "reports"
    settings = Settings(llm_api_key=SecretStr("eval-test"), database_url=migrated_db_url)

    written = await run_eval(
        settings,
        split="dev",
        modes=list(EVAL_MODES),
        project_slug=slug,
        output_dir=output_dir,
        evalsets_dir=evalsets_dir,
        embedder=FakeEmbedder(),
        llm=_RoutedLLM(),
        enforce_counts=False,
    )

    assert len(written) == 2  # dev：retrieval 与 agentic 各一份
    stems = [json_path.name for json_path, _ in written]
    assert any(name.startswith("p1-dev-retrieval-") for name in stems)
    assert any(name.startswith("p1-dev-agentic-") for name in stems)
    for json_path, markdown_path in written:
        assert json_path.exists() and markdown_path.exists()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        # 判据字段：commit / 语料 manifest+hash / 模型 / Prompt / 配置
        assert report["schema_version"] == "p1-eval-v1.2"
        assert report["run"]["devkb_commit"] and report["run"]["holdout_accessed"] is False
        assert report["corpus"]["sha256"]
        assert len(report["corpus"]["manifest"]) == report["corpus"]["document_count"] == 5
        assert report["config"]["embedding_model_id"] and report["config"]["llm_model"]
        assert report["config"]["prompt_version"]
        assert markdown_path.read_text(encoding="utf-8").startswith("# Evaluation v1 报告")

    retrieval_report = json.loads(
        next(p for p, _ in written if p.name.startswith("p1-dev-retrieval-")).read_text(
            encoding="utf-8"
        )
    )
    metrics = retrieval_report["retrieval"]["metrics"]
    assert set(metrics) == {"vector-exact", "vector-hnsw", "lexical", "hybrid-rrf"}
    for mode_metrics in metrics.values():
        assert {"recall_at_5", "recall_at_10", "mrr_at_10", "latency_ms_p50"} <= set(mode_metrics)
    assert isinstance(retrieval_report["gates"]["retrieval"]["hnsw_overlap_gate"], bool)

    agentic_report = json.loads(
        next(p for p, _ in written if p.name.startswith("p1-dev-agentic-")).read_text(
            encoding="utf-8"
        )
    )
    rows = agentic_report["agentic"]["questions"]
    assert [row["id"] for row in rows] == ["q1", "q2", "u1"]
    assert all(row["status"] == "succeeded" and row["run_terminal"] for row in rows)
    gates = agentic_report["gates"]["agentic"]
    assert {"l0_pass", "l1_pass", "citation_proxy", "within_budget", "gate_passed"} <= set(gates)


async def test_run_eval_holdout_requires_explicit_confirmation(tmp_path: Path) -> None:
    settings = Settings(
        llm_api_key=SecretStr("eval-test"),
        database_url="postgresql+asyncpg://devkb:x@127.0.0.1:9/devkb",
    )
    with pytest.raises(InvalidInputError, match="confirm-holdout"):
        await run_eval(
            settings,
            split="holdout",
            modes=["vector-exact"],
            project_slug="any",
            output_dir=tmp_path,
            confirm_holdout=False,
        )


async def test_run_eval_holdout_requires_mode_all(tmp_path: Path) -> None:
    """§7：holdout 一次性访问不得只跑部分模式（confirm 了也一样拒绝）。"""
    settings = Settings(
        llm_api_key=SecretStr("eval-test"),
        database_url="postgresql+asyncpg://devkb:x@127.0.0.1:9/devkb",
    )
    duplicated = [*EVAL_MODES, "agentic"]  # set 相等但列表不等：重复模式同样拒绝
    for partial in (["agentic"], ["lexical"], ["vector-exact", "vector-hnsw"], duplicated):
        with pytest.raises(InvalidInputError, match="mode all"):
            await run_eval(
                settings,
                split="holdout",
                modes=partial,  # type: ignore[arg-type]
                project_slug="any",
                output_dir=tmp_path,
                confirm_holdout=True,
            )
    # 被模式校验挡下的尝试不写台账（未读题即未访问）
    assert not (tmp_path / "evalsets" / HOLDOUT_LEDGER_NAME).exists()


async def test_run_eval_holdout_full_run_uses_split_aware_gates(
    session: AsyncSession, migrated_db_url: str, tmp_path: Path
) -> None:
    slug = f"t20h-{uuid.uuid4().hex[:8]}"
    await _seed_project(session, slug)
    evalsets_dir = _write_mini_evalsets(tmp_path / "evalsets")
    settings = Settings(llm_api_key=SecretStr("eval-test"), database_url=migrated_db_url)

    written = await run_eval(
        settings,
        split="holdout",
        modes=list(EVAL_MODES),
        project_slug=slug,
        output_dir=tmp_path / "reports",
        evalsets_dir=evalsets_dir,
        embedder=FakeEmbedder(),
        llm=_RoutedLLM(),
        confirm_holdout=True,
        enforce_counts=False,
    )

    assert len(written) == 1  # holdout：单份合并报告
    json_path, markdown_path = written[0]
    assert json_path.name.startswith("p1-holdout-")
    report = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert report["schema_version"] == "p1-eval-v1.2"
    assert report["run"]["holdout_accessed"] is True
    assert report["run"]["holdout_attempt"] == 1
    assert report["run"]["holdout_rerun_acknowledged"] is None
    assert "本报告是第" not in markdown  # 首次运行不打重跑标记

    # §7 逐题原始结果：完整 Answer 落盘（含不可答题的判定理由）
    rows = report["agentic"]["questions"]
    for row in rows:
        answer = row["answer"]
        assert set(answer) >= {
            "answer_text",
            "claims",
            "citations",
            "not_found",
            "limitations",
            "mode",
            "trace_summary",
        }
        assert answer["mode"] == row["mode"]
    unanswerable = [row for row in rows if not row["answerable"]]
    assert unanswerable and all(row["answer"]["answer_text"] for row in unanswerable)
    assert "不可答题判定与理由" in markdown
    assert all(row["id"] in markdown for row in unanswerable)

    # §7 第 3 条：P0 历史基线对照 + 语料规模变化声明，且不产生 Gate 判定
    baseline = report["p0_baseline"]
    assert baseline["baseline"]["retrieval"]["recall_at_10"] == 0.875
    assert baseline["baseline"]["corpus"] == {
        "document_count": 39,
        "chunk_count": 1370,
        "scope": "P0 冻结 Markdown 范围",
    }
    assert baseline["current_vector_exact"] is not None
    assert set(baseline["delta_vs_p0"]) == {"recall_at_5", "recall_at_10", "mrr_at_10"}
    assert baseline["corpus_scale"]["changed"] is True  # fixture 语料 5 文档 ≠ P0 39
    assert "与 P0 holdout 历史基线对照" in markdown and "0.875" in markdown
    assert "不作 P1 硬 Gate" in markdown

    # 一次性访问台账：started + completed 各一条
    ledger = read_holdout_ledger(evalsets_dir)
    assert [record["event"] for record in ledger] == ["started", "completed"]
    assert ledger[0]["attempt"] == 1 and ledger[0]["modes"] == list(EVAL_MODES)
    assert ledger[0]["devkb_commit"] and ledger[0]["acknowledge_rerun"] is None
    assert ledger[1]["reports"] == [str(json_path)]
    retrieval_gate = report["gates"]["retrieval"]
    # §7 硬 Gate：vector-hnsw R@10 ≥ 同快照 vector-exact；overlap 只记录不判定
    assert isinstance(retrieval_gate["hnsw_recall_ge_exact"], bool)
    assert retrieval_gate["gate_passed"] == retrieval_gate["hnsw_recall_ge_exact"]
    assert "hnsw_overlap_gate" not in retrieval_gate
    assert retrieval_gate["hnsw_overlap_mean"] is not None
    agentic_gate = report["gates"]["agentic"]
    # §7：拒答/误拒只记录；硬项 = L0/L1/预算/终态/无失败 run
    assert agentic_gate["correct_unanswerable_gate"] is None
    assert agentic_gate["false_refusal_gate"] is None
    assert agentic_gate["no_failed_runs"] is True
    assert agentic_gate["gate_passed"] is True


async def test_run_eval_holdout_second_run_requires_acknowledged_rerun(
    session: AsyncSession, migrated_db_url: str, tmp_path: Path
) -> None:
    """§7：不得静默重跑挑最好成绩——重跑须带裁决理由，且报告显著标记。"""
    slug = f"t20r-{uuid.uuid4().hex[:8]}"
    await _seed_project(session, slug)
    evalsets_dir = _write_mini_evalsets(tmp_path / "evalsets")
    output_dir = tmp_path / "reports"
    settings = Settings(llm_api_key=SecretStr("eval-test"), database_url=migrated_db_url)

    async def _run(acknowledge: str | None = None) -> list[tuple[Path, Path]]:
        return await run_eval(
            settings,
            split="holdout",
            modes=list(EVAL_MODES),
            project_slug=slug,
            output_dir=output_dir,
            evalsets_dir=evalsets_dir,
            embedder=FakeEmbedder(),
            llm=_RoutedLLM(),
            confirm_holdout=True,
            acknowledge_rerun=acknowledge,
            enforce_counts=False,
        )

    await _run()
    # 第二次：仅 --confirm-holdout 不够，必须带用户裁决理由
    with pytest.raises(InvalidInputError, match="acknowledge-rerun"):
        await _run()
    # 报告名按秒取时间戳且同名拒绝覆盖：跨过秒边界才能验证被裁决允许的重跑
    await asyncio.sleep(1.05)
    assert [record["event"] for record in read_holdout_ledger(evalsets_dir)] == [
        "started",
        "completed",
    ]  # 被拒的重跑不算一次访问

    reason = "用户裁决：机制缺陷修复后重跑，见偏差记录 2026-07-20"
    written = await _run(reason)
    report = json.loads(written[0][0].read_text(encoding="utf-8"))
    markdown = written[0][1].read_text(encoding="utf-8")
    assert report["run"]["holdout_attempt"] == 2
    assert report["run"]["holdout_rerun_acknowledged"] == reason
    assert "本报告是第 2 次 holdout 运行" in markdown and reason in markdown
    ledger = read_holdout_ledger(evalsets_dir)
    assert [record["event"] for record in ledger] == [
        "started",
        "completed",
        "started",
        "completed",
    ]
    assert ledger[2]["attempt"] == 2 and ledger[2]["acknowledge_rerun"] == reason


async def test_run_eval_holdout_cannot_be_rerun_via_a_different_output_dir(
    session: AsyncSession, migrated_db_url: str, tmp_path: Path
) -> None:
    """台账锚在题集目录：换 --output-dir 不能重开一本新台账（2026-07-20 复评探针）。"""
    slug = f"t20o-{uuid.uuid4().hex[:8]}"
    await _seed_project(session, slug)
    evalsets_dir = _write_mini_evalsets(tmp_path / "evalsets")
    settings = Settings(llm_api_key=SecretStr("eval-test"), database_url=migrated_db_url)

    async def _run(output_dir: Path) -> list[tuple[Path, Path]]:
        return await run_eval(
            settings,
            split="holdout",
            modes=list(EVAL_MODES),
            project_slug=slug,
            output_dir=output_dir,
            evalsets_dir=evalsets_dir,
            embedder=FakeEmbedder(),
            llm=_RoutedLLM(),
            confirm_holdout=True,
            enforce_counts=False,
        )

    await _run(tmp_path / "reports-a")
    with pytest.raises(InvalidInputError, match="acknowledge-rerun"):
        await _run(tmp_path / "reports-b")
    ledger = read_holdout_ledger(evalsets_dir)
    assert [record["event"] for record in ledger] == ["started", "completed"]
    assert ledger[0]["output_dir"] == str(tmp_path / "reports-a")  # 输出目录随尝试留痕
    assert not (tmp_path / "reports-b").exists()


async def test_run_eval_holdout_failed_attempt_is_recorded_in_ledger(tmp_path: Path) -> None:
    """中断/失败的尝试也必须留痕：读题前记 started，异常后记 failed。"""
    evalsets_dir = _write_mini_evalsets(tmp_path / "evalsets")
    output_dir = tmp_path / "reports"
    settings = Settings(
        llm_api_key=SecretStr("eval-test"),
        database_url="postgresql+asyncpg://devkb:x@127.0.0.1:9/devkb",
    )
    with pytest.raises(Exception):  # noqa: B017 — 连接不上的具体异常类型不重要
        await run_eval(
            settings,
            split="holdout",
            modes=list(EVAL_MODES),
            project_slug="missing",
            output_dir=output_dir,
            evalsets_dir=evalsets_dir,
            embedder=FakeEmbedder(),
            llm=_RoutedLLM(),
            confirm_holdout=True,
            enforce_counts=False,
        )
    ledger = read_holdout_ledger(evalsets_dir)
    assert [record["event"] for record in ledger] == ["started", "failed"]
    assert ledger[1]["attempt"] == 1 and ledger[1]["error"]
    # 失败尝试同样占用一次性访问：下一次运行仍需裁决理由
    assert (evalsets_dir / HOLDOUT_LEDGER_NAME).is_file()


async def test_run_eval_lexical_only_never_loads_real_embedder(
    session: AsyncSession,
    migrated_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯 lexical 评测是轻量路径：绝不 import/加载真实 embedding 模型。"""
    slug = f"t20l-{uuid.uuid4().hex[:8]}"
    await _seed_project(session, slug)
    evalsets_dir = _write_mini_evalsets(tmp_path / "evalsets")

    def _forbid(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("lexical-only 评测不得实例化 SentenceTransformerEmbedder")

    monkeypatch.setattr("devkb.embedding.SentenceTransformerEmbedder", _forbid)
    settings = Settings(llm_api_key=SecretStr("eval-test"), database_url=migrated_db_url)
    written = await run_eval(
        settings,
        split="dev",
        modes=["lexical"],
        project_slug=slug,
        output_dir=tmp_path / "reports",
        evalsets_dir=evalsets_dir,
        enforce_counts=False,
    )
    assert len(written) == 1
    report = json.loads(written[0][0].read_text(encoding="utf-8"))
    assert set(report["retrieval"]["metrics"]) == {"lexical"}
    # 无 exact+hnsw 对照：本报告不判定检索硬 Gate
    assert report["gates"]["retrieval"]["gate_passed"] is None


def test_write_report_refuses_overwrite(tmp_path: Path) -> None:
    report: dict[str, Any] = {"schema_version": "p1-eval-v1.2"}
    write_report(report, "# md", tmp_path, "p1-dev-retrieval-x")
    with pytest.raises(InvalidInputError, match="拒绝覆盖"):
        write_report(report, "# md", tmp_path, "p1-dev-retrieval-x")


def test_expand_modes_enum() -> None:
    assert expand_modes("all") == list(EVAL_MODES)
    assert expand_modes("vector-hnsw") == ["vector-hnsw"]
    with pytest.raises(InvalidInputError):
        expand_modes("vector")  # 别名不合法（Evaluation-v1 §3）
    with pytest.raises(InvalidInputError):
        expand_modes("agentic-hybrid")
