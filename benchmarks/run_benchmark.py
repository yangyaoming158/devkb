"""T1.3/T1.4 embedding 候选基准：资源占用、吞吐、延迟、Recall@5。

用法：
  .venv/bin/python benchmarks/run_benchmark.py --model BAAI/bge-m3 --device cuda

输出：benchmarks/results/<model名>_<device>.json
指标口径见《模型与环境基准方案》第 4 节；决策规则见第 5 节（本脚本只产数据不做决策）。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from pathlib import Path

HERE = Path(__file__).parent
CORPUS = HERE / "sample_corpus.jsonl"
QUERIES = HERE.parent / "evalsets" / "v0" / "retrieval_dev.jsonl"

# 各候选的查询/文档前缀约定（来源：各模型卡用法说明）
MODEL_CONVENTIONS: dict[str, dict] = {
    "BAAI/bge-m3": {"query_prefix": "", "doc_prefix": "", "prompt_name": None},
    "Qwen/Qwen3-Embedding-0.6B": {"query_prefix": "", "doc_prefix": "", "prompt_name": "query"},
    "intfloat/multilingual-e5-base": {
        "query_prefix": "query: ",
        "doc_prefix": "passage: ",
        "prompt_name": None,
    },
    "BAAI/bge-base-zh-v1.5": {
        "query_prefix": "为这个句子生成表示以用于检索相关文章：",
        "doc_prefix": "",
        "prompt_name": None,
    },
}


def rss_mb() -> float:
    text = Path("/proc/self/status").read_text()
    m = re.search(r"VmRSS:\s+(\d+) kB", text)
    return int(m.group(1)) / 1024 if m else -1.0


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", choices=["cuda", "cpu"], required=True)
    ap.add_argument("--skip-throughput", action="store_true", help="只跑质量测试（省时）")
    ap.add_argument(
        "--skip-recall",
        action="store_true",
        help="跳过全量编码与 Recall@5（CPU 轮次用：召回与 GPU 相同，只测资源/吞吐/延迟）",
    )
    ap.add_argument(
        "--recall-only",
        action="store_true",
        help="只重算 Recall@5 并合并进已有结果文件（语料修复后重测用，不覆盖吞吐/延迟）",
    )
    args = ap.parse_args()

    import torch
    from sentence_transformers import SentenceTransformer

    conv = MODEL_CONVENTIONS[args.model]
    corpus = load_jsonl(CORPUS)
    queries = [q for q in load_jsonl(QUERIES) if q.get("answerable")]

    result: dict = {
        "model": args.model,
        "device": args.device,
        "torch": torch.__version__,
        "cuda_available": torch.cuda.is_available(),
        "corpus_size": len(corpus),
        "n_queries": len(queries),
    }

    if args.device == "cuda":
        torch.cuda.reset_peak_memory_stats()

    # CUDA 用 fp16（8GB 卡上 bge-m3 fp32+长序列会 OOM，实测见 T1.3 记录）；CPU 保持 fp32
    model_kwargs = {"torch_dtype": torch.float16} if args.device == "cuda" else {}
    ram0 = rss_mb()
    t0 = time.perf_counter()
    model = SentenceTransformer(args.model, device=args.device, model_kwargs=model_kwargs)
    # 统一截断上限：P0 分块目标 300–500 token，1024 足够；也避免 8k/32k 上下文模型的注意力爆显存
    model.max_seq_length = min(model.max_seq_length or 512, 1024)
    result["load_time_s"] = round(time.perf_counter() - t0, 2)
    result["ram_delta_mb"] = round(rss_mb() - ram0, 1)
    result["embedding_dim"] = model.get_sentence_embedding_dimension()
    result["dtype"] = "fp16" if args.device == "cuda" else "fp32"
    result["max_seq_length"] = model.max_seq_length
    if args.device == "cuda":
        result["vram_after_load_mb"] = round(torch.cuda.memory_allocated() / 2**20, 1)

    doc_texts = [conv["doc_prefix"] + c["text"] for c in corpus]

    # --- 吞吐（batch 8/32/64，取前 200 块计时）---
    if not args.skip_throughput and not args.recall_only:
        sample = doc_texts[:200]
        result["throughput_chunks_per_s"] = {}
        for bs in (8, 32, 64):
            t0 = time.perf_counter()
            model.encode(sample, batch_size=bs, normalize_embeddings=True, show_progress_bar=False)
            dt = time.perf_counter() - t0
            result["throughput_chunks_per_s"][str(bs)] = round(len(sample) / dt, 1)

    # --- 全量语料编码（batch 32，供召回测试）---
    doc_emb = None
    if not args.skip_recall:
        t0 = time.perf_counter()
        doc_emb = model.encode(
            doc_texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False
        )
        result["full_corpus_encode_s"] = round(time.perf_counter() - t0, 1)
    if args.device == "cuda":
        result["vram_peak_mb"] = round(torch.cuda.max_memory_allocated() / 2**20, 1)

    # --- 单查询延迟 P50（20 次）---
    def encode_query(text: str):
        if conv["prompt_name"]:
            return model.encode([text], prompt_name=conv["prompt_name"], normalize_embeddings=True)
        return model.encode([conv["query_prefix"] + text], normalize_embeddings=True)

    if not args.recall_only:
        lat = []
        for _ in range(20):
            t0 = time.perf_counter()
            encode_query("订单创建的幂等是怎么实现的？")
            lat.append((time.perf_counter() - t0) * 1000)
        result["query_latency_p50_ms"] = round(statistics.median(lat), 1)

    # --- Recall@5（锚点判中：rel_path 相等 且 anchor 与 heading 互含）---
    if args.skip_recall:
        _write(result, args)
        return
    import numpy as np

    hits, per_query = 0, []
    for q in queries:
        q_emb = encode_query(q["question"])
        scores = np.asarray(doc_emb) @ np.asarray(q_emb).T
        top5 = [corpus[i] for i in scores.ravel().argsort()[::-1][:5]]
        hit = any(
            c["rel_path"] == r["rel_path"]
            and (
                r["anchor"].lower() in c["heading"].lower()
                or c["heading"].lower() in r["anchor"].lower()
            )
            for c in top5
            for r in q["relevant"]
        )
        hits += hit
        per_query.append(
            {"id": q["id"], "hit": hit, "top1": top5[0]["rel_path"] + " ➜ " + top5[0]["heading"]}
        )
    result["recall_at_5"] = round(hits / len(queries), 3)
    result["per_query"] = per_query
    _write(result, args)


def _write(result: dict, args) -> None:
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{args.model.replace('/', '__')}_{args.device}.json"
    if args.recall_only and out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        old.update(
            {
                k: result[k]
                for k in (
                    "recall_at_5",
                    "per_query",
                    "full_corpus_encode_s",
                    "dtype",
                    "max_seq_length",
                )
                if k in result
            }
        )
        old["recall_note"] = (
            "recall 基于 2026-07-12 修复后的语料（锚点全覆盖）重算；吞吐/延迟为修复前无争抢环境实测"
        )
        result = old
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "per_query"}, ensure_ascii=False, indent=2
        )
    )
    print(f"-> {out}")


if __name__ == "__main__":
    main()
