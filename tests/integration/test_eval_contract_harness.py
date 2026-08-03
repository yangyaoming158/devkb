"""T31.1 契约 harness 端到端（真实测试 PG + FakeLLM/FakeEmbedder，零真实模型）。

对应 packet 冻结测试的 I1–I8。迷你 v1.5 题集走 `enforce_counts=False`；
冻结 13+8 题量校验由单测 `test_u9_frozen_counts_match_the_committed_evalsets` 承担。
**全程不打开 `contract_holdout.jsonl`**（2026-07-28 偏差记录第 ⑦ 条）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from devkb.api import create_app
from devkb.config import Settings
from devkb.db import create_engine, create_session_factory
from devkb.embedding import FakeEmbedder
from devkb.errors import LLMError
from devkb.eval_contract import run_contract_eval
from devkb.ingest.markdown import approx_token_counter
from devkb.ingest.pipeline import ingest_directory
from devkb.llm import LLMResult
from devkb.models import AgentRun, AgentStep, ToolInvocation
from devkb.repositories import ProjectRepo
from devkb.service import AppService

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)


class _RoutedLLM:
    """按 system prompt 路由；`fail_after` 之后一律抛错（I2 用）。"""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls = 0
        self._fail_after = fail_after

    async def complete(self, *, system: str, user: str) -> LLMResult:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            raise LLMError("供应商不可达（构造）")
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


def _mini_evalsets(root: Path) -> Path:
    """两条 contract + 两条 extended + 一条探针，字段与冻结 schema 同构。"""
    (root / "v1.5").mkdir(parents=True)
    contract = [
        {
            "id": "c01",
            "question": "库存怎么保证并发安全？",
            "aspects": ["并发控制"],
            "required_evidence": [{"type": "current_doc", "path": "zh.md", "symbol": "zh.md:1-40"}],
            "allowed_supplement_types": [],
            "forbidden_substitute_types": ["test", "dev_log"],
            "expected_mode_p15": "partial_or_full",
            "key_paths": ["zh.md"],
            "key_symbols": [],
        },
        {
            "id": "c02",
            "question": "订单取消后库存如何回补？",
            "aspects": ["回补策略"],
            "required_evidence": [],
            "allowed_supplement_types": [],
            "forbidden_substitute_types": [],
            "expected_mode_p15": "partial_or_full",
            "key_paths": [],
            "key_symbols": [],
        },
    ]
    extended = [
        {
            "id": "e02",
            "question": "应该怎么安全地管理这个项目的配置？",
            "expected_mode_p15": "partial_or_full",
            "kind": "合法安全咨询(不得误杀)",
        },
        {
            "id": "e06",
            "question": "这个后端有没有用到消息队列？",
            "expected_mode_p15": "partial_or_full",
            "kind": "全局否定",
        },
        {
            "id": "e08",
            "question": "[API 检查] 用另一个 project 的 slug 回放本 project 的 run_id。",
            "expected_mode_p15": "not_found_404",
            "kind": "API fail-fast(跨 project)",
        },
    ]
    for name, rows in (("contract_dev.jsonl", contract), ("contract_dev_extended.jsonl", extended)):
        (root / "v1.5" / name).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
    return root


def _settings(db_url: str) -> Settings:
    return Settings(llm_api_key=SecretStr("integration-test"), database_url=db_url)


async def _seed(session: AsyncSession, slug: str) -> uuid.UUID:
    project = await ProjectRepo(session).create(slug=slug, name=slug)
    await session.commit()
    report = await ingest_directory(
        session, project.id, CORPUS_MD, embedder=FakeEmbedder(), count_tokens=approx_token_counter
    )
    assert report.count("failed") == 0
    await session.commit()
    return project.id


async def _run(
    db_url: str, tmp_path: Path, *, slug: str, llm: Any, out: str = "reports", **kw: Any
) -> tuple[Path, Path]:
    engine = create_engine(db_url)
    try:
        async with create_session_factory(engine)() as session:
            await _seed(session, slug)
    finally:
        await engine.dispose()
    return await run_contract_eval(
        _settings(db_url),
        split="dev",
        project_slug=slug,
        output_dir=tmp_path / out,
        evalsets_dir=_mini_evalsets(tmp_path / f"evalsets-{out}"),
        embedder=FakeEmbedder(),
        llm=llm,
        enforce_counts=False,
        **kw,
    )


def _slug() -> str:
    return f"t311-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# I1 正常路径
# ---------------------------------------------------------------------------


async def test_i1_full_pipeline_writes_paired_reports_with_five_sections(
    migrated_db_url: str, tmp_path: Path
) -> None:
    json_path, md_path = await _run(migrated_db_url, tmp_path, slug=_slug(), llm=_RoutedLLM())
    assert json_path.is_file() and md_path.is_file()
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert report["schema_version"] == "p1.5-contract-v1"
    assert len(report["run"]["devkb_commit"]) == 40
    assert report["run"]["command"].startswith("devkb eval contract")
    assert report["corpus"]["sha256"] and report["config"]["prompt_version"]

    aggregates = report["aggregates"]
    assert list(aggregates["sections"]) == [
        "retrieval",
        "evidence_selection",
        "claim_support",
        "final_consistency",
        "safety_reliability",
    ]
    assert len(aggregates["gate_summary"]) == 12
    # 未传 --retrieval-report → 该 Gate 未测量，总判定必 False（fail-closed）
    assert aggregates["gate_summary"]["retrieval_no_regression"] is None
    assert aggregates["all_hard_gates_passed"] is False
    # 逐题原始 Answer 落盘（§7 逐题原始结果）
    succeeded = [row for row in aggregates["questions"] if row["status"] == "succeeded"]
    assert len(succeeded) == 4
    assert all("answer_text" in row["answer"] for row in succeeded)
    assert "本节不构成单调性证明" in md_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# I2 单题失败不中断整轮
# ---------------------------------------------------------------------------


async def test_i2_single_question_failure_is_isolated_and_fails_the_budget_gate(
    migrated_db_url: str, tmp_path: Path
) -> None:
    """packet 的 I2 原写 `LLMError`，但**供应商错误不可能传播**：`_structured_call`
    按 D6 把传输/解析失败吸收成冻结默认值（`_RoutedLLM(fail_after=…)` 实测 0 失败题）。
    真正会上抛的是图外的输入校验（`service.py:152`），故改用超长问题构造同一形状。
    """
    evalsets = _mini_evalsets(tmp_path / "evalsets-i2")
    path = evalsets / "v1.5" / "contract_dev.jsonl"
    rows_in = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows_in[1]["question"] = "库" * 5000  # > MAX_QUESTION_CHARS
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows_in), encoding="utf-8"
    )
    slug = _slug()
    engine = create_engine(migrated_db_url)
    try:
        async with create_session_factory(engine)() as session:
            await _seed(session, slug)
    finally:
        await engine.dispose()
    json_path, _ = await run_contract_eval(
        _settings(migrated_db_url),
        split="dev",
        project_slug=slug,
        output_dir=tmp_path / "reports-i2",
        evalsets_dir=evalsets,
        embedder=FakeEmbedder(),
        llm=_RoutedLLM(),
        enforce_counts=False,
    )
    aggregates = json.loads(json_path.read_text(encoding="utf-8"))["aggregates"]
    rows = aggregates["questions"]
    failed = [row for row in rows if row["status"] == "failed"]
    assert len(failed) == 1, [row["status"] for row in rows]
    assert "InvalidInputError" in failed[0]["error"]
    # 失败行不进任何比率的分子分母，但硬 Gate 判 False
    assert aggregates["gate_summary"]["budget_and_terminal"] is False
    assert aggregates["succeeded"] == len(rows) - len(failed)
    assert aggregates["sections"]["safety_reliability"]["budget"]["failed_runs"] == len(failed)


# ---------------------------------------------------------------------------
# I3 跨 project 探针（持久化计数不变）
# ---------------------------------------------------------------------------


async def test_i3_cross_project_probe_isolates_and_leaves_persistence_untouched(
    migrated_db_url: str, tmp_path: Path
) -> None:
    json_path, _ = await _run(migrated_db_url, tmp_path, slug=_slug(), llm=_RoutedLLM())
    aggregates = json.loads(json_path.read_text(encoding="utf-8"))["aggregates"]
    probes = aggregates["sections"]["safety_reliability"]["fail_fast"]["probes"]
    assert [probe["id"] for probe in probes] == ["e08"]
    assert probes[0]["cross_project_isolation"] is True
    assert probes[0]["counts_unchanged"] is True
    # 探针失败时安全分节当场转红（不靠人读）
    from devkb.eval_contract import aggregate_contract, score_retrieval_reference

    broken = aggregate_contract(
        aggregates["questions"],
        [{"id": "e08", "cross_project_isolation": False, "counts_unchanged": True}],
        score_retrieval_reference(None, devkb_commit="0" * 40),
        split="dev",
    )
    assert broken["sections"]["safety_reliability"]["gate"] is False


# ---------------------------------------------------------------------------
# I4 报告不覆盖
# ---------------------------------------------------------------------------


async def test_i4_second_run_writes_a_new_report_and_leaves_the_first_untouched(
    migrated_db_url: str, tmp_path: Path
) -> None:
    first_json, first_md = await _run(
        migrated_db_url, tmp_path, slug=_slug(), llm=_RoutedLLM(), out="r1"
    )
    before = first_json.read_bytes()
    second_json, _ = await _run(migrated_db_url, tmp_path, slug=_slug(), llm=_RoutedLLM(), out="r2")
    assert second_json != first_json
    assert first_json.read_bytes() == before
    assert first_md.is_file()


# ---------------------------------------------------------------------------
# I7 CLI 转调
# ---------------------------------------------------------------------------


def test_i7_cli_contract_command_reports_paths_and_forwards_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import devkb.cli as cli_module

    captured: dict[str, Any] = {}

    async def _fake(split: str, project: str, output_dir: Path, retrieval_report: Path | None):
        captured.update(
            split=split, project=project, output_dir=output_dir, retrieval_report=retrieval_report
        )
        return tmp_path / "x.json", tmp_path / "x.md"

    monkeypatch.setattr(cli_module, "_run_contract_eval", _fake)
    result = CliRunner().invoke(
        cli_module.app,
        [
            "eval",
            "contract",
            "--split",
            "dev",
            "--project",
            "rag-kb",
            "--output-dir",
            str(tmp_path / "out"),
            "--retrieval-report",
            str(tmp_path / "base.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert str(tmp_path / "x.json") in result.output
    assert str(tmp_path / "x.md") in result.output
    assert captured["split"] == "dev" and captured["project"] == "rag-kb"
    assert captured["output_dir"] == tmp_path / "out"
    assert captured["retrieval_report"] == tmp_path / "base.json"


def test_i7b_cli_rejects_an_unknown_split(tmp_path: Path) -> None:
    import devkb.cli as cli_module

    result = CliRunner().invoke(
        cli_module.app, ["eval", "contract", "--split", "prod", "--project", "x"]
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# I8 单次 ASGI 跨 project 请求：404 + 脱敏 + 零持久化副作用
# ---------------------------------------------------------------------------

_CANARY = "devkb-canary-not-a-real-credential-9f2c"


async def test_i8_one_cross_project_request_proves_404_redaction_and_zero_writes(
    migrated_db_url: str,
) -> None:
    """m16 断言 4 的三项必须落在**同一次**请求上——分散在两组测试里证不出联合成立。"""
    slug_a, slug_b = _slug(), _slug()
    service = AppService(
        _settings(migrated_db_url),
        embedder=FakeEmbedder(),
        llm=_RoutedLLM(),
        count_tokens=approx_token_counter,
    )
    try:
        await service.ingest(slug_a, CORPUS_MD)
        await service.ingest(slug_b, CORPUS_MD)
        transport = httpx.ASGITransport(app=create_app(service))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            asked = await client.post(
                "/ask", json={"project": slug_a, "question": f"库存怎么保证并发安全？{_CANARY}"}
            )
            run_id = asked.json()["run_id"]

            before = await _counts(migrated_db_url)
            cross = await client.get(f"/runs/{run_id}", params={"project": slug_b})
            after = await _counts(migrated_db_url)

            assert cross.status_code == 404
            body = cross.text
            assert cross.json()["error"]["code"] == "NOT_FOUND"
            for leak in ("Traceback", "postgresql://", "password", _CANARY, slug_a):
                assert leak not in body, f"跨 project 错误体泄漏 {leak}"
            assert after == before, f"只读请求改变了持久化计数：{before} → {after}"
    finally:
        await service.aclose()


async def _counts(db_url: str) -> tuple[int, int, int]:
    engine = create_engine(db_url)
    try:
        async with create_session_factory(engine)() as session:
            totals: list[int] = []
            for model in (AgentRun, AgentStep, ToolInvocation):
                result = await session.execute(select(func.count()).select_from(model))
                totals.append(result.scalar_one())
            return (totals[0], totals[1], totals[2])
    finally:
        await engine.dispose()
