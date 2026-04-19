.PHONY: help lint lint-antipatterns lint-js lint-md lint-asvs lint-notice lint-i18n format security complexity test test-unit test-e2e persona-audit persona-audit-report tracker coverage docs docs-check sbom ci clean deps

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Python code quality ───────────────────────────────────
lint:  ## Run ruff check + ruff format --check (matches the pre-commit hooks)
	ruff check skiff/ app.py tests/ tools/
	ruff format --check skiff/ app.py tests/ tools/

lint-antipatterns:  ## AP001-AP014 project-specific anti-pattern scan (py + js + md)
	python3 tools/lint_antipatterns.py --check skiff/ docs/

format:  ## Format + auto-fix Python
	ruff format skiff/ app.py tests/ tools/
	ruff check --fix skiff/ app.py tests/ tools/

security:  ## ruff security rules (bandit equivalent) + pip-audit
	ruff check --select S --ignore S110,S105,S603,S607 skiff/ app.py
	pip-audit --strict -r requirements.txt

security-scan:  ## Dynamic + extended static scans (semgrep + trivy + ZAP). Requires docker.
	# Runs the same scanners CI runs, locally, against a throwaway
	# SKIFF instance on port 18300. Reproduces the weekly CI run so
	# a contributor can verify a finding before opening a PR.
	# See docs/hardening/security-scans.md for triage playbook.
	@command -v docker >/dev/null || { echo "security-scan requires docker"; exit 1; }
	@./scripts/security-scan.sh

complexity:  ## Cyclomatic complexity (McCabe) + pylint heuristics (matches pyproject.toml ignores)
	# pyproject.toml enables C90 and the non-PLR2004 PLR rules across
	# the whole lint pass. This target re-runs the same rule set in
	# isolation so a contributor can audit just complexity hotspots
	# without the rest of the ruff output. Keep it aligned with the
	# `[tool.ruff.lint.ignore]` list — otherwise policy lives in two
	# places and `make complexity` drifts from `make lint`.
	ruff check --select C90,PLR --ignore PLR2004,PLR0913,PLR0915 skiff/ app.py

# ── JS + Markdown quality ─────────────────────────────────
lint-js:  ## Guard: no innerHTML string interpolation outside ui.js
	@bad=$$(git grep -n -E 'innerHTML\s*=\s*([`"'\''"]).*\$$\{|innerHTML\s*=\s*[^;]+\+' -- 'skiff/static/**/*.js' ':!skiff/static/ui.js' ':(exclude)skiff/static/swagger-ui/' ':(exclude)skiff/static/xterm/' || true); \
	if [ -n "$$bad" ]; then \
	  echo "::error::innerHTML with interpolation outside ui.js — XSS risk:"; \
	  echo "$$bad"; \
	  exit 1; \
	fi; \
	echo "lint-js: no innerHTML interpolation found outside ui.js"

lint-md:  ## Check every internal markdown link resolves
	python3 tools/check_md_links.py --check

lint-i18n:  ## Advisory: report JS literals that should route through t() (not a CI gate)
	@python3 scripts/lint-untranslated-strings.py

lint-asvs:  ## Verify SECURITY.md ASVS v5.0 self-assessment is complete (V1–V18)
	python3 tools/check_asvs_coverage.py --check

lint-notice:  ## Verify NOTICE attributes every direct runtime dep in pyproject.toml
	python3 tools/check_notice_coverage.py --check

# ── Tests ─────────────────────────────────────────────────
test:  ## Run full unit + property test suite
	pytest -v -m "not e2e" tests/

test-unit:  ## Run just the unit tier (no Docker daemon needed)
	pytest -v -m unit tests/

test-e2e:  ## Run Playwright e2e tests (requires .[e2e] + chromium)
	pytest -v -m e2e tests/

persona-audit:  ## Run the driver-seat persona audit harness (see docs/dev/persona_audit_tracker.md)
	# The harness captures screenshots / DOM / console / stderr / audit
	# per step, cross-checks against docs + competitor matrix + zero-trust
	# invariants, and emits finding.json entries under
	# tests/e2e-artifacts/persona-audit/pass-<N>/. Run repeatedly — each
	# invocation is one pass; the done rubric demands 2 consecutive clean
	# passes + all 16 gates green.
	#
	# Override PERSONA=<tag> and JOURNEY=<name> to drive a slice.
	pytest -v -m "persona_audit" tests/journeys/ \
	  $(if $(PERSONA),-k "$(PERSONA)") \
	  $(if $(JOURNEY),-k "$(JOURNEY)")

persona-audit-report:  ## Write docs/dev/persona_audit_report_<date>.md from the latest pass
	python3 scripts/persona_audit_report.py

tracker:  ## Refresh docs/dev/persona_audit_tracker.md + backing CSVs
	python3 scripts/regenerate_tracker.py

coverage:  ## Coverage report (HTML + term-missing), excludes e2e
	pytest --cov --cov-report=term-missing --cov-report=html -m "not e2e" tests/

# ── Docs ──────────────────────────────────────────────────
docs:  ## Regenerate every auto-generated doc (errors, audit-events, config-knobs, features)
	python3 tools/gen_catalogues.py
	python3 tools/gen_feature_docs.py

docs-check:  ## Fail if any auto-generated doc drifted from source
	python3 tools/gen_catalogues.py --check
	python3 tools/gen_feature_docs.py --check
	python3 tools/check_md_links.py --check

# ── Supply chain ──────────────────────────────────────────
deps:  ## Regenerate hash-pinned requirements.txt from pyproject.toml
	pip-compile --generate-hashes --strip-extras -o requirements.txt pyproject.toml

sbom:  ## Regenerate sbom.cdx.json (CycloneDX) from the current package metadata
	# Uses `cyclonedx-py requirements` so the SBOM only covers SKIFF's
	# pinned runtime deps — not every editable-installed project in
	# the developer's venv. The CI workflow (`.github/workflows/security.yml`)
	# runs the authoritative Anchore Syft SBOM; this target is for
	# locally regenerating the committed-ish artefact before a release tag.
	@which cyclonedx-py >/dev/null 2>&1 || pip install --quiet cyclonedx-bom
	cyclonedx-py requirements requirements.txt -o sbom.cdx.json
	@echo "wrote sbom.cdx.json (gitignored; release-time artefact only)"

# ── Aggregate ─────────────────────────────────────────────
# Matches .github/workflows/ci.yml. `test` runs the unit tier without the
# coverage gate so a local run finishes fast; `coverage` mirrors what CI
# enforces (fail_under = 90) and is what a contributor should run before
# pushing a PR.
ci:  ## Everything CI runs: lint + lint-antipatterns + lint-js + lint-md + lint-asvs + lint-notice + security + docs-check + coverage
	$(MAKE) lint
	$(MAKE) lint-antipatterns
	$(MAKE) lint-js
	$(MAKE) lint-md
	$(MAKE) lint-asvs
	$(MAKE) lint-notice
	$(MAKE) security
	$(MAKE) docs-check
	$(MAKE) coverage

clean:  ## Remove build artefacts + cache dirs
	rm -rf .pytest_cache .ruff_cache htmlcov __pycache__ build dist
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
