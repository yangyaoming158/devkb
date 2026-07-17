"""构建可复现的摄取语料视图（《P1实现规格》§6.2，T13.3）。

从只读源仓库把 include 命中且未被 exclude 的文件复制到 /tmp 下的全新目录，
保持相对路径与原始字节；生成隐藏 manifest（不参与摄取）记录源 commit、
每个 rel_path、sha256、类型与总数。只用标准库；不创建 symlink、不写源仓库。

用法：
    uv run python tools/build_corpus_view.py \
        --config corpora/mini-mall-p1.toml [--source DIR] [--dest-root /tmp/devkb-corpus]

构建后强制自检：指令文件/隐藏路径/依赖与生成物目录不得出现在 manifest；
逐文件复制字节 sha256 与源一致；数量与 expected 哨兵一致，否则非零退出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = ".devkb-corpus-manifest.json"


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """最小 glob：`**` 跨层级、`*`/`?` 不跨 `/`；整串锚定。"""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 3] == "**/":
                out.append(r"(?:[^/]+/)*")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif ch == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class ViewConfig:
    version: str
    source: Path
    includes: list[tuple[str, list[re.Pattern[str]]]]  # (type, patterns)
    exclude_hidden: bool
    exclude_dir_names: frozenset[str]
    exclude_file_names: frozenset[str]
    expected: dict[str, int]


def load_config(config_path: Path) -> ViewConfig:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    includes = [
        (item["type"], [glob_to_regex(p) for p in item["patterns"]]) for item in data["include"]
    ]
    exclude = data["exclude"]
    return ViewConfig(
        version=data["version"],
        source=Path(data["source"]),
        includes=includes,
        exclude_hidden=bool(exclude.get("hidden", True)),
        exclude_dir_names=frozenset(exclude.get("dir_names", [])),
        exclude_file_names=frozenset(exclude.get("file_names", [])),
        expected={k: int(v) for k, v in data.get("expected", {}).items()},
    )


def classify(rel_posix: str, cfg: ViewConfig) -> str | None:
    for file_type, patterns in cfg.includes:
        if any(p.match(rel_posix) for p in patterns):
            return file_type
    return None


def is_excluded(rel_parts: tuple[str, ...], cfg: ViewConfig) -> bool:
    if cfg.exclude_hidden and any(part.startswith(".") for part in rel_parts):
        return True
    if any(part in cfg.exclude_dir_names for part in rel_parts[:-1]):
        return True
    return rel_parts[-1] in cfg.exclude_file_names


def git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def collect(cfg: ViewConfig) -> list[tuple[Path, str, str]]:
    """返回 (源路径, rel_posix, type)，确定性排序；symlink 一律跳过并告警。"""
    selected: list[tuple[Path, str, str]] = []
    for path in sorted(cfg.source.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(cfg.source)
        rel_posix = rel.as_posix()
        if is_excluded(rel.parts, cfg):
            continue
        file_type = classify(rel_posix, cfg)
        if file_type is None:
            continue
        if path.is_symlink():
            print(f"warn: 跳过 symlink（视图禁止）：{rel_posix}", file=sys.stderr)
            continue
        selected.append((path, rel_posix, file_type))
    return selected


def build_view(cfg: ViewConfig, dest_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    view_dir = dest_root / f"{cfg.version}-{stamp}-{uuid.uuid4().hex[:6]}"
    view_dir.mkdir(parents=True, exist_ok=False)  # 全新目录，绝不复用

    files = collect(cfg)
    entries: list[dict[str, object]] = []
    for src, rel_posix, file_type in files:
        dest = view_dir / rel_posix
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        source_digest = hashlib.sha256(src.read_bytes()).hexdigest()
        copied_digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        if source_digest != copied_digest:
            raise SystemExit(f"复制后字节不一致：{rel_posix}")
        entries.append(
            {
                "rel_path": rel_posix,
                "type": file_type,
                "sha256": source_digest,
                "bytes": dest.stat().st_size,
            }
        )

    counts: dict[str, int] = {}
    for entry in entries:
        counts[str(entry["type"])] = counts.get(str(entry["type"]), 0) + 1
    manifest = {
        "version": cfg.version,
        "source": str(cfg.source),
        "source_commit": git_value(cfg.source, "rev-parse", "HEAD"),
        "source_worktree_dirty": bool(git_value(cfg.source, "status", "--porcelain")),
        "built_at": stamp,
        "total": len(entries),
        "counts": counts,
        "files": entries,
    }
    (view_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return view_dir


def verify(view_dir: Path, cfg: ViewConfig) -> list[str]:
    """构建后强制自检；返回问题列表（空 = 通过）。"""
    manifest = json.loads((view_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    problems: list[str] = []
    for entry in manifest["files"]:
        parts = tuple(str(entry["rel_path"]).split("/"))
        if any(part.startswith(".") for part in parts):
            problems.append(f"隐藏路径混入：{entry['rel_path']}")
        if any(part in ("node_modules", "target", "build", "dist") for part in parts):
            problems.append(f"依赖/生成物目录混入：{entry['rel_path']}")
        if parts[-1] in ("CLAUDE.md", "AGENTS.md", "TASKMASTER.md"):
            problems.append(f"指令文件混入：{entry['rel_path']}")
    for key, expected_count in cfg.expected.items():
        actual = manifest["total"] if key == "total" else manifest["counts"].get(key, 0)
        if actual != expected_count:
            problems.append(
                f"数量哨兵不符：{key} 期望 {expected_count} 实际 {actual}"
                "（源仓库范围变化须显式更新 corpora 定义并复核）"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("corpora/mini-mall-p1.toml"))
    parser.add_argument("--source", type=Path, default=None, help="覆盖 toml 中的源仓库路径")
    parser.add_argument("--dest-root", type=Path, default=Path("/tmp/devkb-corpus"))
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.source is not None:
        cfg = ViewConfig(
            version=cfg.version,
            source=args.source,
            includes=cfg.includes,
            exclude_hidden=cfg.exclude_hidden,
            exclude_dir_names=cfg.exclude_dir_names,
            exclude_file_names=cfg.exclude_file_names,
            expected=cfg.expected,
        )

    view_dir = build_view(cfg, args.dest_root)
    problems = verify(view_dir, cfg)
    manifest = json.loads((view_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    print(f"视图目录：{view_dir}")
    print(f"源 commit：{manifest['source_commit']} dirty={manifest['source_worktree_dirty']}")
    print(f"文件数：{manifest['total']} 分类：{manifest['counts']}")
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 1
    print("自检通过：无指令文件/隐藏路径/依赖生成物混入，数量与哨兵一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
