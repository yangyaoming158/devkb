"""T19.4 并发边界（承接 Evaluation-v1 §5.3 第 11 类）：

- 同步 embedding 移入线程并受进程内 semaphore（默认 1）限流——并发请求可观察上限；
- 事件循环不被同步 embedding 阻塞——慢 embedding 进行中 /healthz 仍即时响应；
- 客户端断开（连接取消）后 run 仍到终态可查询（/ask 路由 shield）。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from devkb.api import create_app
from devkb.config import Settings
from devkb.embedding import FakeEmbedder
from devkb.ingest.markdown import approx_token_counter
from devkb.llm import LLMResult
from devkb.service import AppService

CORPUS_MD = Path(__file__).parents[1] / "fixtures" / "corpus_md"

PLAN = '{"intent":"knowledge_qa","queries":["库存 并发"]}'
EVAL_OK = '{"sufficiency":"sufficient","supported_aspects":["库存"],"missing_aspects":[]}'
GEN = (
    '{"answer_text":"库存并发由行锁保证 [E1]。","claims":[{"text":"库存并发由行锁保证",'
    '"evidence_ids":["E1"],"quotes":[]}],"not_found":[]}'
)

EMBED_DELAY_S = 0.5


class _ProbeEmbedder(FakeEmbedder):
    """embed_query 慢（线程内 sleep）+ 并发计数：观察 semaphore 上限与线程化。"""

    def __init__(self, delay_s: float = EMBED_DELAY_S) -> None:
        super().__init__()
        self._delay_s = delay_s
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self._delay_s)
        with self._lock:
            self.active -= 1
        return super().embed_query(text)


class _RoutedLLM:
    """按 system prompt 路由的并发安全 FakeLLM：多 run 交错时各调用各得其所。"""

    # T31.2R-a：`_structured_call` 一律传 `json_mode=True`，替身必须接住它，
    # 否则 TypeError 会被当成 request_failed，测试绿着但 agent 已全降级。
    async def complete(self, *, system: str, user: str, json_mode: bool = False) -> LLMResult:
        del json_mode
        if "查询规划器" in system:
            text = PLAN
        elif "充分性评估器" in system:
            text = EVAL_OK
        else:
            text = GEN
        return LLMResult(
            text=text,
            model="deepseek-v4-flash",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )


def _service(db_url: str, probe: _ProbeEmbedder) -> AppService:
    settings = Settings(llm_api_key=SecretStr("integration-test"), database_url=db_url)
    return AppService(settings, embedder=probe, llm=_RoutedLLM(), count_tokens=approx_token_counter)


def _client(service: AppService) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(service))
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=httpx.Timeout(30.0)
    )


def _slug() -> str:
    return f"t19c-{uuid.uuid4().hex[:8]}"


async def test_concurrent_asks_observe_semaphore_cap_and_loop_stays_responsive(
    migrated_db_url: str,
) -> None:
    slug = _slug()
    probe = _ProbeEmbedder()
    service = _service(migrated_db_url, probe)
    try:
        await service.ingest(slug, CORPUS_MD)  # embed_documents 不经探针慢路径
        body = {"project": slug, "question": "库存怎么保证并发安全？"}
        async with _client(service) as client:
            ask_tasks = [asyncio.create_task(client.post("/ask", json=body)) for _ in range(3)]
            await asyncio.sleep(0.1)  # 首个 embedding 已在线程中 sleep

            started = time.perf_counter()
            health = await client.get("/healthz")
            health_elapsed = time.perf_counter() - started
            # 事件循环未被阻塞：慢 embedding 进行中 healthz 即时返回
            assert health.status_code == 200
            assert health_elapsed < EMBED_DELAY_S / 2
            assert any(not task.done() for task in ask_tasks)

            responses = await asyncio.gather(*ask_tasks)
        assert [r.status_code for r in responses] == [200, 200, 200]
        assert {r.json()["mode"] for r in responses} == {"full"}
        # 三个并发请求各至少 1 次 embedding，但 semaphore 上限 1 从未被突破
        assert probe.calls >= 3
        assert probe.max_active == 1
    finally:
        await service.aclose()


async def test_client_disconnect_still_reaches_terminal_run_state(migrated_db_url: str) -> None:
    slug = _slug()
    probe = _ProbeEmbedder()
    service = _service(migrated_db_url, probe)
    try:
        await service.ingest(slug, CORPUS_MD)
        body = {"project": slug, "question": "库存怎么保证并发安全？"}
        async with _client(service) as client:
            task = asyncio.create_task(client.post("/ask", json=body))
            await asyncio.sleep(0.2)  # run 已建立、embedding 进行中
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # shield 使处理在断开后继续：轮询直到 run 落到终态
            terminal: dict[str, Any] | None = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                runs = await service.list_runs(slug)
                if runs and runs[0]["status"] != "running":
                    terminal = runs[0]
                    break
                await asyncio.sleep(0.05)
        assert terminal is not None, "断开后 run 未在时限内到达终态"
        assert terminal["status"] == "succeeded"
    finally:
        await service.aclose()


class _SlowLoadService(AppService):
    """未注入 embedder：走真实懒加载路径，_load_embedder 慢 0.5s 并计数。"""

    load_calls = 0

    def _load_embedder(self) -> FakeEmbedder:
        type(self).load_calls += 1
        time.sleep(EMBED_DELAY_S)
        return FakeEmbedder()


async def test_lazy_model_load_runs_in_thread_and_initializes_once(migrated_db_url: str) -> None:
    slug = _slug()
    seed = _service(migrated_db_url, _ProbeEmbedder(delay_s=0.0))
    try:
        await seed.ingest(slug, CORPUS_MD)
    finally:
        await seed.aclose()

    settings = Settings(llm_api_key=SecretStr("integration-test"), database_url=migrated_db_url)
    _SlowLoadService.load_calls = 0
    service = _SlowLoadService(settings, llm=_RoutedLLM(), count_tokens=approx_token_counter)
    try:
        body = {"project": slug, "question": "库存怎么保证并发安全？"}
        async with _client(service) as client:
            ask_tasks = [asyncio.create_task(client.post("/ask", json=body)) for _ in range(2)]
            await asyncio.sleep(0.1)  # 首问已进入线程中的模型加载

            started = time.perf_counter()
            health = await client.get("/healthz")
            health_elapsed = time.perf_counter() - started
            # 事件循环未被首问模型加载阻塞
            assert health.status_code == 200
            assert health_elapsed < EMBED_DELAY_S / 2
            assert any(not task.done() for task in ask_tasks)

            responses = await asyncio.gather(*ask_tasks)
        assert [r.status_code for r in responses] == [200, 200]
        # 并发首问只加载一次（asyncio.Lock 双检）
        assert _SlowLoadService.load_calls == 1
    finally:
        await service.aclose()
