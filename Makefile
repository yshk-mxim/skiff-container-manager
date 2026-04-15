.PHONY: help lint format security complexity test test-unit coverage clean ci

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

lint:  ## Run ruff linter
	ruff check skiff/ app.py tests/

format:  ## Format and auto-fix with ruff
	ruff format skiff/ app.py tests/
	ruff check --fix skiff/ app.py tests/

security:  ## Run ruff security rules (bandit equivalent)
	ruff check --select S --ignore S110,S105,S603,S607 skiff/app.py

complexity:  ## Check cyclomatic complexity
	ruff check --select C90,PLR skiff/app.py

test:  ## Run all unit tests
	pytest -v tests/

test-unit:  ## Run unit tests (no Docker daemon required)
	pytest -v -m unit tests/

coverage:  ## Generate coverage report
	pytest --cov --cov-report=term-missing --cov-report=html tests/

clean:  ## Remove build artifacts and caches
	rm -rf .pytest_cache .ruff_cache htmlcov __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

test-e2e:  ## Run Playwright e2e tests (pip install -e .[e2e] && playwright install chromium first)
	pytest -v -m e2e tests/

ci:  ## CI pipeline — lint + security + unit tests
	$(MAKE) lint
	$(MAKE) security
	$(MAKE) test-unit
