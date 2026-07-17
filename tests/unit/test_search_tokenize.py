"""T12.2 FTS token 化纯函数基础行为（规格 §7）。

这里只锁 backfill 依赖的核心行为；中文/英文/Java 符号/RabbitMQ/URL/错误码的
golden 全集与最终冻结在 T14.1。
"""

from __future__ import annotations

from devkb.retrieval import (
    MAX_TOKEN_REPEAT,
    MAX_TOTAL_TOKENS,
    build_search_text,
    tokenize_for_search,
)


def test_camel_snake_path_split_preserves_originals() -> None:
    tokens = tokenize_for_search("OrderService docs/api-gateway-contract.md order_status_machine")
    assert "orderservice" in tokens, "原词（小写化）必须保留"
    assert "order" in tokens and "service" in tokens, "camelCase 拆分词必须保留"
    assert "docs/api-gateway-contract.md" in tokens, "路径原词必须保留"
    assert {"docs", "api", "gateway", "contract", "md"} <= set(tokens)
    assert "order_status_machine" in tokens and "status" in tokens and "machine" in tokens


def test_cjk_segmented_by_deterministic_jieba() -> None:
    text = "订单状态机支持自动取消"
    first = tokenize_for_search(text)
    assert first == tokenize_for_search(text), "同一输入必须输出确定性 token 流"
    assert "订单" in first and "状态机" in first


def test_numbers_and_routing_keys_produce_stable_tokens() -> None:
    tokens = tokenize_for_search("重复支付返回错误码 40901，事件 order.paid.event 不再消费")
    assert "40901" in tokens
    assert "order.paid.event" in tokens, "routing key 原词必须保留"
    assert "paid" in tokens and "event" in tokens


def test_lowercase_only_normalizes_tokens_not_source() -> None:
    tokens = tokenize_for_search("RabbitMQ")
    assert "rabbitmq" in tokens
    assert all(token == token.lower() for token in tokens)


def test_repeat_cap_is_deterministic() -> None:
    tokens = tokenize_for_search(" ".join(["Repeat"] * 50))
    assert tokens.count("repeat") == MAX_TOKEN_REPEAT


def test_overlong_token_dropped_but_split_parts_kept() -> None:
    assert tokenize_for_search("x" * 100) == [], "超长且无法拆分的 token 整体丢弃"
    tokens = tokenize_for_search("Very" + "Long" * 21)  # 88 字符 camelCase 标识符
    assert ("very" + "long" * 21) not in tokens, "超长原词丢弃"
    assert "very" in tokens and "long" in tokens, "camel 拆分词保留"


def test_total_token_hard_limit() -> None:
    # 12000 个互不相同的 token，超出总量上限后确定性截断
    text = " ".join(f"tok{i}" for i in range(12000))
    tokens = tokenize_for_search(text)
    assert len(tokens) == MAX_TOTAL_TOKENS
    assert tokens[0] == "tok0"


def test_build_search_text_joins_title_and_content_tokens() -> None:
    search_text = build_search_text(
        "Architecture > Event Flow", "支付成功后发布 PaymentSucceeded 事件"
    )
    parts = search_text.split(" ")
    assert "architecture" in parts and "event" in parts
    assert "paymentsucceeded" in parts and "支付" in parts
    assert build_search_text("", "") == ""
