"""devkb 命令行入口（规格 §4：ingest / ask / runs，P0 唯一 API）。"""

from __future__ import annotations

import asyncio
import json
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from devkb import __version__
from devkb.errors import ConfigError, DevKbError
from devkb.ingest.pipeline import IngestReport
from devkb.logging import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="devkb — 软件项目知识助手")
runs_app = typer.Typer(no_args_is_help=True, help="查看历史 run")
app.add_typer(runs_app, name="runs")
eval_app = typer.Typer(no_args_is_help=True, help="Evaluation v1 评测（报告落盘，不覆盖历史）")
app.add_typer(eval_app, name="eval")
console = Console()
DEFAULT_REPORT_DIR = Path("evalsets/reports")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", help="打印版本号并退出"),
) -> None:
    if version:
        typer.echo(f"devkb {__version__}")
        raise typer.Exit()
    try:
        from devkb.config import get_settings

        settings = get_settings()
        configure_logging(level=settings.log_level, app_env=settings.app_env)
    except ConfigError:
        # 配置缺失时仍用默认日志配置；具体命令执行时会给出明确的配置错误
        configure_logging()


@app.command()
def ingest(
    directory: Path = typer.Argument(..., exists=True, file_okay=False, help="待摄取目录"),
    project: str = typer.Option(..., "--project", help="项目 slug（不存在则创建）"),
) -> None:
    """摄取目录下的 .md/.txt 文档（幂等：未变更文档跳过）。"""
    try:
        report = asyncio.run(_run_ingest(directory, project))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc

    table = Table(title=f"ingest {directory} → project '{project}'")
    table.add_column("结果")
    table.add_column("文件数", justify="right")
    table.add_column("chunks", justify="right")
    table.add_row(
        "成功", str(report.count("ingested")), str(sum(o.chunks for o in report.outcomes))
    )
    table.add_row("跳过（未变更）", str(report.count("skipped")), "-")
    table.add_row("失败", str(report.count("failed")), "-")
    console.print(table)

    failures = [o for o in report.outcomes if o.status == "failed"]
    if failures:
        fail_table = Table(title="失败原因")
        fail_table.add_column("文件")
        fail_table.add_column("原因")
        for o in failures:
            fail_table.add_row(o.rel_path, o.error or "")
        console.print(fail_table)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（默认仅本机回环）"),
    port: int = typer.Option(8000, "--port", help="监听端口"),
) -> None:
    """启动本地 HTTP API（P1 无鉴权，默认只监听 127.0.0.1，不得直接暴露公网）。"""
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            "[yellow]警告[/yellow] P1 API 无鉴权：监听非回环地址意味着"
            "同网络任何人都能读取知识库与历史 run，请勿暴露公网"
        )
    import uvicorn

    from devkb.api import create_app

    uvicorn.run(create_app(), host=host, port=port)


@app.command("backfill-search")
def backfill_search(
    project: str = typer.Option(..., "--project", help="项目 slug"),
) -> None:
    """为已有 chunks 回填 FTS search_text（幂等；失败整体回滚，不改 embedding）。"""
    try:
        total, updated = asyncio.run(_run_backfill_search(project))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"project '{project}'：chunks 共 {total}，本次更新 {updated}（重跑幂等应为 0）")


@app.command()
def ask(
    question: str = typer.Argument(..., help="要提问的问题"),
    project: str = typer.Option(..., "--project", help="项目 slug"),
    top_k: int = typer.Option(None, "--top-k", help="检索条数（默认取配置）"),
    pipeline: str = typer.Option(
        "agentic", "--pipeline", help="问答链路：agentic（P1 默认）| fixed-rag（P0 对照）"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出完整 Answer JSON"),
) -> None:
    """向项目知识库提问，输出带引用的中文回答。"""
    from devkb.retrieval import MAX_FINAL_TOP_K

    if pipeline not in ("agentic", "fixed-rag"):
        raise typer.BadParameter("pipeline 只支持 agentic | fixed-rag", param_hint="--pipeline")
    if top_k is not None and not 1 <= top_k <= MAX_FINAL_TOP_K:
        raise typer.BadParameter(f"top_k 须在 1..{MAX_FINAL_TOP_K}", param_hint="--top-k")
    try:
        answer = asyncio.run(_run_ask(question, project, top_k, pipeline))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        console.print_json(json.dumps(answer, ensure_ascii=False))
        return
    _render_answer(answer)


def _render_answer(answer: dict[str, Any]) -> None:
    """兼容渲染：P0 Answer（无 mode/claims）与 P1 Answer v1 共用一个渲染器。"""
    mode = answer.get("mode")
    if mode is not None:
        color = {"full": "green", "partial": "yellow", "refusal": "red"}.get(mode, "white")
        console.print(f"[{color}]mode={mode}[/{color}]")
    console.print(answer["answer_text"])
    for item in answer.get("not_found", []):
        console.print(f"[yellow]not_found[/yellow] {item}")
    for item in answer.get("limitations", []):
        console.print(f"[dim]limitation[/dim] {item}")
    if answer["warnings"]:
        for w in answer["warnings"]:
            console.print(f"[yellow]warning[/yellow] {w}")
    if answer["citations"]:
        table = Table(title="引用")
        table.add_column("E#")
        table.add_column("位置")
        table.add_column("score", justify="right")
        for c in answer["citations"]:
            table.add_row(
                c["evidence_id"],
                f"{c['rel_path']}:L{c['start_line']}-L{c['end_line']} · {c['title_path']}",
                f"{c['score']:.3f}",
            )
        console.print(table)
    stats = answer["stats"]
    console.print(
        f"[dim]run={answer['run_id']} model={stats['model']} "
        f"tokens={stats['tokens_in']}+{stats['tokens_out']} {stats['latency_ms']}ms[/dim]"
    )


@runs_app.command("list")
def runs_list(
    project: str = typer.Option(..., "--project", help="项目 slug"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """列出最近的 run。"""
    try:
        rows = asyncio.run(_runs_list(project, limit))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc
    table = Table(title=f"runs · project '{project}'")
    for col in ("run_id", "状态", "问题", "tokens", "latency", "时间"):
        table.add_column(col)
    for r in rows:
        table.add_row(*r)
    console.print(table)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="run UUID"),
    project: str = typer.Option(..., "--project", help="项目 slug"),
) -> None:
    """查看单个 run 的完整 Answer JSON、元数据与步骤摘要。"""
    try:
        payload = asyncio.run(_runs_show(run_id, project))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


@runs_app.command("replay")
def runs_replay(
    run_id: str = typer.Argument(..., help="run UUID"),
    project: str = typer.Option(..., "--project", help="项目 slug"),
) -> None:
    """只读回放 run 轨迹：只查数据库，不重新执行任何节点/工具/LLM 调用。"""
    try:
        payload = asyncio.run(_runs_replay(run_id, project))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc
    _render_replay(payload)


def _step_verdict(node: str, summary: dict[str, Any]) -> str:
    """从 step 输出摘要还原节点判定：为什么补检/拒答/重生成在此可读。"""
    if node == "plan":
        return f"queries={summary.get('queries')}"
    if node == "retrieve":
        return f"round={summary.get('retrieval_round')}"
    if node == "evaluate":
        missing = summary.get("missing_aspects") or []
        verdict = str(summary.get("sufficiency"))
        return f"{verdict} 缺失:{'；'.join(missing)}" if missing else verdict
    if node == "refine":
        return f"补检 queries={summary.get('queries')}"
    if node == "generate":
        if summary.get("generate_failed"):
            return "生成失败(降级)"
        return f"claims={summary.get('claim_count')}"
    if node == "verify":
        if summary.get("passed"):
            return "L0/L1 通过"
        errors = summary.get("errors") or []
        return f"L0/L1 未过:{'；'.join(errors[:2])}"
    if node == "finalize":
        return f"mode={summary.get('final_mode')}"
    return ""


def _render_replay(payload: dict[str, Any]) -> None:
    run = payload["run"]
    status_color = {"succeeded": "green", "failed": "red"}.get(run["status"], "yellow")
    answer = run.get("answer") or {}
    mode = answer.get("mode")
    header = f"run {run['run_id']} [{status_color}]{run['status']}[/{status_color}]"
    console.print(header + (f" mode={mode}" if mode else ""))
    question = run["question"]
    console.print(f"[dim]Q: {question[:80]}{'…' if len(question) > 80 else ''}[/dim]")

    table = Table(title="replay（只读回放，不重新执行）")
    for col in ("seq", "节点", "状态", "判定", "工具", "证据数", "tokens/cost/latency", "原因"):
        table.add_column(col, overflow="fold")
    for step in payload["steps"]:
        summary = step.get("output_summary") or {}
        requests = summary.get("llm_requests") or []
        tokens = sum(
            int(r["usage"].get("prompt_tokens", 0)) + int(r["usage"].get("completion_tokens", 0))
            for r in requests
        )
        costs = [Decimal(r["cost"]) for r in requests if r.get("cost") is not None]
        evidence_count = summary.get("evidence_count")
        status_style = {"ok": "green", "degraded": "yellow", "failed": "red"}.get(
            step["status"], "white"
        )
        metrics = [str(tokens) if requests else "-", f"{sum(costs):.6f}" if costs else "-"]
        metrics.append(f"{step['latency_ms']}ms" if step["latency_ms"] is not None else "-")
        table.add_row(
            str(step["seq"]),
            f"{step['node']}#{step['attempt']}",
            f"[{status_style}]{step['status']}[/{status_style}]",
            _step_verdict(step["node"], summary),
            "; ".join(f"{t['tool_name']}:{t['status']}" for t in step["tools"]),
            "" if evidence_count is None else str(evidence_count),
            "/".join(metrics),
            step.get("error") or "",
        )
    console.print(table)
    console.print(
        f"[dim]总计 tokens={run['tokens_in']}+{run['tokens_out']} "
        f"cost={run['cost']} latency={run['latency_ms']}ms[/dim]"
    )


@eval_app.command("run")
def eval_run(
    split: str = typer.Option(..., "--split", help="dev | holdout"),
    mode: str = typer.Option(
        "all", "--mode", help="vector-exact|vector-hnsw|lexical|hybrid-rrf|agentic|all"
    ),
    project: str = typer.Option("mini-mall", "--project", help="项目 slug"),
    confirm_holdout: bool = typer.Option(
        False, "--confirm-holdout", help="holdout 只在最终验收运行一次，必须显式确认"
    ),
    acknowledge_rerun: str | None = typer.Option(
        None,
        "--acknowledge-rerun",
        help="holdout 已访问过时再次运行所需的用户裁决理由（报告中显著标记）",
    ),
    output_dir: Path = typer.Option(DEFAULT_REPORT_DIR, "--output-dir"),
) -> None:
    """按 split/mode 运行 Evaluation v1 评测，产出 JSON + Markdown 时间戳报告。"""
    if split not in ("dev", "holdout"):
        raise typer.BadParameter("split 只支持 dev | holdout", param_hint="--split")
    if split == "holdout" and not confirm_holdout:
        console.print(
            "[red]INVALID_INPUT[/red] holdout 只在最终验收运行一次：必须显式 --confirm-holdout"
        )
        raise typer.Exit(1)
    if split == "holdout" and mode != "all":
        console.print(
            "[red]INVALID_INPUT[/red] holdout 一次性访问必须完整运行："
            "--mode all（Evaluation-v1 §7），不得只跑部分模式"
        )
        raise typer.Exit(1)
    try:
        paths = asyncio.run(
            _run_eval(split, mode, project, confirm_holdout, output_dir, acknowledge_rerun)
        )
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc
    for json_path, markdown_path in paths:
        console.print(str(json_path))
        console.print(str(markdown_path))


def _eval_command(
    split: str,
    mode: str,
    project: str,
    confirm_holdout: bool,
    output_dir: Path,
    acknowledge_rerun: str | None,
) -> str:
    """报告里的复现命令：shlex.join 保证含空格/引号/$ 的参数可原样粘贴重放。

    裁决理由是自由文本、输出目录可含空格，直接插值会被 shell 拆参或展开。
    """
    argv = ["devkb", "eval", "run", "--split", split, "--mode", mode, "--project", project]
    if confirm_holdout:
        argv.append("--confirm-holdout")
    if acknowledge_rerun:
        argv += ["--acknowledge-rerun", acknowledge_rerun]
    # 输出目录进复现命令：报告落在哪里必须可追溯（台账另有独立固定位置）
    if output_dir != DEFAULT_REPORT_DIR:
        argv += ["--output-dir", str(output_dir)]
    return shlex.join(argv)


async def _run_eval(
    split: str,
    mode: str,
    project: str,
    confirm_holdout: bool,
    output_dir: Path,
    acknowledge_rerun: str | None = None,
) -> list[tuple[Path, Path]]:
    from devkb.config import get_settings
    from devkb.evaluation import expand_modes, run_eval

    modes = expand_modes(mode)
    command = _eval_command(split, mode, project, confirm_holdout, output_dir, acknowledge_rerun)
    return await run_eval(
        get_settings(),
        split=split,  # type: ignore[arg-type]
        modes=modes,
        project_slug=project,
        output_dir=output_dir,
        confirm_holdout=confirm_holdout,
        acknowledge_rerun=acknowledge_rerun,
        command=command,
    )


@asynccontextmanager
async def _app_service() -> AsyncIterator[Any]:
    """每条命令构造一个 AppService（懒 import：CLI 启动不拖入 ml/agent 依赖）。"""
    from devkb.config import get_settings
    from devkb.service import AppService

    service = AppService(get_settings())
    try:
        yield service
    finally:
        await service.aclose()


async def _run_backfill_search(project_slug: str) -> tuple[int, int]:
    async with _app_service() as service:
        return await service.backfill_search(project_slug)


async def _run_ask(
    question: str,
    project_slug: str,
    top_k: int | None,
    pipeline: str = "agentic",
) -> dict[str, Any]:
    async with _app_service() as service:
        return await service.ask(project_slug, question, top_k=top_k, pipeline=pipeline)


async def _runs_list(project_slug: str, limit: int) -> list[tuple[str, ...]]:
    async with _app_service() as service:
        rows = await service.list_runs(project_slug, limit)
    return [
        (
            r["run_id"],
            r["status"],
            (r["question"][:40] + "…") if len(r["question"]) > 40 else r["question"],
            f"{r['tokens_in'] or 0}+{r['tokens_out'] or 0}",
            f"{r['latency_ms'] or 0}ms",
            datetime.fromisoformat(r["created_at"]).strftime("%m-%d %H:%M"),
        )
        for r in rows
    ]


async def _load_run_trace(run_id: str, project_slug: str) -> dict[str, Any]:
    """runs show/replay 共用：按项目隔离装配只读轨迹（非法/跨项目 run_id → NotFound）。"""
    async with _app_service() as service:
        return await service.run_trace(project_slug, run_id)


async def _runs_show(run_id: str, project_slug: str) -> dict[str, Any]:
    trace = await _load_run_trace(run_id, project_slug)
    payload = dict(trace["run"])
    payload["steps"] = [
        {key: step[key] for key in ("seq", "node", "attempt", "status", "latency_ms", "error")}
        for step in trace["steps"]
    ]
    return payload


async def _runs_replay(run_id: str, project_slug: str) -> dict[str, Any]:
    return await _load_run_trace(run_id, project_slug)


async def _run_ingest(directory: Path, project_slug: str) -> IngestReport:
    async with _app_service() as service:
        return await service.ingest(project_slug, directory)
