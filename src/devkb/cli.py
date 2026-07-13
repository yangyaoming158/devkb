"""devkb 命令行入口。P0 契约：ingest / ask / runs（T6/T9 实现），当前为骨架。"""

from __future__ import annotations

import typer

from devkb import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False, help="devkb — 软件项目知识助手")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", help="打印版本号并退出"),
) -> None:
    if version:
        typer.echo(f"devkb {__version__}")
        raise typer.Exit()
