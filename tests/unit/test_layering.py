"""D7 分层静态扫描（T4.5）：sqlalchemy 查询构造只允许出现在 repositories.py。

CLAUDE.md 硬规则的机器强制版本：models.py/db.py 允许导入 DDL 类型与引擎，
但任何文件（repositories.py 除外）导入 select/insert/update/delete/text 等
查询构造即 fail。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src" / "devkb"
ALLOWED_FILES = {"repositories.py"}
QUERY_CONSTRUCTS = {
    "select",
    "insert",
    "update",
    "delete",
    "text",
    "union",
    "exists",
    "lambda_stmt",
}


def test_query_constructs_only_in_repositories() -> None:
    violations: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name in ALLOWED_FILES:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] == "sqlalchemy"
            ):
                bad = {alias.name for alias in node.names} & QUERY_CONSTRUCTS
                if bad:
                    violations.append(f"{py.relative_to(SRC)}:{node.lineno} 导入了查询构造 {bad}")
    assert not violations, "D7 违规——查询构造只允许在 repositories.py：\n" + "\n".join(violations)
