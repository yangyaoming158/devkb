"""T29 unknown project fail-fast 顺序（RT-23，《P1.5实现规格》§11、机制检查 m17）。

现有 unknown-project 用例（test_service.py / test_api.py）注入的是已实例化的
FakeEmbedder/FakeLLM，生产工厂根本不在调用图上——c8 把这一点变成机器断言：同样的
"两个 factory 均 0 次"在**成功路径**上也成立，故它对调用顺序零判别力。

本文件因此一律用**冷 AppService**（零注入）+ 装在真实构造接缝上的 spy：
- `devkb.embedding.SentenceTransformerEmbedder` 与 `devkb.llm.OpenAICompatLLM` 都是
  函数体内 import（service.py:95 / :119），monkeypatch 模块属性即可拦截真实构造；
- c2 另把 `_session_factory` 换成记录进出的包装，用一条有序事件账本锁住两-session
  契约（首次查询 session 必须在两个工厂被构造前退出），因为计数断言对单 session
  实现同样通过。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

import devkb.embedding
import devkb.llm
from devkb.api import create_app
from devkb.config import Settings
from devkb.embedding import FakeEmbedder
from devkb.errors import (
    DevKbError,
    EmbeddingError,
    InternalError,
    InvalidInputError,
    NotFoundError,
)
from devkb.ingest.markdown import approx_token_counter
from devkb.llm import FakeLLM
from devkb.service import AppService

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)
SUCCESS_SCRIPT: list[str | Exception] = [PLAN, EVAL_OK, GEN]

QUESTION = "库存怎么保证并发安全？"
UNKNOWN_SLUG = "t29-no-such-project"

# m17 断言 3：零 project/run/step/tool 持久化
PERSISTED_TABLES = ("projects", "agent_runs", "agent_steps", "tool_invocations")

# 两-session 契约的冻结事件账本（与 Task Packet「实现计划 · 数据流」逐字对应）：
# 首次查询 session 进→出，随后在活动 session 数为 0 时构造两个工厂，最后另开一个
# session 跑问答。单 session 实现会得到 ("factory","embedder",1)，旧顺序会把两个
# factory 事件排到首个 ("session","enter") 之前——两者都与本账本不等。
FROZEN_SESSION_LEDGER: list[tuple[Any, ...]] = [
    ("session", "enter"),
    ("session", "exit"),
    ("factory", "embedder", 0),
    ("factory", "llm", 0),
    ("session", "enter"),
    ("session", "exit"),
]

# 诚实边界：用户可见错误体不得出现 DSN/驱动/内部路径/依赖名/traceback
FORBIDDEN_IN_USER_FACING_ERROR = (
    "postgresql",
    "asyncpg",
    "Traceback",
    "/home/",
    "SentenceTransformer",
    "torch",
    "openai",
    "AsyncOpenAI",
)


class _Ledger:
    """session 进出与工厂构造的单一有序事件流（工厂事件带当时的活动 session 数）。"""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.active = 0

    def session_enter(self) -> None:
        self.active += 1
        self.events.append(("session", "enter"))

    def session_exit(self) -> None:
        self.active -= 1
        self.events.append(("session", "exit"))

    def factory(self, kind: str) -> None:
        self.events.append(("factory", kind, self.active))


class _FactorySpy:
    """替换真实工厂：记录构造次数与时刻，并返回可用的 Fake 使成功路径能跑通。"""

    def __init__(self, kind: str, ledger: _Ledger, produce: Callable[[], Any]) -> None:
        self._kind = kind
        self._ledger = ledger
        self._produce = produce
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self._ledger.factory(self._kind)
        return self._produce()


class _ExplodingFactory:
    """必抛桩：被调用即失败——用于证明"工厂失败也不得改写 404"（RT-23 第二症状）。"""

    def __init__(self, kind: str, ledger: _Ledger) -> None:
        self._kind = kind
        self._ledger = ledger
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self._ledger.factory(self._kind)
        raise RuntimeError(f"{self._kind} factory must not be called")


@dataclass
class _Spies:
    embedder: Any
    llm: Any
    ledger: _Ledger = field(default_factory=_Ledger)

    @property
    def calls(self) -> tuple[int, int]:
        return (self.embedder.calls, self.llm.calls)


def _settings(db_url: str) -> Settings:
    return Settings(llm_api_key=SecretStr("t29-integration"), database_url=db_url)


def _install_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    script: list[str | Exception] | None = None,
    exploding: bool = False,
) -> _Spies:
    ledger = _Ledger()
    if exploding:
        embedder_factory: Any = _ExplodingFactory("embedder", ledger)
        llm_factory: Any = _ExplodingFactory("llm", ledger)
    else:
        embedder_factory = _FactorySpy("embedder", ledger, FakeEmbedder)
        llm_factory = _FactorySpy(
            "llm", ledger, lambda: FakeLLM(list(script or []), model="deepseek-v4-flash")
        )
    monkeypatch.setattr(devkb.embedding, "SentenceTransformerEmbedder", embedder_factory)
    monkeypatch.setattr(devkb.llm, "OpenAICompatLLM", llm_factory)
    return _Spies(embedder=embedder_factory, llm=llm_factory, ledger=ledger)


def _cold_service(db_url: str) -> AppService:
    """零注入：embedder/llm/count_tokens 都必须经生产工厂懒加载（spy 因此可观察）。"""
    return AppService(_settings(db_url))


def _record_sessions(service: AppService, ledger: _Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 session factory 换成记录进出的包装（只改测试内的实例属性，src/ 无钩子）。"""
    real = service._session_factory

    @asynccontextmanager
    async def wrapped() -> AsyncIterator[AsyncSession]:
        ledger.session_enter()
        try:
            async with real() as session:
                yield session
        finally:
            ledger.session_exit()

    monkeypatch.setattr(service, "_session_factory", wrapped)


async def _seed_project(db_url: str) -> str:
    """用**注入 Fake 的另一个 service** 建语料：不惊动 spy，冷 service 仍是冷的。"""
    slug = f"t29-{uuid.uuid4().hex[:8]}"
    seeder = AppService(
        _settings(db_url),
        embedder=FakeEmbedder(),
        llm=FakeLLM([]),
        count_tokens=approx_token_counter,
    )
    try:
        await seeder.ingest(slug, CORPUS_MD)
    finally:
        await seeder.aclose()
    return slug


async def _table_counts(session: AsyncSession) -> dict[str, int]:
    # 每次先 rollback 开新事务：避免同一事务快照掩盖期间的已提交写入
    await session.rollback()
    counts: dict[str, int] = {}
    for table in PERSISTED_TABLES:
        result = await session.execute(text(f"select count(*) from {table}"))
        counts[table] = int(result.scalar_one())
    return counts


def _forbidden_tokens(db_url: str) -> tuple[str, ...]:
    password = make_url(db_url).password
    return (*FORBIDDEN_IN_USER_FACING_ERROR, *((password,) if password else ()))


def _client(service: AppService) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(service))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# --- c1：失败路径 —— unknown project 不得构造任一工厂 -------------------------------


@pytest.mark.parametrize("pipeline", ["agentic", "fixed-rag"])
async def test_t29_c1_cold_service_unknown_project_constructs_neither_factory(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch, pipeline: str
) -> None:
    spies = _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    service = _cold_service(migrated_db_url)
    try:
        with pytest.raises(NotFoundError) as exc_info:
            await service.ask(UNKNOWN_SLUG, QUESTION, pipeline=pipeline)
    finally:
        await service.aclose()

    assert exc_info.value.code == "NOT_FOUND"
    assert spies.calls == (0, 0)
    # 诚实边界：文案是既有串（锁定"未新增/未改写"），且不含 DSN/驱动/依赖/traceback
    message = str(exc_info.value)
    assert message == f"项目 '{UNKNOWN_SLUG}' 不存在（先执行 devkb ingest）"
    for forbidden in _forbidden_tokens(migrated_db_url):
        assert forbidden not in message


# --- c2：正常路径 —— spy 非空洞 + 两-session 契约的唯一判别测试 ---------------------


async def test_t29_c2_cold_service_closes_first_session_before_constructing_factories(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = await _seed_project(migrated_db_url)
    spies = _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    service = _cold_service(migrated_db_url)
    _record_sessions(service, spies.ledger, monkeypatch)
    try:
        answer = await service.ask(slug, QUESTION)
    finally:
        await service.aclose()

    # ① 非空洞性（对顺序不敏感）：两个 spy 确实挂在生产构造接缝上
    assert answer["mode"] == "full"
    assert spies.calls == (1, 1)
    # ② 两-session 契约（判别性所在）：首次 session 已退出、活动数为 0 才构造工厂
    assert spies.ledger.events == FROZEN_SESSION_LEDGER


# --- c3：工厂失败不得改写错误码 ----------------------------------------------------


async def test_t29_c3_failing_factories_do_not_turn_404_into_embedding_error(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _install_spies(monkeypatch, exploding=True)
    service = _cold_service(migrated_db_url)
    try:
        with pytest.raises(DevKbError) as exc_info:
            await service.ask(UNKNOWN_SLUG, QUESTION)
    finally:
        await service.aclose()

    error = exc_info.value
    assert isinstance(error, NotFoundError)
    assert not isinstance(error, EmbeddingError | InternalError)
    assert error.code == "NOT_FOUND"
    assert "RuntimeError" not in str(error) and "factory" not in str(error)
    assert spies.calls == (0, 0)


# --- c4：输入校验先于 project 查询与工厂 -------------------------------------------


async def test_t29_c4_input_validation_precedes_project_lookup_and_factories(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    service = _cold_service(migrated_db_url)
    invalid_calls: list[dict[str, Any]] = [
        {"pipeline": "agent-x"},
        {"top_k": 0},
        {"top_k": 13},
        {"question": "   "},
        {"question": "长" * 4001},
    ]
    try:
        for overrides in invalid_calls:
            question = overrides.pop("question", QUESTION)
            # slug 仍取不存在的项目：报 InvalidInputError 而非 NotFoundError，
            # 说明校验也先于 project 解析
            with pytest.raises(InvalidInputError):
                await service.ask(UNKNOWN_SLUG, question, **overrides)
    finally:
        await service.aclose()

    assert spies.calls == (0, 0)


# --- c5：零持久化（四表全局计数，m17 断言 3） --------------------------------------


async def test_t29_c5_unknown_project_persists_no_project_run_step_or_tool_row(
    migrated_db_url: str, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    before = await _table_counts(session)
    service = _cold_service(migrated_db_url)
    try:
        with pytest.raises(NotFoundError):
            await service.ask(UNKNOWN_SLUG, QUESTION)
    finally:
        await service.aclose()
    after = await _table_counts(session)

    assert after == before
    assert set(after) == set(PERSISTED_TABLES)


# --- c6 / c6b：API 层（404 与 422 都不得构造工厂） ---------------------------------


async def test_t29_c6_api_unknown_project_returns_404_without_constructing_factories(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spies = _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    service = _cold_service(migrated_db_url)
    try:
        async with _client(service) as client:
            response = await client.post(
                "/ask", json={"project": UNKNOWN_SLUG, "question": QUESTION}
            )
    finally:
        await service.aclose()

    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "error_id"}
    assert body["error"]["code"] == "NOT_FOUND" and body["error"]["error_id"]
    assert spies.calls == (0, 0)
    for forbidden in _forbidden_tokens(migrated_db_url):
        assert forbidden not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"project": "Bad Slug!", "question": QUESTION},
        {"project": UNKNOWN_SLUG, "question": QUESTION, "top_k": 13},
    ],
)
async def test_t29_c6b_api_invalid_input_returns_422_without_constructing_factories(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    spies = _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    service = _cold_service(migrated_db_url)
    try:
        async with _client(service) as client:
            response = await client.post("/ask", json=body)
    finally:
        await service.aclose()

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INPUT"
    assert spies.calls == (0, 0)


# --- c7：重复 fail-fast 无累积，且懒加载仍只发生一次 -------------------------------


async def test_t29_c7_repeated_fail_fast_keeps_zero_then_lazy_init_still_happens_once(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug = await _seed_project(migrated_db_url)
    spies = _install_spies(monkeypatch, script=[*SUCCESS_SCRIPT, *SUCCESS_SCRIPT])
    service = _cold_service(migrated_db_url)
    try:
        for _ in range(3):
            with pytest.raises(NotFoundError):
                await service.ask(UNKNOWN_SLUG, QUESTION)
        assert spies.calls == (0, 0)

        assert (await service.ask(slug, QUESTION))["mode"] == "full"
        assert spies.calls == (1, 1)

        # 第二次合法请求命中懒加载缓存：不得重复构造
        assert (await service.ask(slug, QUESTION))["mode"] == "full"
        assert spies.calls == (1, 1)
    finally:
        await service.aclose()


# --- c8：fail-closed 负例 —— 注入形态下同一断言空洞 --------------------------------


async def test_t29_c8_injected_fakes_make_zero_count_assertion_vacuous(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """与 c1 完全相同的 (0, 0) 断言，在**成功路径**上也成立 → 计数本身不证明顺序。

    这正是既有 test_service.py:82 / test_api.py:94 无法捕获 RT-23 的机器证明：
    只有冷 service（本文件其余用例）才让该断言具备判别力。
    """
    slug = await _seed_project(migrated_db_url)
    spies = _install_spies(monkeypatch, script=SUCCESS_SCRIPT)
    service = AppService(
        _settings(migrated_db_url),
        embedder=FakeEmbedder(),
        llm=FakeLLM(list(SUCCESS_SCRIPT), model="deepseek-v4-flash"),
        count_tokens=approx_token_counter,
    )
    try:
        answer = await service.ask(slug, QUESTION)
    finally:
        await service.aclose()

    assert answer["mode"] == "full"
    assert spies.calls == (0, 0)
