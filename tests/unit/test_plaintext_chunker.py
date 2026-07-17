"""T13.4 纯文本分块（配置文件）：行号逐字契约、空行分块、超长二分、不解析语法。"""

from __future__ import annotations

from devkb.ingest.markdown import approx_token_counter, chunk_plaintext

YAML = """\
# 数据库连接（注释不是标题）
spring:
  datasource:
    url: jdbc:postgresql://localhost:5432/mall
    password: ${DB_PASSWORD}

server:
  port: 8080
"""


def test_blocks_split_on_blank_lines_with_verbatim_lines() -> None:
    chunks = chunk_plaintext(YAML, count_tokens=approx_token_counter)
    lines = YAML.splitlines()
    assert chunks, "非空输入应产出 chunk"
    for c in chunks:
        assert c.content == "\n".join(lines[c.start_line - 1 : c.end_line]), "内容逐字对应源行"
        assert c.title_path == "", "纯文本没有可信标题结构"
    joined = "\n".join(c.content for c in chunks)
    assert "${DB_PASSWORD}" in joined, "占位符必须原样保留，从不解析/展开"
    assert "# 数据库连接（注释不是标题）" in chunks[0].content, "# 行是内容不是标题"


def test_oversized_block_binary_split() -> None:
    source = "\n".join(f"key{i}=value-{i}" for i in range(400))  # 无空行的超长块
    chunks = chunk_plaintext(source, count_tokens=approx_token_counter)
    assert len(chunks) > 1, "超目标块必须按行二分"
    lines = source.splitlines()
    covered: list[int] = []
    for c in chunks:
        assert c.content == "\n".join(lines[c.start_line - 1 : c.end_line])
        assert c.token_count <= 400 + 40
        covered.extend(range(c.start_line, c.end_line + 1))
    assert covered == list(range(1, 401)), "全部源行恰好覆盖一次"


def test_empty_and_blank_sources_yield_no_chunks() -> None:
    assert chunk_plaintext("", count_tokens=approx_token_counter) == []
    assert chunk_plaintext("\n\n\n", count_tokens=approx_token_counter) == []
