"""FTS token 化纯函数（规格 §7）：摄取期写入、backfill 与查询期共用。

独立成模块的原因：repositories.py 在 chunk 插入点统一计算 search_text（新摄取
与 backfill 必然同函数，T12.2 复评修复），而 retrieval.py 依赖 repositories，
函数留在 retrieval 会形成循环导入。本模块无 SQL、无 IO，只做确定性文本变换。
当前版本诞生于 T12.2；golden 全集与最终冻结在 T14.1。
"""

from __future__ import annotations

import re

import jieba

# ASCII 标识符/路径/错误码/routing key 连续段（点/斜杠/冒号/短横线/下划线在段内），
# 或 CJK 连续段（基本区 + 扩展 A）
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:\-]*|[一-鿿㐀-䶿]+")
# camelCase / PascalCase / 字母数字边界拆分：HTTPServer → HTTP+Server，order40901 → order+40901
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_SEPARATORS_RE = re.compile(r"[._/:\-]+")

MAX_TOKEN_LENGTH = 64  # 超长 token 丢弃整体、保留拆分词（tsvector 单词素上限 2KB 的安全余量）
MAX_TOKEN_REPEAT = 10  # 同一 token 计入次数上限：保留词频信号但防止病态膨胀
MAX_TOTAL_TOKENS = 8192  # 输出总量硬上限，确定性截断

# 独立实例 + 关闭 HMM：不受全局用户词典影响，输出确定性（T11.2 spike 锁定）
_JIEBA = jieba.Tokenizer()


def tokenize_for_search(text: str) -> list[str]:
    """把原文变成规范化 token 流：保留原词（小写化）+ 拆分词，确定性输出。

    小写归一化只作用于 token 流；语料原文与引用不改写（§7 规则 6）。
    """
    tokens: list[str] = []
    seen: dict[str, int] = {}

    def _add(raw: str) -> None:
        token = raw.strip().lower()
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return
        count = seen.get(token, 0)
        if count >= MAX_TOKEN_REPEAT or len(tokens) >= MAX_TOTAL_TOKENS:
            return
        seen[token] = count + 1
        tokens.append(token)

    for segment in _SEGMENT_RE.findall(text):
        if segment[0].isascii():
            segment = segment.rstrip("./:-_")  # 句尾标点等尾随分隔符不进 token
            if not segment:
                continue
            _add(segment)  # 原词（如 docs/api-gateway-contract.md、order.paid.event）
            parts = [p for p in _SEPARATORS_RE.split(segment) if p]
            for part in parts:
                if part != segment:
                    _add(part)
                for sub in _CAMEL_RE.findall(part):
                    if sub != part:
                        _add(sub)
        else:
            # CJK：jieba 精确模式，HMM=False 保证确定性
            for word in _JIEBA.cut(segment, HMM=False):
                _add(word)
    return tokens


def build_search_text(title_path: str, content: str) -> str:
    """chunk 的 search_text 生成（§5.1）：标题路径 token + 正文 token 拼接。"""
    return " ".join(tokenize_for_search(f"{title_path}\n{content}"))
