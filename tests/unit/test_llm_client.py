"""T31.2R-a：`OpenAICompatLLM.complete` 的请求侧 JSON 模式与空响应判定。

T31.2 首次真实 dev 运行暴露：`deepseek-v4-flash` 会间歇性把合法 JSON 写进
`message.reasoning_content` 而让 `message.content` 变成**空串**，而客户端只判
`content is None`，于是空串一路走到 `model_validate_json("")` 被记成"模型输出
不合规"。本文件锁住两件事：①`json_mode` 是**按调用点可选**的（P0 对照路径的
请求里不得出现 `response_format`，`answer.py:32-37` 要的是中文散文不是 JSON）；
②空串/纯空白与 `None` 一样都算**没拿到响应**。

不触网：用 stub 替换 `openai.AsyncOpenAI`，只断言请求参数与响应处理。
"""

from __future__ import annotations

from typing import Any

import openai
import pytest

from devkb.llm import FakeLLM, LLMError, OpenAICompatLLM


class _Usage:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return dict(self._payload)


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str | None, model: str) -> None:
        self.choices = [_Choice(content)]
        self.model = model
        self.usage = _Usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


class _Completions:
    def __init__(self, outcome: str | None | Exception, model: str) -> None:
        self._outcome = outcome
        self._model = model
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        self.requests.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _Response(self._outcome, self._model)


class _StubClient:
    def __init__(self, outcome: str | None | Exception, model: str) -> None:
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _Completions(outcome, model)  # type: ignore[attr-defined]


def _client(monkeypatch: pytest.MonkeyPatch, outcome: str | None | Exception) -> _Completions:
    """装好一个 stub 供应商客户端，返回它的 completions 以便检查请求参数。"""
    stub = _StubClient(outcome, "deepseek-v4-flash")
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_kwargs: stub)
    return stub.chat.completions  # type: ignore[attr-defined,no-any-return]


def _llm() -> OpenAICompatLLM:
    return OpenAICompatLLM(api_key="k", base_url="https://example.invalid", model="m")


# --- U1/U2：json_mode=True 的请求侧与返回值 ------------------------------------


async def test_u1_json_mode_declares_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    completions = _client(monkeypatch, '{"ok":true}')
    await _llm().complete(system="s", user="u", json_mode=True)

    assert len(completions.requests) == 1
    assert completions.requests[0]["response_format"] == {"type": "json_object"}


async def test_u2_returns_text_and_usage_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _client(monkeypatch, '{"ok":true}')
    result = await _llm().complete(system="s", user="u", json_mode=True)

    assert result.text == '{"ok":true}'
    assert result.model == "deepseek-v4-flash"
    assert result.usage["completion_tokens"] == 5


# --- U3：P0 对照路径的请求侧逐字节不变 -----------------------------------------


async def test_u3_default_call_never_sends_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`answer.py` 的 P0 路径不传 `json_mode`，请求里就**不得**出现该键。

    P0 的 `SYSTEM_PROMPT` 要求中文散文（`answer.py:32-37`），加 JSON 模式会当场
    破坏已冻结的 P0 对照基线。
    """
    completions = _client(monkeypatch, "这是中文散文答案 [E1]。")
    await _llm().complete(system="s", user="u")

    assert "response_format" not in completions.requests[0]


# --- U4/U4b/U5：空响应判定（含 P0 终态变化的冻结测试） -------------------------


@pytest.mark.parametrize("content", ["", "   ", "\n", "\t \n"])
async def test_u4_blank_content_is_an_empty_response_on_the_p0_path(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """**本用例即「P0 终态变化声明」的冻结测试**（前审 PG-T312Ra-02）。

    改前：空串不抛异常 → `apply_l0("")` → run 落 `succeeded` + 空正文。
    改后：抛 `LLMError` → `answer.py:69-74` tenacity 重试 1 次 → 仍空则
    `:99-113` 把 run 落 `failed` 并上抛。方向是"如实记为失败"。
    """
    _client(monkeypatch, content)
    with pytest.raises(LLMError, match="空响应"):
        await _llm().complete(system="s", user="u")


@pytest.mark.parametrize("content", ["", "   "])
async def test_u4b_json_mode_does_not_relax_the_empty_response_rule(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """fail-closed 负例：「JSON 模式使失败率降到 0」**推不出**（实测 31/32）。

    声明了 JSON 模式**不代表**可以放宽空响应判定——残余空串仍须照旧判失败。
    """
    _client(monkeypatch, content)
    with pytest.raises(LLMError, match="空响应"):
        await _llm().complete(system="s", user="u", json_mode=True)


async def test_u5_none_content_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _client(monkeypatch, None)
    with pytest.raises(LLMError, match="空响应"):
        await _llm().complete(system="s", user="u")


# --- U6：供应商错误映射，且不自动降级重试 --------------------------------------


async def test_u6_provider_error_maps_to_llm_error_without_silent_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """供应商拒绝 `response_format` 时不得静默改用无 JSON 模式重试。

    那会把"供应商契约变了"这件事藏起来；`max_retries=0` 的收敛策略也要求重试
    只发生在调用点（D6 有界重问），不在客户端内隐式发生。
    """
    completions = _client(monkeypatch, openai.BadRequestError.__new__(openai.BadRequestError))
    with pytest.raises(LLMError):
        await _llm().complete(system="s", user="u", json_mode=True)

    assert len(completions.requests) == 1


# --- U11：FakeLLM 与 Protocol 同步 ---------------------------------------------


async def test_u11_fake_llm_accepts_json_mode_without_changing_behaviour() -> None:
    """`FakeLLM` 必须能接住 `json_mode`（否则 agent 侧一传就炸），且行为不变。"""
    fake = FakeLLM(["first", "second"])

    assert (await fake.complete(system="s", user="u", json_mode=True)).text == "first"
    assert (await fake.complete(system="s", user="u")).text == "second"
    assert fake.prompts == [{"system": "s", "user": "u"}] * 2
