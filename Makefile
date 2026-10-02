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

comma     := ,

.DEFAULT_GOAL := help
.PHONY: help bootstrap lint format format-check typecheck test coverage \
        audit audit-vulns audit-dead audit-deps audit-secrets check clean \
        secrets-baseline

help:
	@echo 'kwsed development checks'
	@echo
	@echo '  make bootstrap     install the project and the pinned toolchain'
	@echo '  make lint          ruff check          (linting)'
	@echo '  make format        ruff format         (rewrites files)'
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

# Formatting is enforced. The tree was reformatted once, as its own commit, so
# the diff stayed reviewable instead of arriving as a surprise inside some
# later, unrelated change.
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
	$(MAKE) --no-print-directory coverage-floor

coverage:
	$(PYTHON) -m pytest --cov-report=term-missing

# A floor on the pure-logic modules, which are the ones where an untested
# branch is a wrong answer rather than a missing feature: a parser that
# accepts bad input does not look broken, it looks like data.
#
# These currently sit between 96% and 100%, so the floor below is well
# under all of them. It is 92 rather than the 95 it could have been because
# kws/banks.py is at 96.3% -- barely three missed opportunities -- and 95%
# would have left roughly one statement of slack there. A floor one
# statement from failing is a floor that gets deleted.
#
# kws/bridge.py and kws/alsa.py are deliberately NOT in this list. Much of
# what is uncovered in them needs a real MIDI port or a real ALSA
# sequencer, so a floor there would only measure what the test doubles can
# reach. Their code is instead gated by the 70% total above.
LOGIC_MODULES := kws/messages.py,kws/banks.py,kws/records.py,kws/dumps.py,kws/multi.py,kws/deps.py,kws/catalog.py
LOGIC_FLOOR := 92

coverage-floor:
	@# Looped rather than one --include over the lot, because coverage's
	@# --fail-under compares the *total* of whatever report it printed. One
	@# call over all seven would pass while one of them sat at 60%.
	@for mod in $(subst $(comma), ,$(LOGIC_MODULES)); do \
	    $(VENV)/bin/python -m coverage report --include=$$mod \
	        --fail-under=$(LOGIC_FLOOR) >/dev/null 2>&1 || { \
	        echo "coverage floor: $$mod is below $(LOGIC_FLOOR)%"; \
	        $(VENV)/bin/python -m coverage report --include=$$mod; \
	        exit 1; }; \
	done
	@echo 'coverage floor: every logic module at or above $(LOGIC_FLOOR)%'
	@$(VENV)/bin/python -m coverage report --include=$(LOGIC_MODULES) \
	    --skip-covered | tail -n 9

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
	$(VULTURE) --min-confidence 80 $(SRC)

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
	@echo '== format =='    && $(MAKE) --no-print-directory format-check
	@echo '== typecheck ==' && $(MAKE) --no-print-directory typecheck
	@echo '== test =='      && $(MAKE) --no-print-directory test
	@echo '== audit =='     && $(MAKE) --no-print-directory audit
	@echo
	@echo 'all checks passed.'

clean:
	rm -rf .coverage .pytest_cache .mypy_cache .ruff_cache htmlcov
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +