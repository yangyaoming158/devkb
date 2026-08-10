"""T33.1 · function-calling 可行性实测探针（P1.6 spike）。

**零仓库数据**：工具与问题均为硬编码的合成内容，不查数据库、不加载 embedder、
不导入 `devkb.*` 的任何 src 模块——避免误触生产路径。

要拿的观测（O1–O6，对应 packet 的验收标准 2）：

- O1 能否返回结构化 ``tool_calls``，字段形态如何
- O2 一次响应里 ``tool_calls`` 的实际条数（「最多一个」是真实约束还是执行者的假设）
- O3 把工具结果喂回去后能否继续并给出最终文本
- O4 带 tools 时 ``content`` 与 ``reasoning_content`` 的取值形态（T31.2R-a 的
  「content 为空」是否复现）
- O5 一轮完整交互的实际请求数与 token 用量（供 11/5/4 定标）
- O6 失败形态：不给工具结果直接再问；``tool_choice`` 不同取值

运行（必须 unset 代理，见 CLAUDE.md 环境事实）：

    env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \\
        -u HTTPS_PROXY -u https_proxy \\
        uv run python benchmarks/p16_function_calling_spike.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from typing import Any

# --- 合成工具与问题：整份文件里没有任何本仓库或语料的信息 ---------------------

FAKE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某个城市当前天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}

SECOND_TOOL = {
    "type": "function",
    "function": {
        "name": "get_population",
        "description": "查询某个城市的人口",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

SYSTEM = "你是一个助手。需要外部数据时调用提供的工具，不要编造。"
# 刻意设计成「两个城市 + 两类数据」，用来观测 O2：模型会不会在一次响应里塞多个 tool_call
MULTI_QUESTION = "北京和上海现在的天气和人口分别是多少？"
SINGLE_QUESTION = "北京现在天气怎么样？"


def _client() -> Any:
    from openai import AsyncOpenAI

    key = os.environ.get("DEVKB_LLM_API_KEY", "")
    if not key:
        # .env 不入库，但本机开发时值在其中；只读取，永不打印（I4）
        env_path = pathlib.Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEVKB_LLM_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    if not key:
        print("SKIP: 未取得 DEVKB_LLM_API_KEY（只读环境变量或 .env，不打印其值）")
        sys.exit(2)
    return AsyncOpenAI(
        api_key=key,
        base_url=os.environ.get("DEVKB_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        timeout=60.0,
        max_retries=0,  # 显式关闭 SDK 隐式重试，使请求数可数（D6 纪律）
    )


MODEL = os.environ.get("DEVKB_LLM_MODEL", "deepseek-v4-flash")


def _describe(resp: Any) -> dict[str, Any]:
    """把响应压成可留档的字段摘要（不转述，直接取字段）。"""
    choice = resp.choices[0]
    msg = choice.message
    calls = getattr(msg, "tool_calls", None) or []
    return {
        "finish_reason": choice.finish_reason,
        "content_type": type(msg.content).__name__,
        "content_len": len(msg.content or ""),
        "content_head": (msg.content or "")[:80],
        "has_reasoning_content": hasattr(msg, "reasoning_content"),
        "reasoning_len": len(getattr(msg, "reasoning_content", None) or ""),
        "tool_calls_n": len(calls),
        "tool_calls": [
            {
                "id": c.id,
                "type": c.type,
                "name": c.function.name,
                "arguments_raw": c.function.arguments,
                "arguments_parses": _json_ok(c.function.arguments),
            }
            for c in calls
        ],
        "usage": {
            "prompt": resp.usage.prompt_tokens,
            "completion": resp.usage.completion_tokens,
            "total": resp.usage.total_tokens,
        }
        if resp.usage
        else None,
    }


def _json_ok(raw: str) -> bool:
    try:
        json.loads(raw)
    except Exception:
        return False
    return True


async def run_once(trial: int) -> dict[str, Any]:
    client = _client()
    out: dict[str, Any] = {"trial": trial, "requests": 0}

    # --- O1/O2/O4/O5：第一次带 tools 的请求 ---------------------------------
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": MULTI_QUESTION},
    ]
    r1 = await client.chat.completions.create(
        model=MODEL, messages=messages, tools=[FAKE_TOOL, SECOND_TOOL]
    )
    out["requests"] += 1
    out["step1_multi_intent"] = _describe(r1)

    # --- O3：把工具结果喂回去，看能否继续 -----------------------------------
    calls = getattr(r1.choices[0].message, "tool_calls", None) or []
    if calls:
        messages.append(r1.choices[0].message.model_dump(exclude_none=True))
        for c in calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": json.dumps(
                        {"result": "晴，24℃" if c.function.name == "get_weather" else 21_500_000},
                        ensure_ascii=False,
                    ),
                }
            )
        r2 = await client.chat.completions.create(
            model=MODEL, messages=messages, tools=[FAKE_TOOL, SECOND_TOOL]
        )
        out["requests"] += 1
        out["step2_after_tool_result"] = _describe(r2)
    else:
        out["step2_after_tool_result"] = {"skipped": "第一步没有 tool_calls"}

    # --- O6-a：不给工具结果，直接再问一次 -----------------------------------
    if calls:
        bad = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": MULTI_QUESTION},
            r1.choices[0].message.model_dump(exclude_none=True),
            {"role": "user", "content": "算了，直接说结论。"},
        ]
        try:
            r3 = await client.chat.completions.create(
                model=MODEL, messages=bad, tools=[FAKE_TOOL, SECOND_TOOL]
            )
            out["requests"] += 1
            out["step3_missing_tool_result"] = _describe(r3)
        except Exception as exc:  # 失败形态本身就是观测目标
            out["step3_missing_tool_result"] = {
                "raised": type(exc).__name__,
                "message": str(exc)[:300],
            }

    # --- O6-b：tool_choice="none" 与单意图问题 -------------------------------
    r4 = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": SINGLE_QUESTION},
        ],
        tools=[FAKE_TOOL],
        tool_choice="none",
    )
    out["requests"] += 1
    out["step4_tool_choice_none"] = _describe(r4)

    r5 = await client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": SINGLE_QUESTION},
        ],
        tools=[FAKE_TOOL],
    )
    out["requests"] += 1
    out["step5_single_intent"] = _describe(r5)

    return out


async def main() -> None:
    if any(os.environ.get(v) for v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")):
        print("ABORT: 代理环境变量仍然设置——CLAUDE.md 环境事实要求 unset 后再调真实模型")
        sys.exit(3)

    trials = []
    for i in range(1, 4):  # 三次重复，观察稳定性（该模型有已知间歇性问题）
        print(f"--- trial {i} ---", flush=True)
        try:
            t = await run_once(i)
        except Exception as exc:
            t = {"trial": i, "fatal": type(exc).__name__, "message": str(exc)[:400]}
        trials.append(t)
        print(json.dumps(t, ensure_ascii=False, indent=1), flush=True)

    dest = pathlib.Path("benchmarks/p16_function_calling_spike.json")
    dest.write_text(
        json.dumps({"model": MODEL, "trials": trials}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\nwritten: {dest}")


if __name__ == "__main__":
    asyncio.run(main())
