"""T17.3 L1 引文原文匹配：逐字/空白标点/篡改/跨 chunk 拼接/错误 evidence 正反例。"""

from __future__ import annotations

import uuid

from devkb.agent.state import ClaimOutput, Evidence
from devkb.agent.verification import (
    L1_MIN_QUOTE_CHARS,
    l1_errors,
    normalize_for_l1,
    quote_matches_evidence,
)

E1_CONTENT = (
    "库存扣减由事务保护，核心实现位于 OrderService.deductStock()。\n"
    "扣减前会检查可用库存：if (available < quantity) { throw new StockException(); }"
)
E2_CONTENT = "订单取消时通过补偿事务回滚库存，入口为 OrderCompensator.rollback()。"


def _evidence(number: int, content: str) -> Evidence:
    return Evidence(
        evidence_id=f"E{number}",
        chunk_id=uuid.UUID(int=number),
        rel_path="src/OrderService.java",
        title_path="OrderService",
        content=content,
        start_line=1,
        end_line=30,
        score=0.9,
    )


EVIDENCES = [_evidence(1, E1_CONTENT), _evidence(2, E2_CONTENT)]


def _claim(evidence_ids: list[str], quotes: list[str]) -> ClaimOutput:
    return ClaimOutput(text="库存扣减断言", evidence_ids=evidence_ids, quotes=quotes)


VERBATIM = "库存扣减由事务保护，核心实现位于 OrderService.deductStock()。"


def test_verbatim_quote_passes() -> None:
    assert quote_matches_evidence(VERBATIM, E1_CONTENT)
    assert quote_matches_evidence("if (available < quantity)", E1_CONTENT)


def test_whitespace_and_punctuation_variants_pass() -> None:
    # 换行/空格差异
    assert quote_matches_evidence(
        VERBATIM.replace("，", "，\n").replace("位于 ", "位于  "), E1_CONTENT
    )
    # 全角/半角标点差异（，→, 。→.）与全角字母数字折叠
    assert quote_matches_evidence(VERBATIM.replace("，", ",").replace("。", "."), E1_CONTENT)
    fullwidth = "ＯｒｄｅｒＳｅｒｖｉｃｅ．ｄｅｄｕｃｔＳｔｏｃｋ（）"
    assert normalize_for_l1(fullwidth) == "OrderService.deductStock()"


def test_light_tampering_fails() -> None:
    # 词语替换
    assert not quote_matches_evidence(VERBATIM.replace("事务", "锁"), E1_CONTENT)
    # 代码细节篡改（< 改 <=）
    assert not quote_matches_evidence("if (available <= quantity)", E1_CONTENT)
    # 大小写篡改：代码标识符大小写敏感
    assert not quote_matches_evidence("orderservice.deductStock()", E1_CONTENT)


def test_cross_chunk_concatenation_fails() -> None:
    spliced = "throw new StockException(); } 订单取消时通过补偿事务回滚库存"
    claim = _claim(["E1", "E2"], [spliced])
    errors, failed = l1_errors([claim], EVIDENCES)
    assert errors == ["L1:claim[0]:quote[0]:no_verbatim_match"]
    assert failed == {0}


def test_quote_from_wrong_evidence_fails_even_if_verbatim_elsewhere() -> None:
    # 逐字来自 E2，但 claim 只绑定 E1
    claim = _claim(["E1"], ["订单取消时通过补偿事务回滚库存"])
    errors, failed = l1_errors([claim], EVIDENCES)
    assert errors and failed == {0}
    # 绑定修正为 E2 后通过
    fixed = _claim(["E2"], ["订单取消时通过补偿事务回滚库存"])
    assert l1_errors([fixed], EVIDENCES) == ([], set())


def test_too_short_quote_fails_deterministically() -> None:
    short = "保护。"  # 规范化后 3 字符 < 阈值
    assert len(normalize_for_l1(short)) < L1_MIN_QUOTE_CHARS
    assert not quote_matches_evidence(short, E1_CONTENT)


def test_multi_claim_reports_only_failing_indices() -> None:
    claims = [
        _claim(["E1"], ["if (available < quantity)"]),
        _claim(["E2"], ["通过补偿事务回滚库存", "OrderCompensator.rollback() 被篡改"]),
    ]
    errors, failed = l1_errors(claims, EVIDENCES)
    assert errors == ["L1:claim[1]:quote[1]:no_verbatim_match"]
    assert failed == {1}
