"""T14.1 token 化 golden 全集与冻结（规格 §7）。

golden 快照锁定 `tokenize_for_search` 对六类判据输入（中文、英文、Java 符号、
RabbitMQ 名称、URL/路径、错误码）及混合真实查询的**完整 token 流**。
本套件通过后 token 化函数即冻结：任何输出变化都会打破快照，必须
（1）人工审阅 DEVKB_UPDATE_GOLDEN=1 的 diff，（2）对存量语料重跑
`devkb backfill-search`，否则摄取期与查询期 token 不再对称。
"""

from __future__ import annotations

import pytest

from conftest import FIXTURES_DIR, GoldenCheck
from devkb.fts import MAX_TOKEN_LENGTH, tokenize_for_search

GOLDEN_PATH = FIXTURES_DIR / "golden_fts" / "tokenize_cases.json"

# case 名 → 输入。覆盖 T14.1 判据列举的全部类别；输入取材 mini-mall 语料风格。
CASES: dict[str, str] = {
    "chinese": "订单状态机支持超时自动取消，库存回补后重新发布领域事件",
    "english": "The payment gateway retries idempotent requests before the timeout expires",
    "java_symbols": (
        "OrderStateMachine.transition() 校验 InventoryService#deductStock；"
        "字段 order_status_machine 与 createOrder(OrderCreateRequest request)"
    ),
    "rabbitmq": (
        "exchange mall.order.exchange 绑定 order.paid.event，"
        "死信走 order.pay.timeout.queue（x-dead-letter-exchange）"
    ),
    "url_path": (
        "见 docs/api-gateway-contract.md 与 /api/v1/orders/{orderId}/cancel，"
        "仓库 https://github.com/example/mini-mall-order"
    ),
    "error_codes": "重复支付返回 40901，库存不足 40902；HTTP 409 对应 ORDER_STATE_CONFLICT",
    "mixed_query": "OrderStateMachine 在什么情况下会把订单标记为 CANCELLED？",
    "snake_and_kebab": "api-gateway 的 request_id 透传给 inventory_service 做幂等键",
}


def test_tokenize_golden_full_set(golden_check: GoldenCheck) -> None:
    actual = [
        {"case": name, "input": text, "tokens": tokenize_for_search(text)}
        for name, text in CASES.items()
    ]
    golden_check(GOLDEN_PATH, actual)


@pytest.mark.parametrize("text", ["", "   \n\t", "，。！？——（）", "!!!&&&|||"])
def test_no_searchable_content_yields_empty(text: str) -> None:
    assert tokenize_for_search(text) == []


def test_determinism_across_repeated_calls() -> None:
    for text in CASES.values():
        assert tokenize_for_search(text) == tokenize_for_search(text)


def test_all_tokens_within_length_cap_and_lowercase() -> None:
    for text in CASES.values():
        for token in tokenize_for_search(text):
            assert 0 < len(token) <= MAX_TOKEN_LENGTH
            assert token == token.lower()


def _load_golden_tokens() -> dict[str, list[str]]:
    import json

    entries = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return {entry["case"]: entry["tokens"] for entry in entries}


def test_golden_contains_key_retrieval_tokens() -> None:
    """语义抽查：golden 更新时防止"快照照单全收"错误输出（如原词丢失）。"""
    tokens = _load_golden_tokens()
    assert "订单" in tokens["chinese"] and "状态机" in tokens["chinese"]
    assert "idempotent" in tokens["english"]
    assert {"orderstatemachine.transition", "orderstatemachine", "transition"} <= set(
        tokens["java_symbols"]
    )
    assert "order_status_machine" in tokens["java_symbols"]
    assert {"order.paid.event", "mall.order.exchange", "x-dead-letter-exchange"} <= set(
        tokens["rabbitmq"]
    )
    assert "docs/api-gateway-contract.md" in tokens["url_path"]
    assert "orderid" in tokens["url_path"], "路径变量 {orderId} 的标识符可检索"
    assert {"40901", "40902", "409", "order_state_conflict"} <= set(tokens["error_codes"])
    assert "cancelled" in tokens["mixed_query"]
