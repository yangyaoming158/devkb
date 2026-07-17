"""T13.3 语料视图工具：include/exclude 语义、字节不变、manifest 自检、哨兵拦截。

工具是标准库脚本，用 subprocess 走真实命令行入口（与实际使用一致）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
TOOL = ROOT / "tools" / "build_corpus_view.py"
MANIFEST_NAME = ".devkb-corpus-manifest.json"


def _build_source_repo(root: Path) -> None:
    files = {
        "docs/a.md": "# A\n中文内容",
        "docs/sub/b.md": "# B",
        "README.md": "# 根 readme",
        "README.zh-CN.md": "# 中文 readme",
        "notes.md": "根级非 README 的 md：不进视图",
        "sub/README.md": "非根级 README：不进视图",
        "src/App.java": "package demo; class App {}",
        "src/main/resources/application.yml": "server:\n  port: 8080\n",
        "config/app.properties": "key=${SECRET}\n",
        "cfg/x.yaml": "a: 1\n",
        "node_modules/pkg/Evil.java": "class Evil {}",
        "target/Gen.java": "class Gen {}",
        "build/out.yml": "generated: true\n",
        "dist/app.properties": "x=1\n",
        ".github/workflows/ci.yml": "on: push\n",
        "CLAUDE.md": "# 指令文件",
        "docs/AGENTS.md": "# 指令文件",
        "deep/nested/TASKMASTER.md": "# 指令文件",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        check=True,
    )


EXPECTED_VIEW_FILES = {
    "docs/a.md": "markdown",
    "docs/sub/b.md": "markdown",
    "README.md": "markdown",
    "README.zh-CN.md": "markdown",
    "src/App.java": "java",
    "src/main/resources/application.yml": "config",
    "config/app.properties": "config",
    "cfg/x.yaml": "config",
}


def _write_config(path: Path, source: Path, *, total: int = 8) -> None:
    path.write_text(
        f'''
version = "view-test-v1"
source = "{source}"

[[include]]
type = "markdown"
patterns = ["docs/**/*.md", "README*.md"]

[[include]]
type = "java"
patterns = ["**/*.java"]

[[include]]
type = "config"
patterns = ["**/*.yml", "**/*.yaml", "**/*.properties"]

[exclude]
hidden = true
dir_names = ["node_modules", "target", "build", "dist"]
file_names = ["CLAUDE.md", "AGENTS.md", "TASKMASTER.md"]

[expected]
markdown = 4
java = 1
config = 3
total = {total}
''',
        encoding="utf-8",
    )


def _run_tool(config: Path, dest_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--config", str(config), "--dest-root", str(dest_root)],
        capture_output=True,
        text=True,
    )


def test_view_matches_spec_semantics(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _build_source_repo(source)
    config = tmp_path / "view.toml"
    _write_config(config, source)
    dest_root = tmp_path / "views"

    result = _run_tool(config, dest_root)
    assert result.returncode == 0, result.stderr + result.stdout

    view_dirs = list(dest_root.iterdir())
    assert len(view_dirs) == 1
    view = view_dirs[0]
    manifest = json.loads((view / MANIFEST_NAME).read_text(encoding="utf-8"))

    assert {f["rel_path"]: f["type"] for f in manifest["files"]} == EXPECTED_VIEW_FILES
    assert manifest["total"] == 8
    assert manifest["counts"] == {"markdown": 4, "java": 1, "config": 3}
    assert manifest["source_commit"] and manifest["source_worktree_dirty"] is False

    # 相对路径与字节不变；manifest 之外不夹带任何文件
    on_disk = {
        p.relative_to(view).as_posix()
        for p in view.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME
    }
    assert on_disk == set(EXPECTED_VIEW_FILES)
    for rel in EXPECTED_VIEW_FILES:
        assert (view / rel).read_bytes() == (source / rel).read_bytes()
        digest = hashlib.sha256((view / rel).read_bytes()).hexdigest()
        assert digest == next(f["sha256"] for f in manifest["files"] if f["rel_path"] == rel)
    assert not any(p.is_symlink() for p in view.rglob("*")), "视图禁 symlink"

    # 排除项确实不存在：指令文件、隐藏、依赖/生成物、非根 README、根级非 README md
    for rel in [
        "CLAUDE.md",
        "docs/AGENTS.md",
        "deep/nested/TASKMASTER.md",
        ".github/workflows/ci.yml",
        "node_modules/pkg/Evil.java",
        "target/Gen.java",
        "build/out.yml",
        "dist/app.properties",
        "sub/README.md",
        "notes.md",
    ]:
        assert not (view / rel).exists(), f"{rel} 不得进入视图"

    # 配置占位符原样保留（不解析不展开）
    assert (view / "config/app.properties").read_text(encoding="utf-8") == "key=${SECRET}\n"


def test_second_build_creates_fresh_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _build_source_repo(source)
    config = tmp_path / "view.toml"
    _write_config(config, source)
    dest_root = tmp_path / "views"

    assert _run_tool(config, dest_root).returncode == 0
    assert _run_tool(config, dest_root).returncode == 0
    assert len(list(dest_root.iterdir())) == 2, "每次构建都是全新目录"


def test_expected_sentinel_blocks_scope_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _build_source_repo(source)
    config = tmp_path / "view.toml"
    _write_config(config, source, total=445)  # 故意错误的哨兵
    dest_root = tmp_path / "views"

    result = _run_tool(config, dest_root)
    assert result.returncode == 1
    assert "数量哨兵不符" in result.stderr
