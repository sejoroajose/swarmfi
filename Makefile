.PHONY: setup build start stop health test test-unit test-integration lint type-check clean

# ── Setup ──────────────────────────────────────────────────────────────────────
setup:
	@bash scripts/setup.sh

# ── AXL nodes ─────────────────────────────────────────────────────────────────
start:
	@bash scripts/start_nodes.sh

stop:
	@bash scripts/stop_nodes.sh

health:
	@bash scripts/health_check.sh

# ── Tests ──────────────────────────────────────────────────────────────────────
test-unit:
	python3 -m pytest tests/ -m unit -v

test-integration: health
	python3 -m pytest tests/ -m integration -v

test: test-unit test-integration

# ── Code quality ──────────────────────────────────────────────────────────────
lint:
	ruff check agents/ core/ tests/

type-check:
	mypy agents/ core/

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean: stop
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache axl/logs/