# Dependency Audit / Companion Discovery Plan

## Goal

When a performance-only bank is loaded:

1. warn when performances point at patch slots that are absent, empty or `Init`;
2. warn when those patches reference wave sequences not present in the source;
3. look in the same directory for similarly named performance/patch/waveseq
   fragments;
4. auto-merge only when exactly one candidate resolves the missing
   dependencies, and the candidate is the same logical bank;
5. otherwise leave the source untouched and show the ambiguity/missing list.

## Invariants

- Warnings never prevent the session from opening.
- Parts that point at RAM2/RAM3/ROM/Card are external references, not missing
  data from the current file.
- Only referenced patch slots are required; missing unreferenced patches are
  not an error.
- A candidate is matched by bank number/scope and by what it supplies, not by
  filename alone.
- Ambiguous matches are reported, not guessed.
- Auto-merge happens before the target bank map opens; assignments are not
  affected.
- The source pane stays on the performance bank; waveseq-only files are not
  selected as a new empty source.

## Atomic tasks

1. `kws/import_audit.py`
   - `DependencyAudit` dataclass.
   - `audit_source(source)` → missing patch slots, missing waveseq slots,
     referenced counts, summary/complete.
   - tests with synthetic performance/patch records.

2. `kwsed/import_companions.py`
   - normalized stem matching for `*_Performances`, `*_Patches`,
     `*_Waveseq`, `.syx`/`.MID`/`.bnk`.
   - same-directory scan; decode via `importers.read_file`.
   - same logical bank and component/coverage checks.
   - return candidates with merged source and resulting audit.
   - tests with temp `.syx` files.

3. App integration
   - run audit when selecting/adding a file source;
   - try unique companion merges, then re-audit;
   - show `auto-merged …` / missing-dependency warning in the import screen;
   - keep warnings visible after screen refresh.
   - tests.

4. Docs/full gate.

## Implementation status

- [x] 1. `kws/import_audit.py` — local patch/waveseq audit.
- [x] 2. `kwsed/import_companions.py` — same-stem discovery, same-bank and
  coverage matching, byte-identical candidate collapsing.
- [x] 3. App/UI integration — auto-merge in source selection and `s` add-file;
  warning/merge note carried into the import screen.
- [x] 4. README and tests. Full gate: 983 passed, 9 skipped; mypy/ruff clean.
- [x] Automatic companion reading is synchronous for local files; no
  background worker is used because single-file decoding is cheap. Large
  container scanning remains a future task.
- [x] Byte-identical `.MID`/`.syx` companions are collapsed; the source
  extension is preferred. Incomplete or ambiguous candidates produce a
  screen warning, not a guess.
