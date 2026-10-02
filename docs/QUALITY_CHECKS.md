<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Development checks

What `make check` runs, what it deliberately does not, and every finding that
is currently suppressed.

The rule this setup follows: **a check that passes today and also passes on
tomorrow's broken commit is worse than no check**, because it buys confidence
it has not earned. So nothing here is switched off wholesale to make the tree
green — every suppression is counted, and each one is scoped as narrowly as it
can be while still passing on the current code.

## Running them

```sh
make bootstrap   # install the project and the pinned toolchain
make check       # lint, typecheck, test, audit
make help        # every target
```

`make bootstrap` installs `requirements-dev.txt` into `.venv`, which the
README already tells you to create with `--system-site-packages` so the system
`python-rtmidi` is visible.

| Target | Tool | What it does |
|---|---|---|
| `lint` | ruff | `ruff check` over `kws kwsed tools tests quality` |
| `format` | ruff | `ruff format`, rewriting files |
| `format-check` | ruff | the same without rewriting — **not in `check`**, see below |
| `typecheck` | mypy | via `quality/check_mypy.py`, against a baseline |
| `test` | pytest + pytest-cov | 163 tests, coverage with `term-missing` |
| `audit-vulns` | pip-audit | the dependency closure this project installs |
| `audit-dead` | vulture | unused code at confidence ≥ 80 |
| `audit-deps` | deptry | unused / missing / misplaced dependencies |
| `audit-secrets` | detect-secrets | against `.secrets.baseline` |

`pre-commit install` wires up ruff-check, mypy, detect-secrets, shellcheck
and shfmt, with every hook rev pinned to a released tag.

## pip-audit audits a requirements file, not the environment

`.venv` is created with `--system-site-packages`, so `pip-audit --local` sees
every package installed on the host — several hundred of them, including the
host's own `pip` and `setuptools`. Auditing that produces a wall of findings
about software this project has nothing to do with, and the real signal (are
`textual` and `python-rtmidi` safe?) is lost in it.

So the target audits `requirements-dev.txt`, which resolves the closure of the
project's own dependencies plus the toolchain. The one thing it skips is
`kwsed` itself, which is not on PyPI.

## mypy has a baseline

mypy has no baseline of its own, and both built-in alternatives are worse:

- `ignore_errors` per module hides **new** errors of any kind in that module,
  which defeats the point of running a type checker at all.
- Per-module `disable_error_code` hides new errors of a whole category, and
  cannot tell "this was always there" from "this is new and happens to share a
  code".

`quality/check_mypy.py` records each existing finding as
`path | code | message` and fails only on a finding whose key is not on the
list. Line numbers are deliberately excluded, so moving code around does not
break it — a baseline that breaks on every unrelated edit gets deleted within
a week.

A finding that *disappears* is reported as stale but does not fail. Fixing
things should be easy; only new breakage should be loud.

```sh
make typecheck            # fail on anything not already known
make typecheck-baseline   # accept the current output as the new baseline
```

**Strictness.** The project runs non-strict with four flags that cost nothing
today: `warn_unused_ignores`, `warn_redundant_casts`, `no_implicit_optional`
and `warn_unreachable`.

No module is `--strict` clean — `kws/messages.py` needs 2 fixes, `kws/multi.py`
1, `kws/records.py` 2, and so on — so strict-per-module is not configured.
Making it so means editing annotated source, which is out of scope for a
tooling pass. Strict can be dialled in per module later.

## vulture needs a whitelist as a path

vulture 2.x removed `--whitelist`. A whitelist is supplied by passing the file
among the scanned paths, so `quality/vulture_whitelist.py` is on vulture's
command line in the Makefile. That makes the file real Python, so ruff lints
it too; hence the `noqa` on its one line.

## Formatting is not enforced

The tree has never been through a formatter. `ruff format` would rewrite **28
of 30 files**, mostly on continuation style. Enforcing that in `check` or in a
pre-commit hook would mean rewriting the whole codebase in one commit, which
is a decision for the owner and not a lint fix.

`make format` will do it whenever that is wanted, as its own commit so the
diff stays reviewable. Until then the gap is real and worth naming: **a badly
formatted new file passes `make check`.** `make format-check` closes that at
the cost above.

For the same reason `ruff-format` is not wired into `.pre-commit-config.yaml`
even though `ruff-check` is. The first commit touching a Python file would
otherwise come back with 26 rewritten files and be rejected.

## Suppressed findings

Every ruff rule requested is enabled. What is suppressed, and why:

### Disabled project-wide

These are properties of the whole style rather than of particular files, so a
per-file list would be longer and less honest. A **new** file is still checked
for all of them.

| Rule | Count | Why |
|---|---:|---|
| `UP006` `UP035` `UP045` `UP037` | 215 | The project uses `typing.Dict/List/Optional` consistently under `from __future__ import annotations`. Rewriting every annotation is a refactor. |
| `PLC0415` | 55 | Local imports are deliberate: `rtmidi` is imported lazily so `kwscli banks` works with no MIDI backend; the tests import inside functions for `pytest.importorskip`. |
| `PLR2004` | 96 | Protocol offsets, record sizes and function codes are compared as literals throughout. That is how binary formats read; naming every constant is a different project. |
| `PLR0911/0912/0913/0915/0917` | 14 | Complexity and argument-count refactor suggestions, not defects. |
| `RUF005` `RUF012` `RUF022` | 41 | Established conventions: `[a] + [b]`, class-level constant tables, `__all__` ordered by protocol rather than alphabetically. |
| `SIM105` | 6 | An explicit `try`/`except` reads better than `contextlib.suppress` at these sites. |
| `B905` | 3 | `zip(strict=)` changes runtime behaviour; enabling it is a decision, not a fix. |

### Disabled per file

Scoped to the codes listed, so **other** rules still fire in these files — a
long line added to `kws/records.py` is still caught even though `F401` is not.

| Scope | Codes | Why |
|---|---|---|
| `tests/*` | `S101`, `F401` | `assert` is what a test is for; `F401` covers imports used for registration or availability rather than a name. |
| `kws/bankbuild.py` | `C901`, `B007`, `B008`, `I001` | The bank planner is complex by construction; splitting it would fragment the one place the four budgets are enforced. `B008` is a frozen-dataclass default, which is immutable and therefore safe. |
| `kws/bridge.py` | `C901`, `I001`, `RUF100`, `S110`, `S112` | The write fence is complex by construction. `S110`/`S112` are the deliberate teardown and drain loops. `RUF100` covers retained `# noqa: E402` markers that are unnecessary only because E402 exempts `sys.path` manipulation. |
| `kws/favorites.py` | `E501`, `F401`, `RUF023`, `S608` | `E501` is an unsplittable SQL string. `S608` is `PRAGMA user_version = {SCHEMA_VERSION}`, an int constant. |
| `kws/messages.py` | `F401`, `PLW1641` | `Packet` defines `__eq__` and is deliberately unhashable. |
| `kws/config.py` | `PLW0603`, `UP031` | A module-level `_warned` flag for warn-once, and `"\u%04X" % ord(c)`. |
| `kwsed/app.py` | `B904`, `F841`, `I001`, `PLW0108`, `RUF059` | The Textual event layer. |
| `kwsed/cli.py` | `C416`, `C901`, `I001` | The `send` command is the destructive path and stays in one readable function. |
| `kws/{backup,banks,dumps,multi,records}.py` | `F401` | Dead `typing` imports from an earlier revision. |
| `tools/*`, remaining | `B904`, `C901`, `I001`, `RUF100` | `sys.path` bootstrap imports and argument handling. |

### Other tools

| Tool | Suppressed | Where |
|---|---:|---|
| mypy | 30 | `quality/mypy-baseline.txt` |
| vulture | 1 | `quality/vulture_whitelist.py` (at confidence 80; there are 68 at 60) |
| deptry | 0 | — |
| pip-audit | 0 | — |
| detect-secrets | 0 | `.secrets.baseline` is empty of findings |

### Worth deleting rather than suppressing

These are suppressed only because this pass does not edit application source.
Each is a safe, mechanical removal the owner may prefer to make:

- **7 dead `typing` imports** — `Dict` in `kws/backup.py` and `kws/messages.py`,
  `Optional` in `kws/banks.py`, `kws/dumps.py`, `kws/multi.py`,
  `kws/records.py`, `Iterable`/`Sequence` in `kws/favorites.py`. None is
  re-exported through `__all__`.
- **6 unused `# noqa: E402`** markers in `kws/bridge.py`,
  `tests/test_write.py` and `tools/`.
- **`import inspect`** in `tests/test_favorites.py::_loaders`, which is unused.
- **The 6 stale `# noqa: E402`** plus the `F841`/`RUF059` dead locals in
  `kwsed/app.py`, `tests/test_bankbuild.py` and `tests/test_review_fixes.py`.

Once a suppression is no longer needed, delete the entry as well as the
finding — a baseline that only ever grows stops being read.

## CI

There is no CI configuration in this repository, so none was added. When there
is, the job is one line:

```yaml
- run: make check
```