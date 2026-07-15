"""compute_cost 三档计价纯函数（T9.5，单价来源 ADR-0004）。"""

from decimal import Decimal

from devkb.llm import compute_cost


def test_three_tier_pricing() -> None:
    # 1M 命中 + 1M 未命中 + 1M 输出 = 0.02 + 1 + 2 元，逐档可手算复核
    usage = {
        "prompt_tokens": 2_000_000,
        "prompt_cache_hit_tokens": 1_000_000,
        "prompt_cache_miss_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
    }
    assert compute_cost("deepseek-v4-flash", usage) == Decimal("3.02")


def test_typical_small_run_quantized_to_6_places() -> None:
    usage = {
        "prompt_tokens": 1000,
        "prompt_cache_hit_tokens": 300,
        "prompt_cache_miss_tokens": 700,
        "completion_tokens": 200,
    }
    # 300*0.02 + 700*1 + 200*2 = 1106 / 1e6 元
    assert compute_cost("deepseek-v4-flash", usage) == Decimal("0.001106")


def test_unknown_model_returns_none() -> None:
    assert compute_cost("fake-llm", {"prompt_tokens": 100, "completion_tokens": 50}) is None


def test_missing_usage_returns_none() -> None:
    assert compute_cost("deepseek-v4-flash", None) is None
    assert compute_cost("deepseek-v4-flash", {}) is None


def test_no_tier_fields_falls_back_to_all_miss() -> None:
    # 无分档字段时全部输入按未命中保守计价
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    assert compute_cost("deepseek-v4-flash", usage) == Decimal("0.0012")


def test_hit_present_miss_absent_derives_miss_from_total() -> None:
    usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 400, "completion_tokens": 0}
    # miss = 1000 - 400 = 600 → 400*0.02 + 600*1 = 608 / 1e6
    assert compute_cost("deepseek-v4-flash", usage) == Decimal("0.000608")
