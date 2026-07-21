"""快照 B（真实 Qwen tokenizer）：锁真实 token_count 与该模式下的 chunk 边界。

CI 不装 ml 依赖组，`importorskip("transformers")` 使本文件在 CI 自动跳过
（测试纪律：CI 禁真模型；tokenizer 本地缓存由 T1 基准下载）。
"""

from __future__ import annotations

import pytest

from conftest import CORPUS_MD_DIR, FIXTURES_DIR, GoldenCheck, chunks_to_jsonable

pytest.importorskip("transformers")

from devkb.ingest.markdown import chunk_markdown, qwen_token_counter

GOLDEN_ML_DIR = FIXTURES_DIR / "golden_ml"
FIXTURE_NAMES = ["zh", "en", "long_section", "table", "fenced"]

# ADR-0002 冻结模型；tokenizer 加载仅本文件内发生（CI 已跳过）
QWEN_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


@pytest.fixture(scope="module")
def count_tokens():
    return qwen_token_counter(QWEN_MODEL_ID)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_golden_snapshot_b(name: str, count_tokens, golden_check: GoldenCheck) -> None:
    source = (CORPUS_MD_DIR / f"{name}.md").read_text(encoding="utf-8")
    chunks = chunk_markdown(source, count_tokens=count_tokens)
    assert chunks
    golden_check(GOLDEN_ML_DIR / f"{name}.json", chunks_to_jsonable(chunks))


def test_fence_never_split_real_tokenizer(count_tokens) -> None:
    source = (CORPUS_MD_DIR / "fenced.md").read_text(encoding="utf-8")
    chunks = chunk_markdown(source, count_tokens=count_tokens)
    fence_chunks = [c for c in chunks if "```java" in c.content]
    assert len(fence_chunks) == 1
    assert fence_chunks[0].content.count("```") == 2
