"""devkb 命令行入口（规格 §4：ingest / ask / runs，P0 唯一 API）。"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from devkb import __version__
from devkb.errors import ConfigError, DevKbError, NotFoundError
from devkb.ingest.pipeline import IngestReport
from devkb.logging import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="devkb — 软件项目知识助手")
runs_app = typer.Typer(no_args_is_help=True, help="查看历史 run")
app.add_typer(runs_app, name="runs")
console = Console()


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


async def _resolve_project(session: Any, slug: str) -> Any:
    from devkb.repositories import ProjectRepo

    project = await ProjectRepo(session).get_by_slug(slug)
    if project is None:
        raise NotFoundError(f"项目 '{slug}' 不存在（先执行 devkb ingest）")
    return project


async def _run_backfill_search(project_slug: str) -> tuple[int, int]:
    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory
    from devkb.repositories import ChunkRepo

    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await _resolve_project(session, project_slug)
            result = await ChunkRepo(session, project.id).backfill_search_text()
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def _run_ask(
    question: str,
    project_slug: str,
    top_k: int | None,
    pipeline: str = "agentic",
) -> dict[str, Any]:
    from devkb.agent.service import agentic_answer_question
    from devkb.answer import answer_question
    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory
    from devkb.embedding import SentenceTransformerEmbedder
    from devkb.llm import OpenAICompatLLM

    settings = get_settings()
    embedder = SentenceTransformerEmbedder(
        settings.embedding_model_id,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    llm = OpenAICompatLLM(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await _resolve_project(session, project_slug)
            if pipeline == "fixed-rag":
                return await answer_question(
                    session,
                    project.id,
                    question,
                    embedder=embedder,
                    llm=llm,
                    top_k=top_k or settings.retrieval_top_k,
                )
            return await agentic_answer_question(
                session,
                project.id,
                question,
                embedder=embedder,
                llm=llm,
                top_k=top_k or settings.retrieval_top_k,
            )
    finally:
        await engine.dispose()


async def _runs_list(project_slug: str, limit: int) -> list[tuple[str, ...]]:
    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory
    from devkb.repositories import RunRepo

    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await _resolve_project(session, project_slug)
            runs = await RunRepo(session, project.id).list_recent(limit)
            return [
                (
                    str(r.id),
                    r.status,
                    (r.question[:40] + "…") if len(r.question) > 40 else r.question,
                    f"{r.tokens_in or 0}+{r.tokens_out or 0}",
                    f"{r.latency_ms or 0}ms",
                    r.created_at.strftime("%m-%d %H:%M"),
                )
                for r in runs
            ]
    finally:
        await engine.dispose()


async def _load_run_trace(run_id: str, project_slug: str) -> dict[str, Any]:
    """runs show/replay 共用：按项目隔离装配只读轨迹（非法/跨项目 run_id → NotFound）。"""
    import uuid as uuid_mod

    from devkb.agent.service import get_run_trace
    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory

    settings = get_settings()
    try:
        run_uuid = uuid_mod.UUID(run_id)
    except ValueError as exc:
        raise NotFoundError(f"非法 run_id：{run_id}") from exc
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await _resolve_project(session, project_slug)
            try:
                return await get_run_trace(session, project.id, run_uuid)
            except NotFoundError as exc:
                raise NotFoundError(f"run {run_id} 不存在于项目 '{project_slug}'") from exc
    finally:
        await engine.dispose()


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
    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory
    from devkb.embedding import SentenceTransformerEmbedder
    from devkb.ingest.markdown import qwen_token_counter
    from devkb.ingest.pipeline import ingest_directory
    from devkb.repositories import ProjectRepo

    settings = get_settings()
    # 生产计数器与 Embedder 均为 ADR-0002 冻结模型（ml 依赖组，懒加载）
    count_tokens = qwen_token_counter(settings.embedding_model_id)
    embedder = SentenceTransformerEmbedder(
        settings.embedding_model_id,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )

    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            repo = ProjectRepo(session)
            project = await repo.get_by_slug(project_slug)
            if project is None:
                project = await repo.create(slug=project_slug, name=project_slug)
                await session.commit()
            return await ingest_directory(
                session,
                project.id,
                directory,
                embedder=embedder,
                count_tokens=count_tokens,
                target_tokens=settings.chunk_target_tokens,
            )
    finally:
        await engine.dispose()
