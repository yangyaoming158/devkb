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
    async def fake_run_ask(question: str, project: str, top_k: int | None) -> dict[str, Any]:
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
