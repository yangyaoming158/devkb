from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner

from devkb import __version__
from devkb.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def _p0_answer() -> dict[str, Any]:
    """P0 老 Answer：无 mode/claims/not_found/limitations（旧 runs 兼容）。"""
    return {
        "answer_text": "订单状态由 OrderStatus 枚举管理 [E1]。",
        "citations": [
            {
                "evidence_id": "E1",
                "chunk_id": "00000000-0000-0000-0000-000000000001",
                "rel_path": "src/OrderStatus.java",
                "start_line": 10,
                "end_line": 20,
                "title_path": "OrderStatus",
                "score": 0.8123,
            }
        ],
        "warnings": ["L0: 引用 [E9] 不在本次证据集合（共 1 条），已剔除"],
        "stats": {"tokens_in": 100, "tokens_out": 50, "latency_ms": 900, "model": "deepseek-chat"},
        "run_id": "00000000-0000-0000-0000-0000000000aa",
    }


def _v1_answer() -> dict[str, Any]:
    answer = _p0_answer()
    answer.update(
        mode="partial",
        claims=[
            {
                "text": "订单状态由 OrderStatus 枚举管理",
                "evidence_ids": ["E1"],
                "quotes": ["enum OrderStatus"],
            }
        ],
        not_found=["支付回调的状态迁移"],
        limitations=["仅回答了现有证据支持的部分，未覆盖方面见 not_found"],
        trace_summary="节点 plan>retrieve>evaluate>generate>verify>finalize；模式 partial",
    )
    return answer


def _invoke_ask_with(monkeypatch: Any, answer: dict[str, Any], *extra_args: str) -> Any:
    async def fake_run_ask(
        question: str, project: str, top_k: int | None, pipeline: str = "agentic"
    ) -> dict[str, Any]:
        return answer

    monkeypatch.setattr("devkb.cli._run_ask", fake_run_ask)
    return runner.invoke(app, ["ask", "问题", "--project", "demo", *extra_args])


def test_ask_renders_legacy_p0_answer_without_mode(monkeypatch: Any) -> None:
    result = _invoke_ask_with(monkeypatch, _p0_answer())
    assert result.exit_code == 0
    assert "OrderStatus" in result.stdout and "[E1]" in result.stdout
    assert "mode=" not in result.stdout
    assert "deepseek-chat" in result.stdout


def test_ask_renders_answer_v1_fields_and_keeps_code_verbatim(monkeypatch: Any) -> None:
    result = _invoke_ask_with(monkeypatch, _v1_answer())
    assert result.exit_code == 0
    assert "mode=partial" in result.stdout
    assert "not_found" in result.stdout and "支付回调的状态迁移" in result.stdout
    assert "limitation" in result.stdout
    assert "OrderStatus" in result.stdout


def test_ask_json_output_is_full_answer_v1(monkeypatch: Any) -> None:
    result = _invoke_ask_with(monkeypatch, _v1_answer(), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "partial"
    assert payload["claims"][0]["quotes"] == ["enum OrderStatus"]


def test_ask_service_error_renders_stable_code_without_traceback(monkeypatch: Any) -> None:
    from devkb.errors import InternalError

    async def fake_run_ask(
        question: str, project: str, top_k: int | None, pipeline: str = "agentic"
    ) -> dict[str, Any]:
        raise InternalError("内部错误（error_id=abc123def456）", error_id="abc123def456")

    monkeypatch.setattr("devkb.cli._run_ask", fake_run_ask)
    result = runner.invoke(app, ["ask", "问题", "--project", "demo"])
    assert result.exit_code == 1
    assert "INTERNAL_ERROR" in result.output and "abc123def456" in result.output
    assert "Traceback" not in result.output


def test_ask_rejects_invalid_pipeline_and_out_of_range_top_k(monkeypatch: Any) -> None:
    assert _invoke_ask_with(monkeypatch, _v1_answer(), "--pipeline", "agent-x").exit_code != 0
    assert _invoke_ask_with(monkeypatch, _v1_answer(), "--top-k", "0").exit_code != 0
    assert _invoke_ask_with(monkeypatch, _v1_answer(), "--top-k", "13").exit_code != 0
    assert _invoke_ask_with(monkeypatch, _v1_answer(), "--top-k", "12").exit_code == 0


def _trace_payload() -> dict[str, Any]:
    """含补检+重生成+降级的回放载荷（结构与 get_run_trace 一致）。"""
    return {
        "run": {
            "run_id": "00000000-0000-0000-0000-0000000000aa",
            "question": "订单取消后库存是如何回滚的？",
            "status": "succeeded",
            "model": "deepseek-v4-flash",
            "tokens_in": 400,
            "tokens_out": 200,
            "usage": {"llm_calls": 4},
            "cost": "0.000720",
            "latency_ms": 1234,
            "created_at": "2026-07-19T10:00:00",
            "answer": {"mode": "partial"},
        },
        "steps": [
            {
                "seq": 1,
                "node": "plan",
                "attempt": 1,
                "status": "ok",
                "latency_ms": 10,
                "error": None,
                "input_summary": {"question_chars": 13},
                "output_summary": {
                    "queries": ["库存回滚"],
                    "llm_requests": [
                        {
                            "call_key": "plan",
                            "status": "ok",
                            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                            "cost": "0.000180",
                        }
                    ],
                },
                "tools": [],
            },
            {
                "seq": 2,
                "node": "retrieve",
                "attempt": 1,
                "status": "degraded",
                "latency_ms": 20,
                "error": "retrieve:SQLAlchemyError",
                "input_summary": {"queries": ["库存回滚"], "round": 0},
                "output_summary": {"retrieval_round": 1, "evidence_count": 0, "rel_paths": []},
                "tools": [{"tool_name": "retrieve", "status": "failed"}],
            },
            {
                "seq": 3,
                "node": "evaluate",
                "attempt": 1,
                "status": "ok",
                "latency_ms": 30,
                "error": None,
                "input_summary": {"evidence_count": 0, "round": 1},
                "output_summary": {
                    "sufficiency": "insufficient",
                    "supported_count": 0,
                    "missing_aspects": ["回滚补偿"],
                },
                "tools": [],
            },
            {
                "seq": 4,
                "node": "finalize",
                "attempt": 1,
                "status": "ok",
                "latency_ms": 1,
                "error": None,
                "input_summary": {},
                "output_summary": {"final_mode": "refusal", "claim_count": 0},
                "tools": [],
            },
        ],
    }


def test_runs_replay_renders_verdicts_tools_and_degrade_reason(monkeypatch: Any) -> None:
    async def fake_load(run_id: str, project: str) -> dict[str, Any]:
        return _trace_payload()

    monkeypatch.setattr("devkb.cli._runs_replay", fake_load)
    result = runner.invoke(
        app,
        ["runs", "replay", "00000000-0000-0000-0000-0000000000aa", "--project", "demo"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    out = result.stdout
    # 回放能看出为什么补检/拒答：insufficient + 缺失方面 + refusal 终态
    assert "insufficient" in out and "回滚补偿" in out
    assert "mode=refusal" in out
    # 降级原因与失败工具可见
    assert "SQLAlchemyError" in out and "retrieve:failed" in out
    assert "plan#1" in out and "degraded" in out


def test_runs_replay_not_found_exits_with_error(monkeypatch: Any) -> None:
    from devkb.errors import NotFoundError

    async def fake_load(run_id: str, project: str) -> dict[str, Any]:
        raise NotFoundError(f"run {run_id} 不存在于项目 '{project}'")

    monkeypatch.setattr("devkb.cli._runs_replay", fake_load)
    result = runner.invoke(
        app, ["runs", "replay", "00000000-0000-0000-0000-0000000000bb", "--project", "demo"]
    )
    assert result.exit_code == 1
    assert "不存在" in result.stdout


def test_runs_show_includes_step_summaries(monkeypatch: Any) -> None:
    async def fake_load(run_id: str, project: str) -> dict[str, Any]:
        return _trace_payload()

    monkeypatch.setattr("devkb.cli._load_run_trace", fake_load)
    result = runner.invoke(
        app, ["runs", "show", "00000000-0000-0000-0000-0000000000aa", "--project", "demo"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [step["node"] for step in payload["steps"]] == [
        "plan",
        "retrieve",
        "evaluate",
        "finalize",
    ]
    assert payload["steps"][1]["status"] == "degraded"
    assert payload["answer"] == {"mode": "partial"}


def test_ask_routes_pipeline_flag_to_service(monkeypatch: Any) -> None:
    seen: list[str] = []

    async def fake_run_ask(
        question: str, project: str, top_k: int | None, pipeline: str = "agentic"
    ) -> dict[str, Any]:
        seen.append(pipeline)
        return _v1_answer()

    monkeypatch.setattr("devkb.cli._run_ask", fake_run_ask)
    assert runner.invoke(app, ["ask", "q", "--project", "demo"]).exit_code == 0
    assert (
        runner.invoke(app, ["ask", "q", "--project", "demo", "--pipeline", "fixed-rag"]).exit_code
        == 0
    )
    assert seen == ["agentic", "fixed-rag"]
