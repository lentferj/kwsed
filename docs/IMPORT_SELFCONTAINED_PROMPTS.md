<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Subagent prompts — self-contained whole-bank import

Companion to `docs/IMPORT_SELFCONTAINED_PLAN.md` (the design). This file is
the orchestration recipe: one small task per subagent, in order.

## How to run

- Spawn **one subagent per task**, in order T1 → T10. Tasks are small on
  purpose so a single agent never hits its step limit. If a task's diff
  starts exceeding its scope, the agent must stop, commit what passes, and
  report — the orchestrator then splits the remainder into a follow-up.
- Model preference for subagents, in this order (fall back down the list on
  unavailability): `opencode/glm-5.3-flash-free` →
  `opencode/deepseek-flash4.1-free` → `opencode/beam-free` →
  `opencode-go` → `opencode/deepseek-4.1-flash`.
- After each task: run the full suite (`PYTHONPATH=. python -m pytest -q`),
  then commit with a plain message (no AI attribution, per CLAUDE.md).
- Never hand a subagent real instrument or soundset data; fixtures are
  invented. Never open MIDI.

## Shared preamble

Paste this at the top of **every** subagent prompt:

```
Repo: /home/lentferj/git-repos/kwsed — a Textual desktop editor for a
vintage hardware synth (kwsed). Read CLAUDE.md before touching anything.

Hard rules:
- GPL-2.0-or-later header block on every new/edited .py file (copy an
  existing header verbatim, adjust nothing but keep the year).
- New .md files get the short HTML-comment SPDX header.
- NEVER write real instrument, soundset, patch, performance or
  wave-sequence names anywhere in code, tests or fixtures. Invent names.
- Tests are synthetic and pure: no MIDI, no hardware, no bridge.
- Project venv: .venv. Run tests: PYTHONPATH=. python -m pytest -q
  (loop on the task's own test file first, full suite before handing back).
- Keep the diff minimal to the task. No drive-by refactors, no reformat.
- If you approach your step budget: commit nothing broken — leave the tree
  green, report exactly what is done and what remains.
```

## T1 — containment audit engine

```
TASK T1 (engine, no UI). Design: docs/IMPORT_SELFCONTAINED_PLAN.md §4.

Goal: classify every used part of every performance in a source bank.

Files: kws/perfcopy.py (add; or a new kws/containment.py if that keeps
perfcopy under ~2000 lines — prefer the new module).

Add:
- @dataclass PartTarget: bank_number (int), is_rom (bool), patch (int)
- @dataclass PerformanceAudit: slot, used part targets, self_only flag,
  external bank numbers set, rom_parts count
- @dataclass ContainmentAudit: source bank number, per-performance list,
  counts (contained / other-bank / rom-assisted), parts per external bank,
  distinct patch records per group
- def audit_containment(sources: Mapping[str, deps.Source],
                        source_scope: str) -> ContainmentAudit

Semantics (plan D1): "contained" = all used parts point at the source bank
or at ROM. Empty performances (is_empty_performance) are ignored, not
counted as failures. Decode parts with records.performance_parts; ROM via
the existing ROM bank-number checks used elsewhere in perfcopy/relations —
reuse helpers, do not re-derive encodings.

Tests (tests/test_containment.py, all synthetic): build tiny deps.Source
fixtures with invented 16-byte record names and parts set via
records.set_performance_part. Cover: fully self performance; self + ROM;
self + other writable bank (assert the bank number appears in the audit);
empty performance ignored; counts and per-bank totals correct.

Done when: pytest green; audit has no I/O.
```

## T2 — plan/build self-contained (a1 + a2)

```
TASK T2 (engine). Design: plan §4, D3. Depends on T1.

Files: the module from T1 (or kws/perfcopy.py).

Add:
- class ImportMode(Enum): AS_IS, SELF_CONTAINED_1TO1, SELF_CONTAINED_PACKED
- def plan_self_contained(sources, audit, destination_bank, mode,
                          *, perf_slots: Optional[Mapping[int, int]] = None)
    -> AtomicPlan   # perfcopy.plan_atomic_bank under the hood
- def build_self_contained(...) -> AtomicBuilt  # perfcopy.build_atomic_bank

Semantics:
- 1TO1: chosen = contained performances at their source slots; other slots
  cleaned by the existing builder.
- PACKED (D3): chosen performances re-slotted 0..n-1 in source slot order;
  patch compaction/dedup and wave-sequence relocation come free from
  plan_atomic_bank — do not reimplement them.
- AS_IS must not exist here (relocate_bank stays the AS_IS path).

Tests (tests/test_selfcontained.py): synthetic source with: two contained
performances sharing one patch (assert dedup: PACKED uses fewer patch
slots than 1TO1 pins); a wave-sequence reference that must relocate with
its steps; a skipped non-contained performance leaves clean slots (1TO1)
or shrinks the layout (PACKED). Assert byte-level via the existing test
helpers for record equality used in the perfcopy tests.

Done when: pytest green; both modes pure functions.
```

## T3 — remainder + partition (engine for option c)

```
TASK T3 (engine). Depends on T1, T2.

Files: module from T1/T2.

Add:
- def remaining_performances(audit, written_slots: Iterable[int])
    -> list[int]   # source slots not yet written
- def partition_self_contained(source, audit, *, perfs_per_bank=50,
    patches_per_bank=35) -> list[Pass]
    @dataclass Pass: source slots + implied perf slot mapping
    Greedy: keep source order; start a new Pass when adding the next
    performance would exceed either budget (count its distinct patch
    records from the audit). ROM-referencing patches cost nothing.

Tests (extend tests/test_selfcontained.py): a fixture with more
performances+distinct patches than one bank holds → ≥2 passes; a
performance whose patch set would overflow starts the next pass;
remaining_performances subtracts written slots exactly.

Done when: pytest green.
```

## T4 — opt-in repoint helper (D2)

```
TASK T4 (engine, opt-in only; nothing calls it yet). Depends on T1.

Files: module from T1.

Add:
- def repoint_external_refs(perf_bank_bytes: bytes, from_bank: int,
                            to_bank: int) -> bytes
  Rewrites every used performance part naming from_bank to to_bank
  (records.remap_performance_banks is the precedent for self-references —
  generalize, do not special-case). Also rewrite patch wave-sequence
  bank references the same way if records exposes them; if not, cover
  performances only and state the limitation in the docstring.

Tests (extend tests/test_containment.py): parts in from_bank move,
to_bank parts untouched, ROM untouched, unused parts untouched, size
preserved.

Done when: pytest green. Keep it inert: no UI/app call sites.
```

## T5 — import screen: audit, mode key, help fix

```
TASK T5 (UI, pilot-tested). Depends on T1, T2.

Files: kwsed/import_bank.py.

Add:
- WholeBankChoice gains mode: ImportMode (default AS_IS).
- On source change, run audit_containment and render a containment block
  in the summary: "contained: N · other bank: M (banks …) · ROM: K".
- Key m cycles AS_IS → 1TO1 → PACKED; the status line states exactly what
  the current mode writes and skips (e.g. how many performances will be
  left out) BEFORE the gate can be armed.
- FIX THE HELP TEXT: today it claims whole-bank writes "relocate interior
  bank references … leave ROM references in ROM". Correct it to name all
  three classes: self-references move to the target; references to other
  writable banks stay where they point; ROM references stay in ROM.

Pilot tests (extend tests/test_import_bank.py): mode cycling; containment
block contents for a synthetic two-bank source; choice carries the mode.

Done when: pytest green including the pilot tests.
```

## T6 — app wiring: mode through the fenced write

```
TASK T6. Depends on T2, T5.

Files: kwsed/app.py (_import_whole_bank_write and its callers).

Behaviour: WholeBankChoice.mode selects the path —
- AS_IS: perfcopy.relocate_bank (byte-identical to today),
- 1TO1/PACKED: build_self_contained over the chosen source+target.

The fence does not change: EditGate arm, unconditional pre-write backup,
write_bank_bundle, read-back verify all stay exactly as they are. Nothing
may write a second bank.

Tests (extend tests/test_import_ui.py, synthetic device): each mode writes
the expected bytes into the target cache/unit stub; AS_IS output equals
relocate_bank output; skipped performances are clean slots in 1TO1; the
backup file still appears before the write in every mode.

Done when: pytest green.
```

## T7 — spill-over UI (option b)

```
TASK T7. Depends on T6.

Files: kwsed/import_bank.py, kwsed/app.py.

Behaviour: when the imported file holds more than one source bank, the
screen offers the companion banks. For each companion, the suggested RAM
target is the bank the main source's parts actually name (from the audit's
per-bank totals). Selecting a companion + target performs the SAME fenced
whole-bank write path (T6) into that RAM bank — one explicit write per
bank, never automatic, never bundled into the first write.

Tests (tests/test_import_ui.py): companion list appears for a two-bank
file and not for a one-bank file; suggested target equals the audited
bank; performing the spill issues exactly one fenced write to that bank.

Done when: pytest green.
```

## T8a — card/HCard targets in the whole-bank screen

```
TASK T8a. Depends on T6. (First half of option c.)

Files: kwsed/import_bank.py, kwsed/app.py.

Behaviour: in hcard mode the screen's target list additionally includes
the HCard card banks (CARD/NN …), so a whole-bank import can target a
card other than the inserted one. Reuse the app's existing card-bank
resolution; no new bank semantics.

Tests (tests/test_import_ui.py): target list contains HCard banks in
hcard mode and only physical banks otherwise; a write to an HCard bank
lands in the right scope.

Done when: pytest green.
```

## T8b — remainder pass (option c UI)

```
TASK T8b. Depends on T3, T8a.

Files: kwsed/import_bank.py, kwsed/app.py.

Behaviour: after a 1TO1/PACKED write, the screen offers a continuation
pass: remaining_performances from the audit minus the written slots, with
a fresh target choice (another card bank after a card switch, or a RAM
bank). Pass state (written slots per target) lives in the session and is
echoed in the status line. Each pass is its own fenced write.

Tests (tests/test_import_ui.py): two-pass scenario writes disjoint slot
sets to two targets; union covers exactly the contained performances;
skipped non-contained performances are reported in both passes.

Done when: pytest green.
```

## T9 — docs

```
TASK T9. Depends on T6–T8b (document what exists, not plans).

Files: docs/IMPORT_PLAN.md (new section), README.md (key help), and a
RESOLUTION_NOTES entry recording the 2026-11-14 diagnosis (counts only:
which bank was imported, how many parts pointed at the other bank/ROM,
what the new modes do). Obey the CLAUDE.md rules: no real patch or
performance names anywhere.

Done when: markdown lint-clean, SPDX headers present, suite still green.
```

## T10 — CLI parity (optional)

```
TASK T10 (optional; only if the CLI exposes whole-bank import).

Files: the CLI module exposing whole-bank import.

Add flags: --mode asis|1to1|packed, and --list-companions for option b.
Reuse the T6 path; no separate logic.

Tests: CLI-level synthetic tests, same style as existing CLI tests.

Done when: pytest green.
```
