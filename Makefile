# kwsed -- development checks.
#
# Everything here runs against .venv, which the README tells you to create
# with --system-site-packages so the system python-rtmidi is visible. Every
# target therefore goes through .venv/bin rather than PATH, because the tools
# are installed there and nowhere else.
#
#   make bootstrap   install the project and the pinned toolchain
#   make lint        ruff check
#   make format      ruff format
#   make typecheck   mypy, against a baseline of pre-existing findings
#   make test        pytest with coverage
#   make audit       pip-audit, vulture, deptry, detect-secrets
#   make check       all of the above, in order, stopping at the first failure

VENV      := .venv
PYTHON    := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
RUFF      := $(VENV)/bin/ruff
MYPY      := $(VENV)/bin/mypy
VULTURE   := $(VENV)/bin/vulture
PIP_AUDIT := $(VENV)/bin/pip-audit
DETECT    := $(VENV)/bin/detect-secrets

# Scanned source. kws/data is the generated name catalog and is excluded by
# ruff's extend-exclude and by detect-secrets' own filters.
SRC       := kws kwsed tools tests quality

# vulture 2.x has no --whitelist flag: a whitelist is supplied by including
# the file among the scanned paths, so it belongs here.
WHITELIST := quality/vulture_whitelist.py

.DEFAULT_GOAL := help
.PHONY: help bootstrap lint format format-check typecheck test coverage \
        audit audit-vulns audit-dead audit-deps audit-secrets check clean \
        secrets-baseline

help:
	@echo 'kwsed development checks'
	@echo
	@echo '  make bootstrap     install the project and the pinned toolchain'
	@echo '  make lint          ruff check          (linting)'
	@echo '  make format        ruff format         (rewrites files; NOT run by check)'
	@echo '  make format-check  ruff format --check (fails instead of rewriting)'
	@echo '  make typecheck     mypy, vs quality/mypy-baseline.txt'
	@echo '  make test          pytest + coverage (term-missing)'
	@echo '  make audit         pip-audit, vulture, deptry, detect-secrets'
	@echo '  make check         everything above; any failure stops the run'
	@echo
	@echo 'See docs/QUALITY_CHECKS.md for what is suppressed and why.'

bootstrap:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt
	$(PIP) install -e .
	@echo 'toolchain ready.'

# --- linting and formatting --------------------------------------------------

lint:
	$(RUFF) check $(SRC)

format:
	$(RUFF) format $(SRC)

# NOT part of `make check`, on purpose.
#
# The tree has never been run through a formatter: 28 of 30 files disagree
# with ruff's (black-derived) layout, mostly on continuation style. Enforcing
# it would mean rewriting the whole codebase in one commit, which is a decision
# for the owner rather than a lint fix. `make format` will do it whenever that
# is wanted -- as its own commit, so the diff stays reviewable.
#
# The gap this leaves: a new badly formatted file passes `make check`.
# `make format-check` closes that, at the cost above.
format-check:
	$(RUFF) format --check $(SRC)

# --- type checking -----------------------------------------------------------
#
# mypy has no baseline of its own, and both built-in alternatives are worse:
# ignore_errors hides new errors of any kind, and per-module disable_error_code
# cannot tell "always was there" from "new, same code". So quality/check_mypy.py
# records each known finding as file|code|message and fails only on a finding
# that is not on the list. Regenerate after deliberately accepting new debt:
#
#     make typecheck-baseline
typecheck:
	$(PYTHON) quality/check_mypy.py

.PHONY: typecheck-baseline
typecheck-baseline:
	$(PYTHON) quality/check_mypy.py --write

# --- tests -------------------------------------------------------------------

test:
	$(PYTHON) -m pytest

coverage:
	$(PYTHON) -m pytest --cov-report=term-missing

# --- audit -------------------------------------------------------------------

# Audits the dependency closure this project actually installs -- the runtime
# dependencies in pyproject.toml plus the pinned toolchain -- rather than
# `pip-audit --local`. This venv is created with --system-site-packages, so
# --local would audit several hundred unrelated host packages and report
# vulnerabilities in the host's own pip and setuptools.
audit: audit-vulns audit-dead audit-deps audit-secrets

audit-vulns:
	$(PIP_AUDIT) -r requirements-dev.txt --progress-spinner off

audit-dead:
	$(VULTURE) --min-confidence 80 $(SRC) $(WHITELIST)

audit-deps:
	$(VENV)/bin/deptry .

audit-secrets:
	$(DETECT) scan --baseline .secrets.baseline \
	    --exclude-files '^\.venv/' \
	    --exclude-files '^kws/data/' \
	    --exclude-files '^\.pytest_cache/'

# Regenerate .secrets.baseline after reviewing the diff. Review it: this file
# is what tells the next person which strings were considered and dismissed.
# detect-secrets only walks git-tracked files, so the exclusions below are
# belt-and-braces rather than load-bearing.
secrets-baseline:
	$(DETECT) scan --baseline .secrets.baseline \
	    --exclude-files '^\.venv/' \
	    --exclude-files '^kws/data/' \
	    --exclude-files '^\.pytest_cache/'

# --- everything --------------------------------------------------------------

check:
	@echo '== lint =='      && $(MAKE) --no-print-directory lint
	@echo '== typecheck ==' && $(MAKE) --no-print-directory typecheck
	@echo '== test =='      && $(MAKE) --no-print-directory test
	@echo '== audit =='     && $(MAKE) --no-print-directory audit
	@echo
	@echo 'all checks passed.'
	@echo '(formatting is not enforced; see `make format-check` and the note' \
	      'above that target.)'

clean:
	rm -rf .coverage .pytest_cache .mypy_cache .ruff_cache htmlcov
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +