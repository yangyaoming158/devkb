"""D7 分层静态扫描（T4.5）：sqlalchemy 查询构造只允许出现在 repositories.py。

CLAUDE.md 硬规则的机器强制版本：models.py/db.py 允许导入 DDL 类型与引擎，
但任何文件（repositories.py 除外）导入 select/insert/update/delete/text 等
查询构造即 fail。扫描范围含 benchmarks/（T14 复评发现诊断脚本曾在此自建
SQL 绕过检查）；tests/ 允许少量诊断性原生 SQL（如模拟迁移前存量数据），
不在扫描范围。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCAN_DIRS = [ROOT / "src" / "devkb", ROOT / "benchmarks"]
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
    for scan_dir in SCAN_DIRS:
        for py in sorted(scan_dir.rglob("*.py")):
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
                        violations.append(
                            f"{py.relative_to(ROOT)}:{node.lineno} 导入了查询构造 {bad}"
                        )
    assert not violations, "D7 违规——查询构造只允许在 repositories.py：\n" + "\n".join(violations)
