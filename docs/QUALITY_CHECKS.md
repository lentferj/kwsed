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
| `format-check` | ruff | the same without rewriting; run by `check` |
| `typecheck` | mypy | `--strict`, via `quality/check_mypy.py` |
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

`vinsynlib` is skipped by the same route, and deliberately. It is a sibling
checkout rather than a package on an index (see `[tool.uv.sources]` in
`pyproject.toml`), so `pip` cannot resolve it and `pip-audit` would fail
resolving rather than audit anything. It is this family's own source, reviewed
in this repository, and it carries no third-party dependency of its own at
this layer — so there is nothing here for an index to say is vulnerable. If it
is ever published, this note is the thing to revisit.

## mypy runs strict, behind a baseline that is empty

The checker exists because mypy has no baseline of its own, and both built-in
alternatives are worse:

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

**Strictness.** The project runs `strict = true`. mypy --strict passes on all
28 source files, so the baseline above is currently **empty**: there is nothing
being tolerated.

One flag is turned off outside the library, by an explicit list of module names
in `pyproject.toml` — `disallow_untyped_defs` and `disallow_untyped_calls`, for
`tests/` and `tools/` only. The line drawn is annotation debt against checking
value:

- Annotating a test is close to no work. `def test_it_behaves():` says nothing
  a type checker did not already learn from the assert, and requiring `-> None`
  on 240 of them buys noise rather than checking.
- Inside the library it is the opposite. A Textual callback or a bridge method
  reached only by the framework is exactly where a wrong signature goes
  unnoticed, because nothing calls it in a way mypy can see.

The two flags have to move together. Allowing an untyped def while still
forbidding calls to it makes every test that calls a local helper report the
helper instead of the code under test.

The module names are listed rather than wildcarded on purpose. mypy accepts
`test_*` as matching without applying it, and emits no "unused section" note to
say so — a wildcard would have looked like it was working. An explicit list
cannot fail quietly, and a new test file is simply checked strictly, which is
the safe direction to fail in.

## vulture needs no whitelist

vulture 2.x removed `--whitelist`; a whitelist is supplied by passing the file
among the scanned paths, which meant a real Python file that ruff then had to
be persuaded to lint. There was one entry in it — an unused `import inspect`
in a test — and deleting that import deleted the file, the `WHITELIST`
variable in the Makefile, and the `noqa` that made the workaround lintable.

vulture now runs on the source tree alone and reports nothing at confidence
80. At confidence 60 it would report 68 items, which is below the bar worth
holding a codebase to.

## Formatting is enforced

`ruff format` is run by `make check` (`format-check`) and wired into
pre-commit as `ruff-format`.

Adopting it rewrote **28 of 30 files** on a single occasion, almost entirely on
continuation style. That was done as its own commit, so the diff stayed
reviewable rather than arriving as a surprise inside a later change — which is
why the rule is worth stating: a whole-tree reformat is a commit of its own, not
something to discover inside a feature.

Re-measuring every ruff rule against the formatted tree gave the same 720
findings with byte-identical per-rule counts, so adopting the formatter moved
no suppression and needed no re-baselining. None of the 720 was a finding about
layout — `E501` is the one rule that is, and there was still exactly one of
it. The count is 786 today: mypy strict added annotations, which are
themselves findings, and deleting the mechanical suppressions took 19 back off
again.

The mypy baseline came through the reformat unchanged, which was the first real
test of keying findings on `path | code | message` and leaving line numbers out.
A baseline keyed on line numbers would have needed regenerating, and would
break again on the next edit that shifts a line.

## Suppressed findings

Every ruff rule requested is enabled. What is suppressed, and why:

### Disabled project-wide

These are properties of the whole style rather than of particular files, so a
per-file list would be longer and less honest. A **new** file is still checked
for all of them.

**786 findings in total: 502 project-wide, 284 per-file.** (The total rose
from 720 when mypy strict was turned on, because the annotations it required
are themselves `UP006`/`UP045`/`UP035` findings. Suppressing those rules is
what stops that from being a loop. It then fell back by 19 as the
mechanical suppressions were deleted rather than kept.)

| Rule | Count | Why |
|---|---:|---|
| `UP006` `UP035` `UP045` `UP037` | 288 | The project uses `typing.Dict/List/Optional` consistently under `from __future__ import annotations`. Rewriting every annotation is a refactor, and mypy strict made 73 more of them. |
| `PLC0415` | 56 | Local imports are deliberate: `rtmidi` is imported lazily so `kwscli banks` works with no MIDI backend; the tests import inside functions for `pytest.importorskip`. |
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
| `kws/bridge.py` | `C901`, `I001`, `S110`, `S112` | The write fence is complex by construction. `S110`/`S112` are the deliberate teardown and drain loops. |
| `kws/favorites.py` | `E501`, `RUF023`, `S608` | `E501` is an unsplittable SQL string. `S608` is `PRAGMA user_version = {SCHEMA_VERSION}`, an int constant. |
| `kws/messages.py` | `PLW1641` | `Packet` defines `__eq__` and is deliberately unhashable. |
| `kws/config.py` | `PLW0603`, `UP031` | A module-level `_warned` flag for warn-once, and `"\u%04X" % ord(c)`. |
| `kwsed/app.py` | `B904`, `I001`, `PLW0108` | The Textual event layer. |
| `kwsed/cli.py` | `C416`, `C901`, `I001` | The `send` command is the destructive path and stays in one readable function. |
| `tools/*`, remaining | `B904`, `C901`, `I001` | `sys.path` bootstrap imports and argument handling. |

### Other tools

| Tool | Suppressed | Where |
|---|---:|---|
| mypy | 0 | `quality/mypy-baseline.txt` is empty; strict passes outright |
| vulture | 0 | no whitelist needed; vulture 2.x takes one as a scanned path, and there is nothing left to put in it |
| deptry | 0 | — |
| pip-audit | 0 | — |
| detect-secrets | 0 | `.secrets.baseline` is empty of findings |

### Deleted rather than suppressed

The distinction is worth making explicitly: a suppression that documents a
deliberate choice is worth keeping, and one that hides a forgotten import is
not. Sixteen findings were of the second kind and have been deleted.

- **6 dead `typing` imports** - `Optional` in `kws/banks.py`, `kws/dumps.py`,
  `kws/multi.py` and `kws/records.py`, `Tuple` in `kws/messages.py`,
  `Iterable` in `kws/favorites.py`. None was re-exported through `__all__`.
- **6 `# noqa: E402` markers** - `kws/bridge.py`, `tests/test_write.py`,
  `tools/build_bank.py`, and three in `tools/extract_catalog.py`. They were
  never needed: E402 exempts code following a `sys.path` manipulation, which is
  what those imports follow. Only the comments went; the imports stand.
- **`import inspect`** in `tests/test_favorites.py::_loaders`, which uses
  `importlib`. Removing it took `quality/vulture_whitelist.py` with it, since
  that file existed only to hold the name, and with it the `WHITELIST`
  variable in the Makefile.
- **3 dead locals** - `entry` and `kind` in `kwsed/app.py::_fill` (two lines
  but three dead names: the method calls `_visible(pair)`, which unpacks the
  pair itself, and every later `kind`/`number`/`entry` is an attribute of the
  loop variable `item`), and `kind` in `tests/test_review_fixes.py`.

That emptied four `per-file-ignores` entries outright and trimmed the code list
from eight more. vulture needs no whitelist at all now.

### One suppression left on purpose

`tests/test_bankbuild.py` keeps `F841`, for `first = step` in `make_waveseq`: a
snapshot of the step counter taken before the loop advances it. It is dead
code, but it reads like the remains of an assertion this fixture was meant to
make about where a chain starts, and deleting it would remove the evidence.
That is a question about the test rather than about the linter, so it is
written down here instead of being answered by deleting a line.

## CI

There is no CI configuration in this repository, so none was added. When there
is, the job is one line:

```yaml
- run: make check
```