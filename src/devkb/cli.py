"""devkb 命令行入口。P0 契约：ingest / ask / runs（ask/runs 于 T9 实现）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from devkb import __version__
from devkb.errors import DevKbError
from devkb.ingest.pipeline import IngestReport

app = typer.Typer(no_args_is_help=True, add_completion=False, help="devkb — 软件项目知识助手")
console = Console()


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", help="打印版本号并退出"),
) -> None:
    if version:
        typer.echo(f"devkb {__version__}")
        raise typer.Exit()


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


async def _run_ingest(directory: Path, project_slug: str) -> IngestReport:
    from devkb.config import get_settings
    from devkb.db import create_engine, create_session_factory
    from devkb.ingest.markdown import qwen_token_counter
    from devkb.ingest.pipeline import ingest_directory
    from devkb.repositories import ProjectRepo

    settings = get_settings()
    # 生产计数器 = ADR-0002 冻结模型 tokenizer（ml 依赖组，函数内懒加载）
    count_tokens = qwen_token_counter(settings.embedding_model_id)

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
                count_tokens=count_tokens,
                target_tokens=settings.chunk_target_tokens,
            )
    finally:
        await engine.dispose()
