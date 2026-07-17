"""devkb 命令行入口（规格 §4：ingest / ask / runs，P0 唯一 API）。"""

from __future__ import annotations

import asyncio
import json
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
    json_output: bool = typer.Option(False, "--json", help="输出完整 Answer JSON"),
) -> None:
    """向项目知识库提问，输出带引用的中文回答。"""
    try:
        answer = asyncio.run(_run_ask(question, project, top_k))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc

    if json_output:
        console.print_json(json.dumps(answer, ensure_ascii=False))
        return
    console.print(answer["answer_text"])
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
    """查看单个 run 的完整 Answer JSON 与元数据。"""
    try:
        payload = asyncio.run(_runs_show(run_id, project))
    except DevKbError as exc:
        console.print(f"[red]{exc.code}[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


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


async def _run_ask(question: str, project_slug: str, top_k: int | None) -> dict[str, Any]:
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
            return await answer_question(
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


async def _runs_show(run_id: str, project_slug: str) -> dict[str, Any]:
    import uuid as uuid_mod

    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory
    from devkb.repositories import RunRepo

    settings = get_settings()
    try:
        run_uuid = uuid_mod.UUID(run_id)
    except ValueError as exc:
        raise NotFoundError(f"非法 run_id：{run_id}") from exc
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            project = await _resolve_project(session, project_slug)
            run = await RunRepo(session, project.id).get(run_uuid)
            if run is None:
                raise NotFoundError(f"run {run_id} 不存在于项目 '{project_slug}'")
            return {
                "run_id": str(run.id),
                "question": run.question,
                "status": run.status,
                "model": run.model,
                "tokens_in": run.tokens_in,
                "tokens_out": run.tokens_out,
                "usage": run.usage,
                "cost": run.cost,
                "latency_ms": run.latency_ms,
                "created_at": run.created_at,
                "answer": run.answer,
            }
    finally:
        await engine.dispose()


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
