# Unified Import / Copy Plan

## Goal

One source-to-target workflow for both **file import** and **performance copy**.
Every source is a bank snapshot:

- file banks (`.syx`, `.mid`, `.img`, `.zip`, `.LIB`, `.MPC`, ...)
- unit RAM 1-3
- unit ROM 4-11
- the active Card / captured HCard bank
- existing cache entries

The target is always one writable bank (RAM 1-3 or Card). The existing
slot-preserving planner remains the copy engine. This plan only unifies where
the source snapshots come from and how the UI describes them.

## Invariants

1. `perfcopy.plan_slot_import` and `copy_slots_into_bank` stay source-agnostic.
2. A unit source is read lazily and cached for the session. Opening the source
   menu never scans every bank.
3. ROM is read-only. It can be a source, never a target.
4. A source bank that is also the target bank is snapshotted under a unique
   session alias; the target keeps its live records.
5. Source reads do not require the write gate. The final target write remains
   one fenced bundle, one backup and one undo step.
6. Multi-source remains n:1: several source banks/files into one target bank.
7. `q` and `Esc` both go back one screen; every chooser shows a legend.

## Tasks

### 1. Source-session abstraction

New `kwsed/import_source.py`:

- `SourceEntry` with `label`, `scope`, `source`, `origin`, `read_only`.
- `source_path` / `source_origin` compatibility properties for the target pane.
- unique session aliases for colliding scopes (two RAM1 files, RAM1 into RAM1).
- `make_source_entry(...)` for file and unit snapshots.
- `unit_bank_choices()` listing RAM 1-3, ROM 4-11 and Card with labels.
- `origin_for_unit(scope)` / compact file origin formatting.
- tests for aliasing, bank numbers, read-only flags and origins.

### 2. Single-import screen source model

Extend `kwsed/import_single.py`:

- accept initial assignments (used by the `y` shortcut);
- use `source_origin` when a source has no file path (RAM/ROM/Card);
- source menu labels distinguish file banks from unit banks;
- keep assignments when switching/adding sources;
- tests for initial assignments, unit origin display and source switching.

### 3. Unit sources in the import session

Extend `kwsed/app.py`:

- `s` source menu: existing sources + **Add another source file...** +
  **Add unit bank (RAM/ROM/Card)...**.
- unit chooser lists RAM 1-3, ROM 4-11 and Card; already-added banks are
  marked or refused clearly.
- load a unit bank once from cache/unit using `_read_source_offline`; use the
  active Card scope; refuse gracefully on read errors.
- wrap it as a `SourceEntry` with `origin="RAM 2"`, `"ROM 7"`, `"Card 07"`.
- no source scan just to open the menu.

### 4. Unified `y` shortcut

`y` opens the same `SingleImportScreen` instead of the legacy
`CopyPerformanceScreen`:

- load the selected performance's source bank;
- choose the target bank;
- prefill the first free/Init target slot with that performance;
- allow adding more unit/file sources through the same `s` menu;
- write through the same batch path and undo record.
- Keep `CopyPerformanceScreen` code for compatibility, but remove it from the
  `y` action; update its tests to the unified screen where appropriate.

### 5. Whole-bank source choice (optional second pass)

If the unified single flow is stable, offer the same file/unit source choice
for **Whole bank** and feed the selected unit snapshot to
`WholeBankImportScreen`. No copy-engine changes.

### 6. Documentation and verification

- README: one source→target flow, unit sources, origin column, `y` shortcut.
- Full tests, mypy, `quality/check_mypy.py`, ruff.
- Commit atomic tasks separately.

## Implementation status

- [x] 1. Source-session abstraction (`kwsed/import_source.py`).
- [x] 2. Initial assignments and unit origins in `SingleImportScreen`.
- [x] 3. Unit RAM/ROM/Card sources in the `s` source menu, one read per bank.
- [x] 4. `y` opens the unified screen prefilled and writes one batch.
- [x] 5. Whole-bank source choice (file or unit) uses the same source loader.
- [x] 6. README/docs/tests; full gate still to be run after the final doc pass.
- [x] 7. Complementary same-bank fragments merge automatically:
  `*_Performances.syx` + `*_Patches.syx` + `*_Waveseq.syx` become one source
  bank. Conflicting banks remain separate session aliases.
- [ ] Korg Legacy VST `.wsram`/`.fxb` native files remain unsupported; the
  `.syx`/`.MID` files in the same packages are the supported route.

## Acceptance

- A RAM2 performance can be copied into RAM1 and then into Card without
  leaving the unified session.
- A file bank and a ROM bank can be assigned together into one RAM target.
- `y` opens the unified screen with the selected performance preassigned.
- A ROM source never appears as a writable target.
- Source banks are read at most once per session.
- `q` and `Esc` go back one step everywhere in the import flow.
- Target writes are one bundle and one undo step.
