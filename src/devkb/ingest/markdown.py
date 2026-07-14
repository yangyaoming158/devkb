"""Markdown 标题树分块（规格 §6/§7）。

token 计数注入普通 callable（`Callable[[str], int]`）——不新增 Protocol（CLAUDE.md 硬规则）。
生产注入 `qwen_token_counter()`（懒加载，属 ml 依赖组）；CI 与 golden 测试注入
`approx_token_counter`（确定性近似，与 FakeEmbedder 同哲学：测机制不测模型）。

分块规则：
- 按标题（h1–h6）切章节，title_path 为标题面包屑（"架构 > 库存 > 并发控制"）；
- 章节内按顶层块（段落/围栏/表格/列表…）贪心装箱到目标 token 数；
- 超目标的非原子块按行二分递归切分（规格 §7"段落递归切分"）；
- 代码围栏与表格是原子块，永不从内部切开，超目标时整块保留；
- 切分下限是"单行"：引用以行号定位，行内切开会使 content 与行号失配；
- 记录源文件 1-based 起止行号（引用定位用）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from markdown_it import MarkdownIt

TokenCounter = Callable[[str], int]


def approx_token_counter(text: str) -> int:
    """确定性近似计数：CJK ≈ 1 token/字，其余 ≈ 4 字符/token。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def qwen_token_counter(model_id: str) -> TokenCounter:
    """生产计数器：冻结模型 tokenizer（ADR-0002）。懒加载，勿在 CI 路径调用。"""
    from transformers import AutoTokenizer  # ml 依赖组，函数内 import 保证 CI 可导入本模块

    try:
        tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
    except OSError:  # 本地缓存缺失时才走网络
        tokenizer = AutoTokenizer.from_pretrained(model_id)

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False))

    return count


@dataclass(frozen=True)
class MdChunk:
    ordinal: int
    title_path: str
    content: str
    content_hash: str
    token_count: int
    start_line: int  # 1-based，含
    end_line: int  # 1-based，含


@dataclass(frozen=True)
class _Block:
    start: int  # 0-based 行号
    end: int  # 0-based 开区间
    atomic: bool = False  # 围栏/表格：永不内部切分


_ATOMIC_TOKEN_TYPES = frozenset({"fence", "code_block", "table_open"})


def _split_block(
    block: _Block, lines: list[str], count_tokens: TokenCounter, target_tokens: int
) -> list[_Block]:
    """超目标的非原子块按行二分递归切分；原子块与单行是切分下限。"""
    if block.atomic or block.end - block.start <= 1:
        return [block]
    if count_tokens("\n".join(lines[block.start : block.end])) <= target_tokens:
        return [block]
    mid = (block.start + block.end) // 2
    return _split_block(
        _Block(block.start, mid), lines, count_tokens, target_tokens
    ) + _split_block(_Block(mid, block.end), lines, count_tokens, target_tokens)


def chunk_markdown(
    source: str, *, count_tokens: TokenCounter, target_tokens: int = 400
) -> list[MdChunk]:
    lines = source.splitlines()
    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(source)

    # 第一趟：按标题切章节，收集章节内顶层块的行 span
    sections: list[tuple[str, list[_Block]]] = []
    heading_stack: list[tuple[int, str]] = []
    current_title = ""
    current_blocks: list[_Block] = []

    def flush() -> None:
        nonlocal current_blocks
        if current_blocks:
            sections.append((current_title, current_blocks))
        current_blocks = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open" and tok.level == 0:
            flush()
            level = int(tok.tag[1])
            heading_text = tokens[i + 1].content.strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading_text))
            current_title = " > ".join(text for _, text in heading_stack)
            # 标题行并入本章节首块，使 chunk 内容自足
            if tok.map:
                current_blocks.append(_Block(tok.map[0], tok.map[1]))
            i += 3  # heading_open + inline + heading_close
            continue
        if tok.level == 0 and tok.map is not None:
            current_blocks.append(
                _Block(tok.map[0], tok.map[1], atomic=tok.type in _ATOMIC_TOKEN_TYPES)
            )
        i += 1
    flush()

    # 第二趟：章节内贪心装箱（围栏/表格等作为原子块，永不内部切分）
    chunks: list[MdChunk] = []
    for title_path, blocks in sections:
        packs: list[list[_Block]] = []
        pack: list[_Block] = []
        pack_tokens = 0
        for raw_block in blocks:
            for block in _split_block(raw_block, lines, count_tokens, target_tokens):
                block_tokens = count_tokens("\n".join(lines[block.start : block.end]))
                if pack and pack_tokens + block_tokens > target_tokens:
                    packs.append(pack)
                    pack, pack_tokens = [], 0
                pack.append(block)
                pack_tokens += block_tokens
        if pack:
            packs.append(pack)

        for p in packs:
            start, end = p[0].start, p[-1].end
            content = "\n".join(lines[start:end]).strip("\n")
            if not content.strip():
                continue
            chunks.append(
                MdChunk(
                    ordinal=len(chunks),
                    title_path=title_path,
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    token_count=count_tokens(content),
                    start_line=start + 1,
                    end_line=end,
                )
            )
    return chunks
