"""L1 引文原文匹配（规格 §10，T17.3）。

规范化与阈值按判据在 dev 集冻结（2026-07-18）：
NFKC 折叠（全角字母/数字/标点 → 半角）+ 中文标点映射 + 去除全部空白后，
quote 必须是其所绑定的某**单条**证据 content 的精确子串。阈值等价 1.0：
不做模糊相似度——任何 <1.0 的阈值都会放行"轻微篡改"，与引文忠实性目标冲突。
不做大小写折叠：代码标识符大小写敏感（OrderService ≠ orderservice）。
规范化后短于 L1_MIN_QUOTE_CHARS 的 quote 几乎必然误配，直接判失败。
"""

from __future__ import annotations

import unicodedata

from devkb.agent.state import ClaimOutput, Evidence

L1_MIN_QUOTE_CHARS = 4

# NFKC 不折叠的常见中文标点 → 半角对应；纯装饰符号移除
_PUNCT_TABLE = str.maketrans(
    {
        "。": ".",
        "、": ",",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "《": "<",
        "》": ">",
        "【": "[",
        "】": "]",
        "—": "-",
        "…": "...",
        "·": None,
    }
)


def normalize_for_l1(text: str) -> str:
    """L1 冻结规范化：NFKC → 中文标点映射 → 去除全部空白。"""
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCT_TABLE)
    return "".join(ch for ch in folded if not ch.isspace())


def quote_matches_evidence(quote: str, content: str) -> bool:
    """quote 规范化后须为单条证据 content 规范化文本的精确子串。"""
    normalized_quote = normalize_for_l1(quote)
    if len(normalized_quote) < L1_MIN_QUOTE_CHARS:
        return False
    return normalized_quote in normalize_for_l1(content)


def l1_errors(
    claims: list[ClaimOutput],
    evidences: list[Evidence],
) -> tuple[list[str], set[int]]:
    """逐 claim 逐 quote 校验；返回（机器可读错误，失败 claim 下标集合）。

    quote 只须匹配该 claim 绑定证据中的任意一条，但必须整体落在单条证据内——
    跨 chunk 拼接的引文在任何单条证据中都不构成子串，判失败。
    绑定了不存在的 evidence_id 属 L0 职责，此处仅跳过未知 id。
    """
    content_by_id = {evidence.evidence_id: evidence.content for evidence in evidences}
    errors: list[str] = []
    failed: set[int] = set()
    for claim_index, claim in enumerate(claims):
        contents = [
            content_by_id[evidence_id]
            for evidence_id in claim.evidence_ids
            if evidence_id in content_by_id
        ]
        for quote_index, quote in enumerate(claim.quotes):
            if not any(quote_matches_evidence(quote, content) for content in contents):
                errors.append(f"L1:claim[{claim_index}]:quote[{quote_index}]:no_verbatim_match")
                failed.add(claim_index)
    return errors, failed
