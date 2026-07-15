"""LLM 接缝（规格 §3：Protocol 只允许出现在本文件与 embedding.py）。

- OpenAICompatLLM：DeepSeek 经 OpenAI 兼容端点（ADR-0004），temperature=0，
  SDK 异常映射为 LLMTimeoutError/LLMError；自身不重试——
  "1 次重问"由调用点（answer.py 的唯一在线调用点）有界控制（D6）。
- FakeLLM：脚本化输出（按序返回文本或抛出预设异常），记录收到的 prompt，
  供 e2e 断言；CI 禁真模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from devkb.errors import LLMError, LLMTimeoutError

# 单价（元/百万 tokens，ADR-0004 2026-07-13 官网核实；未收录的模型不计价返回 None）
_PRICE_PER_MTOK: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    # (输入缓存命中, 输入缓存未命中, 输出)
    "deepseek-v4-flash": (Decimal("0.02"), Decimal("1"), Decimal("2")),
}


def compute_cost(model: str, usage: dict[str, Any] | None) -> Decimal | None:
    """按三档单价计算一次调用成本（元），纯函数。

    usage 为供应商原始 usage（agent_runs.usage JSONB）。DeepSeek 按缓存命中/
    未命中分别计价，故不能用合计 prompt_tokens 单价折算（规格 §5 第三轮修正）；
    无分档字段的 usage 保守地把全部输入按未命中计。模型不在价目表或 usage 缺失
    时返回 None（cost 可空，不编造）。
    """
    prices = _PRICE_PER_MTOK.get(model)
    if prices is None or not usage:
        return None
    price_hit, price_miss, price_out = prices
    hit = int(usage.get("prompt_cache_hit_tokens", 0))
    miss_raw = usage.get("prompt_cache_miss_tokens")
    miss = (
        int(miss_raw) if miss_raw is not None else max(int(usage.get("prompt_tokens", 0)) - hit, 0)
    )
    out = int(usage.get("completion_tokens", 0))
    cost = (hit * price_hit + miss * price_miss + out * price_out) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"))


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    usage: dict[str, Any]


class LLMClient(Protocol):
    async def complete(self, *, system: str, user: str) -> LLMResult: ...


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_s: float = 60.0,
    ) -> None:
        from openai import AsyncOpenAI

        # max_retries=0：重试策略收敛到调用点，避免 SDK 内隐式重试叠加
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
        )
        self._model = model

    async def complete(self, *, system: str, user: str) -> LLMResult:
        import openai

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.APITimeoutError as exc:
            raise LLMTimeoutError(f"LLM 请求超时：{exc}") from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"LLM 请求失败：{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice is None or choice.message.content is None:
            raise LLMError("LLM 返回空响应")
        usage = response.usage.model_dump() if response.usage else {}
        return LLMResult(text=choice.message.content, model=response.model, usage=usage)


class FakeLLM:
    """按序弹出脚本项：str → 正常返回；Exception → 抛出（模拟超时/故障）。"""

    def __init__(self, script: list[str | Exception], model: str = "fake-llm") -> None:
        self._script = list(script)
        self._model = model
        self.prompts: list[dict[str, str]] = []

    async def complete(self, *, system: str, user: str) -> LLMResult:
        self.prompts.append({"system": system, "user": user})
        if not self._script:
            raise LLMError("FakeLLM 脚本已耗尽")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(
            text=item,
            model=self._model,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_cache_hit_tokens": 20,
                "prompt_cache_miss_tokens": 80,
            },
        )
