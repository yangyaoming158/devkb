from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/workflow_guard.py"


def load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("workflow_guard", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard() -> ModuleType:
    return load_guard()


def valid_metadata() -> dict[str, object]:
    return {
        "task_id": "T100",
        "status": "approved",
        "risk": "medium",
        "base_sha": "a" * 40,
        "max_changed_files": 8,
        "allowed_paths": ["src/devkb/service.py", "tests/unit/"],
        "allow_dependency_changes": False,
        "allow_migration_changes": False,
        "allow_golden_changes": False,
        "size_exception": "",
    }


def test_valid_packet_metadata_passes(guard: ModuleType) -> None:
    assert guard.validate_packet_metadata(valid_metadata()) == []


def test_packet_rejects_missing_key_and_unexplained_large_budget(guard: ModuleType) -> None:
    metadata = valid_metadata()
    metadata.pop("risk")
    metadata["max_changed_files"] = 9

    errors = guard.validate_packet_metadata(metadata)

    assert any("risk" in error for error in errors)

    metadata["risk"] = "medium"
    errors = guard.validate_packet_metadata(metadata)
    assert any("size_exception" in error for error in errors)


def test_packet_reports_wrong_scalar_types_without_crashing(guard: ModuleType) -> None:
    metadata = valid_metadata()
    metadata["status"] = []
    metadata["risk"] = 42

    errors = guard.validate_packet_metadata(metadata)

    assert any("status" in error for error in errors)
    assert any("risk" in error for error in errors)


def test_path_scope_distinguishes_exact_file_and_directory(guard: ModuleType) -> None:
    allowed = ["src/devkb/service.py", "tests/unit/"]

    assert guard.path_is_allowed("src/devkb/service.py", allowed)
    assert guard.path_is_allowed("tests/unit/test_service.py", allowed)
    assert not guard.path_is_allowed("src/devkb/service.py.bak", allowed)
    assert not guard.path_is_allowed("tests/integration/test_service.py", allowed)


def test_scope_rejects_outside_and_unauthorized_dependency(guard: ModuleType) -> None:
    metadata = valid_metadata()
    files = ["src/devkb/service.py", "README.md", "pyproject.toml"]

    errors = guard.validate_scope(metadata, files)

    assert any("README.md" in error for error in errors)
    assert any("pyproject.toml" in error and "依赖" in error for error in errors)


def test_python_policy_detects_debug_skip_and_testing_branch(guard: ModuleType) -> None:
    source = """
import pytest

@pytest.mark.xfail
def test_bad():
    if testing:
        breakpoint()
    pytest.skip("not ready")
"""

    findings, _ = guard.inspect_python(source)

    kinds = {finding.kind for finding in findings}
    assert "breakpoint()" in kinds
    assert "if testing" in kinds
    assert "pytest.skip()" in kinds
    assert "pytest.mark.xfail" in kinds


def test_python_policy_ignores_policy_names_inside_strings(guard: ModuleType) -> None:
    source = """
POLICIES = ["pytest.skip()", "breakpoint()", "if testing"]

def test_documentation():
    assert len(POLICIES) == 3
"""

    findings, assertions = guard.inspect_python(source)

    assert findings == []
    assert assertions == 1


def test_todo_scan_checks_comments_not_string_literals(guard: ModuleType) -> None:
    assert not guard.line_has_todo_comment("src/service.py", "value = 'TODO'")
    assert guard.line_has_todo_comment("src/service.py", "value = 1  # TODO: explain")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".env", True),
        (".env.production", True),
        (".env.example", False),
        ("certs/private.pem", True),
        ("src/devkb/service.py", False),
    ],
)
def test_sensitive_path_policy(guard: ModuleType, path: str, expected: bool) -> None:
    errors = guard.validate_sensitive_paths([path])
    assert bool(errors) is expected
