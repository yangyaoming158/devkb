"""T19.2 `POST /ask` 集成：Pydantic 校验、同步 Answer v1、项目隔离、稳定错误码。

httpx ASGITransport 直连 app（无真实网络监听）；AppService 注入 Fake 组件。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from pydantic import SecretStr

from devkb.api import create_app
from devkb.config import Settings
from devkb.embedding import FakeEmbedder
from devkb.errors import InternalError
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


def _service(db_url: str, script: list[str | Exception]) -> AppService:
    settings = Settings(llm_api_key=SecretStr("integration-test"), database_url=db_url)
    return AppService(
        settings,
        embedder=FakeEmbedder(),
        llm=FakeLLM(script, model="deepseek-v4-flash"),
        count_tokens=approx_token_counter,
    )


def _client(service: AppService) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(service))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _slug() -> str:
    return f"t19api-{uuid.uuid4().hex[:8]}"


async def test_ask_returns_answer_v1_synchronously(migrated_db_url: str) -> None:
    slug = _slug()
    service = _service(migrated_db_url, [PLAN, EVAL_OK, GEN])
    try:
        await service.ingest(slug, CORPUS_MD)
        async with _client(service) as client:
            response = await client.post(
                "/ask", json={"project": slug, "question": "库存怎么保证并发安全？"}
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        answer = response.json()
        assert answer["mode"] == "full"
        assert [c["evidence_id"] for c in answer["citations"]] == ["E1"]
        assert {"answer_text", "claims", "not_found", "stats", "run_id"} <= set(answer)
    finally:
        await service.aclose()


async def test_ask_validates_inputs_with_422(migrated_db_url: str) -> None:
    service = _service(migrated_db_url, [])
    bad_bodies = [
        {"project": "demo"},  # 缺 question
        {"project": "demo", "question": ""},
        {"project": "demo", "question": "长" * 4001},  # 问题长度上限
        {"project": "demo", "question": "q", "top_k": 0},
        {"project": "demo", "question": "q", "top_k": 13},  # top_k 上限
        {"project": "Bad Slug!", "question": "q"},  # slug 严格校验
    ]
    try:
        async with _client(service) as client:
            for body in bad_bodies:
                response = await client.post("/ask", json=body)
                assert response.status_code == 422, body
    finally:
        await service.aclose()


async def test_ask_unknown_project_returns_404_with_stable_error_body(
    migrated_db_url: str,
) -> None:
    service = _service(migrated_db_url, [])
    try:
        async with _client(service) as client:
            response = await client.post(
                "/ask", json={"project": "no-such-project", "question": "库存？"}
            )
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "NOT_FOUND" and error["error_id"]
    finally:
        await service.aclose()


class _BoomService(AppService):
    """ask 直接抛已映射的 InternalError：验证 API 层 500 错误体与 error_id 透传。"""

    async def ask(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise InternalError("内部错误（error_id=deadbeef1234）", error_id="deadbeef1234")


async def test_ask_internal_error_returns_500_with_error_id(migrated_db_url: str) -> None:
    settings = Settings(llm_api_key=SecretStr("integration-test"), database_url=migrated_db_url)
    service = _BoomService(settings)
    try:
        async with _client(service) as client:
            response = await client.post("/ask", json={"project": "demo", "question": "库存？"})
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "INTERNAL_ERROR" and error["error_id"] == "deadbeef1234"
    finally:
        await service.aclose()


async def test_ask_database_unavailable_returns_503_without_leaking_dsn() -> None:
    # 端口 9（discard）永不监听；口令不得出现在错误体
    service = _service("postgresql+asyncpg://devkb:supersecretpw@127.0.0.1:9/devkb", [])
    try:
        async with _client(service) as client:
            response = await client.post("/ask", json={"project": "demo", "question": "库存？"})
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["code"] == "DATABASE_ERROR" and error["error_id"]
        assert "supersecretpw" not in response.text
    finally:
        await service.aclose()


async def test_get_run_returns_steps_tools_and_answer(migrated_db_url: str) -> None:
    slug = _slug()
    service = _service(migrated_db_url, [PLAN, EVAL_OK, GEN])
    try:
        await service.ingest(slug, CORPUS_MD)
        async with _client(service) as client:
            asked = await client.post(
                "/ask", json={"project": slug, "question": "库存怎么保证并发安全？"}
            )
            run_id = asked.json()["run_id"]
            response = await client.get(f"/runs/{run_id}", params={"project": slug})
        assert response.status_code == 200
        payload = response.json()
        run = payload["run"]
        assert run["run_id"] == run_id and run["status"] == "succeeded"
        assert run["answer"]["mode"] == "full"
        nodes = [step["node"] for step in payload["steps"]]
        assert "plan" in nodes and "retrieve" in nodes and "finalize" in nodes
        assert any(step["tools"] for step in payload["steps"])  # 检索工具调用可见
    finally:
        await service.aclose()


async def test_get_run_isolation_and_strict_params(migrated_db_url: str) -> None:
    slug_a, slug_b = _slug(), _slug()
    service = _service(migrated_db_url, [PLAN, EVAL_OK, GEN])
    try:
        await service.ingest(slug_a, CORPUS_MD)
        await service.ingest(slug_b, CORPUS_MD)
        async with _client(service) as client:
            asked = await client.post(
                "/ask", json={"project": slug_a, "question": "库存怎么保证并发安全？"}
            )
            run_id = asked.json()["run_id"]
            cross = await client.get(f"/runs/{run_id}", params={"project": slug_b})
            assert cross.status_code == 404  # 跨项目不可见，不泄漏存在性
            assert cross.json()["error"]["code"] == "NOT_FOUND"

            bad_uuid = await client.get("/runs/not-a-uuid", params={"project": slug_a})
            assert bad_uuid.status_code == 422  # run_id 严格校验

            no_project = await client.get(f"/runs/{run_id}")
            assert no_project.status_code == 422  # project 必填

            unknown = await client.get(f"/runs/{run_id}", params={"project": "no-such-project"})
            assert unknown.status_code == 404
    finally:
        await service.aclose()


class _NoModelService(AppService):
    """healthz 判据：探测路径绝不加载模型/构造 LLM 客户端。"""

    def _get_embedder(self) -> FakeEmbedder:
        raise AssertionError("healthz 不得加载嵌入模型")

    def _get_llm(self) -> FakeLLM:
        raise AssertionError("healthz 不得构造 LLM 客户端")


async def test_healthz_reports_migration_without_model_load(migrated_db_url: str) -> None:
    settings = Settings(llm_api_key=SecretStr("healthz-secret-key"), database_url=migrated_db_url)
    service = _NoModelService(settings)
    try:
        async with _client(service) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok" and payload["database"] == "ok"
        assert payload["migration"]  # 迁移版本非空
        assert "healthz-secret-key" not in response.text
        assert "postgresql" not in response.text  # 不回显 DSN/配置
    finally:
        await service.aclose()


async def test_healthz_database_down_returns_503(migrated_db_url: str) -> None:
    service = _service("postgresql+asyncpg://devkb:supersecretpw@127.0.0.1:9/devkb", [])
    try:
        async with _client(service) as client:
            response = await client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DATABASE_ERROR"
        assert "supersecretpw" not in response.text
    finally:
        await service.aclose()
