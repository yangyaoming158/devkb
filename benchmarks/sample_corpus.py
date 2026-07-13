"""T1.2 基准语料抽样：从 mini-mall-order 抽取 md 章节块 + Java 代码片段。

纯标准库、确定性输出（排序遍历、无随机）。
范围与 P0 摄取范围一致：docs/**/*.md + 根级 README*.md；Java 片段作为检索干扰项
（对应《模型与环境基准方案》第 3 节的"文档+Java 代码片段"）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path("/home/oslab/projects/mini-mall-order")
OUT = Path(__file__).parent / "sample_corpus.jsonl"

MAX_SECTION_CHARS = 1600  # 超过则按段落切分（兼容 512 token 上限的候选模型）
SPLIT_TARGET_CHARS = 1300
MAX_MD_CHUNKS = 350
MAX_JAVA_CHUNKS = 150
JAVA_WINDOW_LINES = 50

HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)")


def md_files() -> list[Path]:
    files = sorted(REPO.glob("docs/**/*.md")) + sorted(REPO.glob("README*.md"))
    return [f for f in files if f.is_file()]


def split_long(text: str) -> list[str]:
    if len(text) <= MAX_SECTION_CHARS:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > SPLIT_TARGET_CHARS and buf:
            parts.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts


def chunk_markdown(path: Path) -> list[dict]:
    rel = str(path.relative_to(REPO))
    heading = "(文首)"
    sections: list[tuple[str, list[str]]] = [(heading, [])]
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = HEADING_RE.match(line)
        if m:
            heading = m.group(2).strip()
            sections.append((heading, []))
        else:
            sections[-1][1].append(line)
    chunks = []
    for head, lines in sections:
        text = "\n".join(lines).strip()
        if len(text) < 40:  # 跳过空节/纯标题节
            continue
        for i, piece in enumerate(split_long(text)):
            chunks.append(
                {
                    "kind": "md",
                    "rel_path": rel,
                    "heading": head,
                    "text": f"{head}\n\n{piece}",
                }
            )
    return chunks


def java_snippets() -> list[dict]:
    files = sorted(
        p
        for p in REPO.glob("**/*.java")
        if "/target/" not in str(p) and "/node_modules/" not in str(p)
    )
    # 均匀抽样文件，每文件取前一个窗口，直到达到上限
    step = max(1, len(files) // MAX_JAVA_CHUNKS)
    out = []
    for f in files[::step][:MAX_JAVA_CHUNKS]:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        window = "\n".join(lines[:JAVA_WINDOW_LINES]).strip()
        if len(window) < 80:
            continue
        rel = str(f.relative_to(REPO))
        out.append({"kind": "java", "rel_path": rel, "heading": f.name, "text": window})
    return out


def load_anchors() -> list[dict]:
    anchors = []
    eval_dir = Path(__file__).parent.parent / "evalsets" / "v0"
    for name in ("retrieval_dev.jsonl", "retrieval_holdout.jsonl"):
        for line in (eval_dir / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                anchors.extend(json.loads(line).get("relevant", []))
    return anchors


def matches_anchor(chunk: dict, anchors: list[dict]) -> bool:
    return any(
        chunk["rel_path"] == a["rel_path"]
        and (
            a["anchor"].lower() in chunk["heading"].lower()
            or chunk["heading"].lower() in a["anchor"].lower()
        )
        for a in anchors
    )


def main() -> None:
    md = []
    for f in md_files():
        md.extend(chunk_markdown(f))
    # 评测锚点所在块必须保留（否则 Recall 天花板塌陷——2026-07-12 实测教训），只对其余块降采样
    anchors = load_anchors()
    must_keep = [c for c in md if matches_anchor(c, anchors)]
    rest = [c for c in md if not matches_anchor(c, anchors)]
    budget = MAX_MD_CHUNKS - len(must_keep)
    if len(rest) > budget:
        step = len(rest) / budget
        rest = [rest[int(i * step)] for i in range(budget)]
    md = must_keep + rest
    print(f"锚点保留块={len(must_keep)}")
    java = java_snippets()
    chunks = md + java
    for i, c in enumerate(chunks):
        c["id"] = f"c{i:04d}"
    with OUT.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"md_chunks={len(md)} java_chunks={len(java)} total={len(chunks)} -> {OUT}")
    if not (300 <= len(chunks) <= 520):
        print("WARN: 总块数不在 300–500 目标区间", file=sys.stderr)


if __name__ == "__main__":
    main()
