"""SentenceTransformerEmbedder 真模型冒烟（手动跑，不进 CI）。

用法：unset 代理后 `uv run python benchmarks/embedder_smoke.py`
验证：ADR-0002 冻结模型可加载（fp16/seq≤1024）、文档与查询嵌入维度=1024、
归一化成立、query prompt 生效（同文本 query/doc 向量应不同）。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

from devkb.config import get_settings
from devkb.embedding import SentenceTransformerEmbedder
from devkb.ingest.markdown import approx_token_counter, chunk_markdown


def main() -> None:
    settings = get_settings()
    print(f"model={settings.embedding_model_id} device={settings.embedding_device}")

    t0 = time.perf_counter()
    embedder = SentenceTransformerEmbedder(
        settings.embedding_model_id,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
    )
    print(f"load: {time.perf_counter() - t0:.1f}s")

    corpus_dir = Path(__file__).parents[1] / "tests" / "fixtures" / "corpus_md"
    texts = [
        c.content
        for f in sorted(corpus_dir.glob("*.md"))
        for c in chunk_markdown(f.read_text(encoding="utf-8"), count_tokens=approx_token_counter)
    ]
    t0 = time.perf_counter()
    doc_vecs = embedder.embed_documents(texts)
    dt = time.perf_counter() - t0
    print(f"docs: {len(texts)} chunks -> {len(doc_vecs)}x{len(doc_vecs[0])} in {dt:.2f}s")

    query = "库存扣减的并发控制是怎么做的？"
    q_vec = embedder.embed_query(query)
    norm = math.sqrt(sum(x * x for x in q_vec))
    print(f"query dim={len(q_vec)} norm={norm:.4f}")

    plain = embedder.embed_documents([query])[0]
    cos = sum(a * b for a, b in zip(q_vec, plain, strict=True))
    print(f"query-prompt 生效检查：cos(query 向量, doc 向量)={cos:.4f}（应 <1，同文本不同 prompt）")

    best = max(
        range(len(texts)), key=lambda i: sum(a * b for a, b in zip(q_vec, doc_vecs[i], strict=True))
    )
    print(f"top1 命中 chunk 前 60 字：{texts[best][:60]!r}")


if __name__ == "__main__":
    main()
