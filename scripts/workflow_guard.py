#!/usr/bin/env python3
"""无第三方依赖的任务范围与工作流门禁。"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_STATUSES = {
    "draft",
    "plan-review",
    "approved",
    "implementing",
    "review",
    "repair",
    "accepted",
    "return-to-design",
}
IMPLEMENTATION_STATUSES = {"approved", "implementing", "review", "repair", "accepted"}
VALID_RISKS = {"low", "medium", "high"}
PROTECTED_BRANCHES = {"main", "master", "p0", "p1", "p1.5"}
REQUIRED_PACKET_KEYS = {
    "task_id",
    "status",
    "risk",
    "base_sha",
    "max_changed_files",
    "allowed_paths",
    "allow_dependency_changes",
    "allow_migration_changes",
    "allow_golden_changes",
    "size_exception",
}
REQUIRED_PACKET_SECTIONS = {
    "## 任务目标",
    "## 范围",
    "## 非目标",
    "## 仓库事实",
    "## 实现计划",
    "## 验收标准",
    "## Review Ledger",
    "## 最终验收",
}
DEPENDENCY_FILES = {
    "Pipfile",
    "Pipfile.lock",
    "package-lock.json",
    "package.json",
    "poetry.lock",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "uv.lock",
    "yarn.lock",
}
MIGRATION_PREFIXES = ("alembic/", "migrations/")
GOLDEN_PREFIXES = (
    "evalsets/reports/",
    "tests/fixtures/golden/",
    "tests/golden/",
    "tests/snapshots/",
)
REQUIRED_SKILLS = ("project-preimplementation-gate", "project-scoped-review")
HIGH_CONFIDENCE_SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
}
TODO_PATTERN = re.compile(r"\b(?:TODO|FIXME)\b")


class GuardError(RuntimeError):
    """表示门禁输入或仓库状态无效。"""


@dataclass(frozen=True)
class PythonFinding:
    kind: str
    line: int


def run_git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """在仓库根目录运行只读 Git 命令。"""

    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GuardError(f"git {' '.join(args)} 失败：{detail}")
    return result


def find_repo_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise GuardError("当前目录不在 Git 仓库中")
    return Path(result.stdout.strip()).resolve()


def parse_toml_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """读取以 +++ 包围的 TOML frontmatter。"""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardError(f"无法读取 {path}：{exc}") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise GuardError(f"{path} 缺少开头 TOML frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "+++")
    except StopIteration as exc:
        raise GuardError(f"{path} 的 TOML frontmatter 未闭合") from exc
    try:
        metadata = tomllib.loads("\n".join(lines[1:closing]))
    except tomllib.TOMLDecodeError as exc:
        raise GuardError(f"{path} 的 TOML frontmatter 无效：{exc}") from exc
    return metadata, text


def validate_allowed_path(value: str) -> str | None:
    if not value.strip():
        return "allowed_paths 不得包含空路径"
    if value.startswith("/") or "\\" in value:
        return f"allowed path 必须是 POSIX 仓库相对路径：{value!r}"
    parts = Path(value.rstrip("/")).parts
    if not parts or any(part in {".", ".."} for part in parts):
        return f"allowed path 不能是仓库根或包含 . / ..：{value!r}"
    return None


def validate_packet_metadata(
    metadata: dict[str, Any],
    *,
    allow_template_placeholders: bool = False,
) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_PACKET_KEYS - metadata.keys())
    if missing:
        errors.append(f"缺少 metadata：{', '.join(missing)}")
        return errors

    if not isinstance(metadata["task_id"], str) or not metadata["task_id"].strip():
        errors.append("task_id 必须是非空字符串")
    if not isinstance(metadata["status"], str) or metadata["status"] not in VALID_STATUSES:
        errors.append(f"status 无效：{metadata['status']!r}")
    if not isinstance(metadata["risk"], str) or metadata["risk"] not in VALID_RISKS:
        errors.append(f"risk 无效：{metadata['risk']!r}")

    base_sha = metadata["base_sha"]
    placeholder = allow_template_placeholders and base_sha == "REPLACE-WITH-40-CHAR-SHA"
    if not placeholder and (
        not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-fA-F]{40}", base_sha) is None
    ):
        errors.append("base_sha 必须是 40 位 commit SHA")

    max_files = metadata["max_changed_files"]
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files <= 0:
        errors.append("max_changed_files 必须是正整数")

    allowed_paths = metadata["allowed_paths"]
    if not isinstance(allowed_paths, list) or any(
        not isinstance(path, str) for path in allowed_paths
    ):
        errors.append("allowed_paths 必须是字符串数组")
    else:
        if not allowed_paths and not allow_template_placeholders:
            errors.append("allowed_paths 不得为空")
        errors.extend(
            error for path in allowed_paths if (error := validate_allowed_path(path)) is not None
        )

    for key in (
        "allow_dependency_changes",
        "allow_migration_changes",
        "allow_golden_changes",
    ):
        if not isinstance(metadata[key], bool):
            errors.append(f"{key} 必须是 boolean")

    size_exception = metadata["size_exception"]
    if not isinstance(size_exception, str):
        errors.append("size_exception 必须是字符串")
    elif isinstance(max_files, int) and max_files > 8 and not size_exception.strip():
        errors.append("max_changed_files 超过 8 时必须填写 size_exception")
    return errors


def validate_packet_document(
    path: Path,
    *,
    allow_template_placeholders: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    metadata, text = parse_toml_frontmatter(path)
    errors = validate_packet_metadata(
        metadata,
        allow_template_placeholders=allow_template_placeholders,
    )
    normalized = text.replace("## 仓库事实调查", "## 仓库事实")
    missing_sections = sorted(
        section for section in REQUIRED_PACKET_SECTIONS if section not in normalized
    )
    if missing_sections:
        errors.append(f"缺少 Task Packet 章节：{', '.join(missing_sections)}")
    return metadata, errors


def path_is_allowed(path: str, allowed_paths: list[str]) -> bool:
    for allowed in allowed_paths:
        if allowed.endswith("/"):
            if path.startswith(allowed):
                return True
        elif path == allowed:
            return True
    return False


def changed_files(root: Path, base_sha: str) -> list[str]:
    tracked = run_git(root, "diff", "--name-only", "-z", base_sha, "--").stdout.split("\0")
    untracked = run_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    return sorted({path for path in [*tracked, *untracked] if path})


def validate_base(root: Path, base_sha: str) -> list[str]:
    errors: list[str] = []
    exists = run_git(root, "cat-file", "-e", f"{base_sha}^{{commit}}", check=False)
    if exists.returncode:
        return [f"base_sha 不存在于本地仓库：{base_sha}"]
    ancestor = run_git(root, "merge-base", "--is-ancestor", base_sha, "HEAD", check=False)
    if ancestor.returncode:
        errors.append(f"base_sha 不是当前 HEAD 的祖先：{base_sha}")
    return errors


def validate_scope(metadata: dict[str, Any], files: list[str]) -> list[str]:
    errors: list[str] = []
    allowed_paths = metadata["allowed_paths"]
    outside = [path for path in files if not path_is_allowed(path, allowed_paths)]
    if outside:
        errors.append(f"发现计划外文件：{', '.join(outside)}")
    if len(files) > metadata["max_changed_files"]:
        errors.append(f"实际变更 {len(files)} 个文件，超过预算 {metadata['max_changed_files']} 个")

    if not metadata["allow_dependency_changes"]:
        changed_dependencies = sorted(DEPENDENCY_FILES.intersection(files))
        if changed_dependencies:
            errors.append(f"存在未授权依赖/锁文件变化：{', '.join(changed_dependencies)}")
    if not metadata["allow_migration_changes"]:
        migrations = [path for path in files if path.startswith(MIGRATION_PREFIXES)]
        if migrations:
            errors.append(f"存在未授权迁移变化：{', '.join(migrations)}")
    if not metadata["allow_golden_changes"]:
        golden = [path for path in files if path.startswith(GOLDEN_PREFIXES)]
        if golden:
            errors.append(f"存在未授权 golden/snapshot 变化：{', '.join(golden)}")
    return errors


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


class PythonPolicyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[PythonFinding] = []
        self.assertion_count = 0

    def visit_Assert(self, node: ast.Assert) -> None:
        self.assertion_count += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted_name(node.func)
        if name == "breakpoint":
            self.findings.append(PythonFinding("breakpoint()", node.lineno))
        elif name == "pdb.set_trace":
            self.findings.append(PythonFinding("pdb.set_trace()", node.lineno))
        elif name in {"pytest.skip", "pytest.xfail"}:
            self.findings.append(PythonFinding(f"{name}()", node.lineno))
        elif name and (
            name.startswith("unittest.skip")
            or name.startswith("pytest.mark.skip")
            or name.startswith("pytest.mark.xfail")
        ):
            self.findings.append(PythonFinding(name, node.lineno))
        if name and (name.startswith("self.assert") or name.startswith("cls.assert")):
            self.assertion_count += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = dotted_name(node)
        if name and (
            name.startswith("pytest.mark.skip")
            or name.startswith("pytest.mark.xfail")
            or name.startswith("unittest.skip")
        ):
            self.findings.append(PythonFinding(name, node.lineno))
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        names = {child.id.lower() for child in ast.walk(node.test) if isinstance(child, ast.Name)}
        if "testing" in names:
            self.findings.append(PythonFinding("if testing", node.lineno))
        self.generic_visit(node)


def inspect_python(source: str) -> tuple[list[PythonFinding], int]:
    tree = ast.parse(source)
    visitor = PythonPolicyVisitor()
    visitor.visit(tree)
    # decorator/call 同一节点可能由 Attribute 与 Call 各记录一次；只保留唯一位置。
    return sorted(set(visitor.findings), key=lambda item: (item.kind, item.line)), (
        visitor.assertion_count
    )


def read_base_file(root: Path, base_sha: str, path: str) -> str:
    result = run_git(root, "show", f"{base_sha}:{path}", check=False)
    return result.stdout if result.returncode == 0 else ""


def validate_python_changes(root: Path, base_sha: str, files: list[str]) -> list[str]:
    errors: list[str] = []
    for relative in files:
        if not relative.endswith(".py"):
            continue
        current_path = root / relative
        current_source = current_path.read_text(encoding="utf-8") if current_path.is_file() else ""
        base_source = read_base_file(root, base_sha, relative)
        try:
            current_findings, current_assertions = inspect_python(current_source)
        except SyntaxError as exc:
            errors.append(f"{relative}:{exc.lineno} Python 语法无效：{exc.msg}")
            continue
        try:
            base_findings, base_assertions = inspect_python(base_source)
        except SyntaxError:
            base_findings, base_assertions = [], 0

        base_counts = Counter(item.kind for item in base_findings)
        current_by_kind: dict[str, list[PythonFinding]] = {}
        for finding in current_findings:
            current_by_kind.setdefault(finding.kind, []).append(finding)
        for kind, findings in sorted(current_by_kind.items()):
            excess = len(findings) - base_counts[kind]
            for finding in findings[-max(excess, 0) :]:
                if excess > 0:
                    errors.append(f"{relative}:{finding.line} 新增禁止项：{kind}")

        if relative.startswith("tests/") and current_assertions < base_assertions:
            errors.append(
                f"{relative} 的断言数量从 {base_assertions} 降至 {current_assertions}；"
                "需恢复断言或回到计划审查说明测试语义"
            )
    return errors


def added_diff_lines(root: Path, base_sha: str) -> list[tuple[str, str]]:
    result = run_git(root, "diff", "--unified=0", "--no-color", base_sha, "--")
    current_path = ""
    added: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            added.append((current_path, line[1:]))

    for relative in run_git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
    ).stdout.splitlines():
        path = root / relative
        if path.is_file() and not path.is_symlink():
            try:
                added.extend(
                    (relative, line) for line in path.read_text(encoding="utf-8").splitlines()
                )
            except UnicodeDecodeError:
                continue
    return added


def validate_sensitive_paths(files: list[str]) -> list[str]:
    errors: list[str] = []
    for relative in files:
        name = Path(relative).name
        if (
            ((name == ".env" or name.startswith(".env.")) and name != ".env.example")
            or name in {"id_dsa", "id_ed25519", "id_rsa"}
            or Path(relative).suffix.lower() in {".pem", ".p12", ".pfx"}
        ):
            errors.append(f"疑似敏感文件不得进入 diff：{relative}")
    return errors


def line_has_todo_comment(relative: str, line: str) -> bool:
    if relative.endswith(".py"):
        try:
            tokens = tokenize.generate_tokens(io.StringIO(line.lstrip()).readline)
            return any(
                token.type == tokenize.COMMENT and TODO_PATTERN.search(token.string)
                for token in tokens
            )
        except (IndentationError, tokenize.TokenError):
            return False
    if relative.endswith((".js", ".ts", ".tsx", ".java")):
        return re.search(r"(?://|/\*|\*)[^\n]*\b(?:TODO|FIXME)\b", line) is not None
    return False


def validate_secrets_and_notes(
    root: Path,
    base_sha: str,
    files: list[str],
) -> tuple[list[str], list[str]]:
    errors = validate_sensitive_paths(files)
    warnings: list[str] = []
    for relative, line in added_diff_lines(root, base_sha):
        for label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS.items():
            if pattern.search(line):
                errors.append(f"{relative} 新增疑似 {label}")
        if line_has_todo_comment(relative, line):
            warnings.append(f"{relative} 新增 TODO/FIXME，请确认已写入任务或 backlog")
    return sorted(set(errors)), sorted(set(warnings))


def parse_skill_frontmatter(path: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {}, [f"无法读取 {path}：{exc}"]
    if not lines or lines[0].strip() != "---":
        return {}, [f"{path} 缺少 YAML frontmatter"]
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, [f"{path} 的 YAML frontmatter 未闭合"]
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        if ":" not in line:
            errors.append(f"{path} frontmatter 行无效：{line!r}")
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, errors


def validate_repo_layout(root: Path) -> list[str]:
    errors: list[str] = []
    required_files = (
        "AGENTS.md",
        "CLAUDE.md",
        "docs/development-workflow.md",
        "docs/templates/task-packet.md",
    )
    for relative in required_files:
        if not (root / relative).is_file():
            errors.append(f"缺少工作流文件：{relative}")

    template = root / "docs/templates/task-packet.md"
    if template.is_file():
        _, packet_errors = validate_packet_document(
            template,
            allow_template_placeholders=True,
        )
        errors.extend(f"{template.relative_to(root)}：{error}" for error in packet_errors)

    tasks_dir = root / "docs/tasks"
    if tasks_dir.is_dir():
        for packet in sorted(tasks_dir.glob("*.md")):
            _, packet_errors = validate_packet_document(packet)
            errors.extend(f"{packet.relative_to(root)}：{error}" for error in packet_errors)

    for name in REQUIRED_SKILLS:
        canonical = root / ".agents/skills" / name
        skill_file = canonical / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"缺少 canonical skill：{skill_file.relative_to(root)}")
            continue
        metadata, skill_errors = parse_skill_frontmatter(skill_file)
        errors.extend(skill_errors)
        if set(metadata) != {"name", "description"}:
            errors.append(
                f"{skill_file.relative_to(root)} frontmatter 只能包含 name 和 description"
            )
        if metadata.get("name") != name or not metadata.get("description"):
            errors.append(f"{skill_file.relative_to(root)} 的 name/description 无效")
        openai_yaml = canonical / "agents/openai.yaml"
        if not openai_yaml.is_file() or f"${name}" not in openai_yaml.read_text(encoding="utf-8"):
            errors.append(f"{openai_yaml.relative_to(root)} 缺失或 default_prompt 未引用 ${name}")

        claude_entry = root / ".claude/skills" / name
        if not claude_entry.is_symlink():
            errors.append(f"{claude_entry.relative_to(root)} 必须是 canonical skill 的 symlink")
        elif claude_entry.resolve() != canonical.resolve():
            errors.append(f"{claude_entry.relative_to(root)} 指向错误位置")

    workflow = root / "docs/development-workflow.md"
    if workflow.is_file():
        workflow_text = workflow.read_text(encoding="utf-8")
        for token in (
            "$project-preimplementation-gate",
            "$project-scoped-review",
            "PLAN_APPROVED",
            "TARGETED_FIX",
            "RETURN_TO_DESIGN",
        ):
            if token not in workflow_text:
                errors.append(f"docs/development-workflow.md 缺少关键 token：{token}")
    return errors


def workspace_facts(root: Path) -> tuple[str, str, str]:
    branch = run_git(root, "branch", "--show-current").stdout.strip()
    head = run_git(root, "rev-parse", "HEAD").stdout.strip()
    status = run_git(root, "status", "--short").stdout.rstrip()
    return branch, head, status


def print_facts(branch: str, head: str, status: str) -> None:
    print(f"branch: {branch or '(detached HEAD)'}")
    print(f"HEAD:   {head}")
    print("status:")
    print(status or "  clean")


def command_validate_repo(root: Path) -> tuple[list[str], list[str]]:
    return validate_repo_layout(root), []


def command_preflight(root: Path, packet_path: str | None) -> tuple[list[str], list[str]]:
    branch, head, status = workspace_facts(root)
    print_facts(branch, head, status)
    errors: list[str] = []
    warnings: list[str] = []
    if not branch:
        errors.append("不得在 detached HEAD 开始任务")
    elif branch in PROTECTED_BRANCHES:
        errors.append(f"不得直接在受保护阶段分支 {branch!r} 开始任务；请新建 task 分支")

    if packet_path is None:
        if status:
            errors.append("未提供 Task Packet 时工作区必须干净；请先隔离旧改动")
        return errors, warnings

    packet = (root / packet_path).resolve()
    try:
        packet.relative_to(root)
    except ValueError:
        errors.append("Task Packet 必须位于当前仓库")
        return errors, warnings
    metadata, packet_errors = validate_packet_document(packet)
    errors.extend(packet_errors)
    if packet_errors:
        return errors, warnings
    base_errors = validate_base(root, metadata["base_sha"])
    errors.extend(base_errors)
    if base_errors:
        return errors, warnings
    files = changed_files(root, metadata["base_sha"])
    errors.extend(validate_scope(metadata, files))
    print(f"base:   {metadata['base_sha']}")
    print(f"changed files: {len(files)}")
    for relative in files:
        print(f"  {relative}")
    return errors, warnings


def command_verify_task(
    root: Path,
    packet_path: str,
    *,
    require_clean: bool,
) -> tuple[list[str], list[str]]:
    packet = (root / packet_path).resolve()
    try:
        packet.relative_to(root)
    except ValueError:
        return ["Task Packet 必须位于当前仓库"], []
    metadata, errors = validate_packet_document(packet)
    warnings: list[str] = []
    if errors:
        return errors, warnings
    if metadata["status"] not in IMPLEMENTATION_STATUSES:
        errors.append(f"Task Packet 状态 {metadata['status']!r} 不允许进入实现/验收")
    base_errors = validate_base(root, metadata["base_sha"])
    errors.extend(base_errors)

    branch, head, status = workspace_facts(root)
    print_facts(branch, head, status)
    if not branch:
        errors.append("不得在 detached HEAD 验收任务")
    if require_clean and status:
        errors.append("最终验收要求干净的 exact HEAD")
    if base_errors:
        return errors, warnings

    files = changed_files(root, metadata["base_sha"])
    print(f"base:   {metadata['base_sha']}")
    print(f"changed files: {len(files)} / {metadata['max_changed_files']}")
    for relative in files:
        print(f"  {relative}")
    errors.extend(validate_scope(metadata, files))
    diff_check = run_git(root, "diff", "--check", metadata["base_sha"], "--", check=False)
    if diff_check.returncode:
        errors.append(f"git diff --check 失败：{diff_check.stdout.strip()}")
    errors.extend(validate_python_changes(root, metadata["base_sha"], files))
    secret_errors, note_warnings = validate_secrets_and_notes(
        root,
        metadata["base_sha"],
        files,
    )
    errors.extend(secret_errors)
    warnings.extend(note_warnings)
    return errors, warnings


def print_result(errors: list[str], warnings: list[str]) -> int:
    for warning in warnings:
        print(f"WARNING: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"FAIL: {len(errors)} 个阻塞问题", file=sys.stderr)
        return 1
    print("PASS")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-repo", help="校验工作流文件和 skill 接缝")

    preflight = subparsers.add_parser("preflight", help="检查分支、工作区和任务范围")
    preflight.add_argument("--packet", help="Task Packet 仓库相对路径")

    verify = subparsers.add_parser("verify-task", help="校验当前 task diff")
    verify.add_argument("--packet", required=True, help="Task Packet 仓库相对路径")
    verify.add_argument("--require-clean", action="store_true", help="要求工作区为 exact HEAD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = find_repo_root(Path.cwd())
        if args.command == "validate-repo":
            errors, warnings = command_validate_repo(root)
        elif args.command == "preflight":
            errors, warnings = command_preflight(root, args.packet)
        else:
            errors, warnings = command_verify_task(
                root,
                args.packet,
                require_clean=args.require_clean,
            )
    except (GuardError, OSError) as exc:
        return print_result([str(exc)], [])
    return print_result(errors, warnings)


if __name__ == "__main__":
    raise SystemExit(main())
