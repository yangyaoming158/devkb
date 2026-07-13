.PHONY: up down lint fmt typecheck test ci

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
