"""Embedding 接缝（规格 §3：Protocol 只允许出现在本文件与 llm.py）。

- SentenceTransformerEmbedder：ADR-0002 冻结模型的生产实现（ml 依赖组，懒加载）。
  强制约束：fp16、max_seq_length ≤ 1024、批量 ≤ 基准值 32、查询侧 prompt_name="query"、
  CUDA OOM 按减半退避（batch 64 实测显存溢出致吞吐悬崖）。
- FakeEmbedder：确定性哈希向量——测机制不测模型质量（CI 禁真模型）。
  同一文本的 query/document 向量一致，便于集成测试做精确命中断言。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from typing import Protocol

from devkb.errors import EmbeddingError
from devkb.models import EMBEDDING_DIM

MAX_SEQ_LENGTH = 1024  # ADR-0002：超长截断上限


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    def _vector(self, text: str) -> list[float]:
        rnd = random.Random(hashlib.sha256(text.encode("utf-8")).digest())
        raw = [rnd.uniform(-1.0, 1.0) for _ in range(self._dim)]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_id: str,
        *,
        device: str = "cuda",
        batch_size: int = 32,
    ) -> None:
        # ml 依赖组，函数内 import 保证 CI（不装该组）可导入本模块
        import torch
        from sentence_transformers import SentenceTransformer

        self._torch = torch
        model_kwargs = {"torch_dtype": torch.float16} if device == "cuda" else {}
        self._model = SentenceTransformer(model_id, device=device, model_kwargs=model_kwargs)
        current_seq = self._model.max_seq_length
        if current_seq is None or current_seq > MAX_SEQ_LENGTH:
            self._model.max_seq_length = MAX_SEQ_LENGTH
        self._batch_size = batch_size

    def _encode(self, texts: Sequence[str], prompt_name: str | None = None) -> list[list[float]]:
        batch = self._batch_size
        while True:
            try:
                vectors = self._model.encode(
                    list(texts),
                    batch_size=batch,
                    normalize_embeddings=True,
                    prompt_name=prompt_name,
                )
                return vectors.tolist()
            except self._torch.cuda.OutOfMemoryError as exc:
                self._torch.cuda.empty_cache()
                if batch <= 1:
                    raise EmbeddingError("batch=1 仍显存不足") from exc
                batch //= 2  # ADR-0002：OOM 减半退避
            except Exception as exc:
                raise EmbeddingError(f"嵌入失败：{type(exc).__name__}: {exc}") from exc

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        # ADR-0002：Qwen3 查询侧必须带 query prompt，否则检索质量显著劣化
        return self._encode([text], "query")[0]
