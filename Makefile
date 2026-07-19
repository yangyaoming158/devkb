.PHONY: up down lint fmt typecheck test ci eval-ci

up:
	docker compose up -d

down:
	docker compose down

lint:
	uv run ruff format --check src tests
	uv run ruff check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run pyright

test:
	uv run pytest -q

ci: lint typecheck test

# Evaluation v1 §6 确定性回归（Fake + fixture + 真实测试 PG；不装 ml、不触网）：
# RRF golden / FTS·HNSW 机制 / graph 路径矩阵 / 指标手算 / harness / 已提交报告校验。
# unit 与 integration 分两次调用：两目录 conftest 同名，混合子集会解析串位
eval-ci:
	uv run pytest -q tests/unit/test_rrf.py tests/unit/test_fts_golden.py \
		tests/unit/test_agent_graph.py tests/unit/test_eval_metrics.py \
		tests/unit/test_eval_reports_schema.py
	uv run pytest -q tests/integration/test_retrieval.py tests/integration/test_eval_harness.py
