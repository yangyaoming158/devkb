.PHONY: up down workflow-check preflight verify-task verify-full lint fmt typecheck test ci eval-ci

PACKET ?=

up:
	docker compose up -d

down:
	docker compose down

workflow-check:
	uv run python scripts/workflow_guard.py validate-repo

preflight:
	uv run python scripts/workflow_guard.py preflight $(if $(PACKET),--packet "$(PACKET)",)

verify-task:
	@test -n "$(PACKET)" || (echo "PACKET is required: make verify-task PACKET=docs/tasks/<task>.md"; exit 2)
	uv run python scripts/workflow_guard.py verify-task --packet "$(PACKET)"

verify-full:
	@test -n "$(PACKET)" || (echo "PACKET is required: make verify-full PACKET=docs/tasks/<task>.md"; exit 2)
	uv run python scripts/workflow_guard.py verify-task --packet "$(PACKET)" --require-clean
	$(MAKE) ci
	$(MAKE) eval-ci

lint:
	uv run ruff format --check src tests scripts
	uv run ruff check src tests scripts

fmt:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

typecheck:
	uv run pyright
	uv run pyright scripts/workflow_guard.py

test:
	uv run pytest -q

ci: workflow-check lint typecheck test

# Evaluation v1 §6 确定性回归（Fake + fixture + 真实测试 PG；不装 ml、不触网）：
# RRF golden / FTS·HNSW 机制 / graph 路径矩阵 / 指标手算 / harness / 已提交报告校验。
# unit 与 integration 分两次调用：两目录 conftest 同名，混合子集会解析串位
eval-ci:
	uv run pytest -q tests/unit/test_rrf.py tests/unit/test_fts_golden.py \
		tests/unit/test_agent_graph.py tests/unit/test_agent_policy_recall.py \
		tests/unit/test_eval_metrics.py \
		tests/unit/test_eval_reports_schema.py tests/unit/test_holdout_ledger.py
	uv run pytest -q tests/integration/test_retrieval.py tests/integration/test_eval_harness.py
