"""T1.5 DeepSeek API smoke：连通性、P50 延迟、JSON 结构化输出解析成功率。

用法：.venv/bin/python benchmarks/deepseek_smoke.py
读取环境变量或项目根 .env：DEVKB_LLM_API_KEY、DEVKB_LLM_BASE_URL(默认官方)、DEVKB_LLM_MODEL(默认 deepseek-chat)。
产出：benchmarks/results/deepseek_smoke.json（供 ADR-004 引用）。
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

HERE = Path(__file__).parent


def load_env() -> None:
    env_file = HERE.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


PLAN_PROMPT = (
    "你是软件项目知识助手的查询规划器。把下面的问题拆成不超过3个检索子查询，"
    '只输出 JSON：{"intent": "...", "subqueries": ["..."]}。\n问题：订单创建流程是否可能重复扣减库存？'
)
GEN_PROMPT = (
    "仅依据以下证据回答问题，引用用 [E1] 标记，证据不含答案时明确说明。\n"
    "[E1] 库存扣减以 orderNo+changeType 为幂等键，重复请求返回已有记录的快照，不改库存。\n"
    "问题：库存重复扣减会发生什么？"
)


def main() -> None:
    load_env()
    from openai import OpenAI

    api_key = os.environ.get("DEVKB_LLM_API_KEY")
    assert api_key, "缺少 DEVKB_LLM_API_KEY（写入项目根 .env）"
    base_url = os.environ.get("DEVKB_LLM_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("DEVKB_LLM_MODEL", "deepseek-v4-flash")
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)

    result: dict = {"base_url": base_url, "model": model}

    def run(name: str, prompt: str, json_mode: bool) -> None:
        lats, parse_ok, served_model = [], 0, None
        for _ in range(5):
            t0 = time.perf_counter()
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=300,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
            lats.append((time.perf_counter() - t0) * 1000)
            served_model = resp.model
            if json_mode:
                try:
                    d = json.loads(resp.choices[0].message.content)
                    parse_ok += int("subqueries" in d)
                except (json.JSONDecodeError, TypeError):
                    pass
        result[name] = {
            "p50_ms": round(statistics.median(lats), 0),
            "max_ms": round(max(lats), 0),
            **({"json_parse_ok": f"{parse_ok}/5"} if json_mode else {}),
        }
        result["served_model"] = served_model

    run("plan_json_mode", PLAN_PROMPT, json_mode=True)
    run("generate", GEN_PROMPT, json_mode=False)

    out = HERE / "results" / "deepseek_smoke.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
