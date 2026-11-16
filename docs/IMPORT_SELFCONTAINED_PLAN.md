<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Self-contained whole-bank import — plan

> **Status: historical design record.** This document records the design as
> proposed on 2026-11-14; the behaviour it describes has since shipped and
> may differ in detail. It is superseded by the shipped behaviour documented
> in `README.md`, `docs/IMPORT_PLAN.md` and the code in `kwsed/import_bank.py`
> and `kwsed/app.py`. The directions in this document that name **archived
> HCard/card banks as import targets** are obsolete: archived card banks are
> never import targets, only the single physical Card slot is. The design
> history below is kept for reference.

Status: proposed 2026-11-14 · companion: `docs/IMPORT_SELFCONTAINED_PROMPTS.md`
(subagent tasks + prompts)

## 1. Why

A two-bank ALL-data file imported whole into the Card produced performances
whose parts still name the other RAM bank. `kws.perfcopy.relocate_bank` moves
only **self-references** — by design, and matching the unit's own bank move.
References to another writable bank and to ROM stay where they point.

Two failures on our side:

1. **No pre-import information.** The whole-bank screen shows a name/slot
   table and a patch count, but never says how many parts point outside the
   source bank, or what the write will and will not make self-contained.
2. **The help text is wrong.** `kwsed/import_bank.py` claims whole-bank
   writes "relocate interior bank references to the target and leave ROM
   references in ROM". It omits the third case — references to *other
   writable banks* also stay — which is exactly the case that bites.

The whole-bank import writes exactly one bank. Nothing outside the chosen
target is touched. The failure mode on the instrument is silent slots, not
lost data; the fix is information plus real self-contained modes.

## 2. Requirements

- **R1 — audit before write.** Every whole-bank import shows, before the
  gate can be armed: how many performances are fully contained in the source
  bank, how many reference other writable banks (and which), how many
  reference ROM, and what the selected mode will write and skip.
- **R2 — self-contained modes always offered**, for every writable target,
  explicitly for the Card:
  - **a)** import only performances whose parts already reside in the source
    bank (plus ROM, see D1),
    - **a1)** at their own slots, 1:1,
    - **a2)** re-slotted and defragmented: compact patch slots, deduped
      patches, remapped part→patch numbers, relocated wave sequences and
      steps.
- **R3 — spill-over (b).** When the file holds the companion bank(s) the
  performances reference, offer to write those banks into the RAM banks the
  source's own references name, so the whole set sounds as authored.
- **R4 — multi-pass (c).** Offer a) (a1/a2), then, after a card switch, a
  second pass importing the remaining performances/patches into another
  card/bank, self-contained with remapping and defragmentation.
- **R5 — nothing destructive changes.** The write gate, unconditional
  pre-write backup, and read-back verification stay exactly as they are. No
  mode writes a second bank implicitly; every extra write is its own fenced
  operation.

## 3. Decision points

- **D1 — ROM references count as contained.** ROM banks are always present
  on the instrument; a performance whose parts are self + ROM sounds. The
  audit reports ROM references separately so the number is never hidden, but
  "contained" means self-or-ROM. A strict self-only toggle is a later add-on,
  not part of this plan.
- **D2 — spill-over target.** Default: the RAM bank the source's parts
  actually name. An explicit *repoint* (rewrite the target's references from
  bank X to bank Y) exists as a helper but is opt-in only, never automatic.
- **D3 — packed slot order.** Performances keep source order and are
  re-slotted 0..n-1; patches are compacted in first-use order with the
  existing dedup.

## 4. Engine design (`kws/perfcopy.py`; new `kws/containment.py` if cleaner)

All pure, no MIDI, no hardware.

```
@dataclass PartTarget            # one used part: bank number, is_rom, patch
@dataclass PerformanceAudit      # slot, classification, external bank set
@dataclass ContainmentAudit      # counts + per-bank part totals + patch sets

class ImportMode(Enum):
    AS_IS                 # today's relocate_bank
    SELF_CONTAINED_1TO1   # a1
    SELF_CONTAINED_PACKED # a2

def audit_containment(sources, source_scope) -> ContainmentAudit
def plan_self_contained(sources, audit, destination_bank, mode) -> AtomicPlan
def build_self_contained(...) -> AtomicBuilt
def remaining_performances(audit, written_slots) -> list[int]
def partition_self_contained(source, audit, *, perfs=50, patches=35) -> list[Pass]
def repoint_external_refs(perf_bank, from_bank, to_bank) -> bytes   # opt-in, D2
```

Reuse, do not duplicate:

- `plan_atomic_bank` / `build_atomic_bank` (+ `AtomicPlan`, `AtomicBuilt`)
  already relocate patches, wave sequences and the 501-step pool with
  deduplication — a1 and a2 are slot-assignment policies on top of it.
- `relations.PerfId` identifies a source performance.
- `is_empty_performance` keeps "contained" honest about Init slots.
- `relocate_bank` stays the AS-IS path, byte for byte.

Semantics:

- **a1**: chosen = contained performances at their source slots; every other
  slot is cleaned. Patches: `plan_atomic_bank` pins exactly what is used.
- **a2**: chosen = contained performances, re-slotted 0..n-1 in source order;
  patch slots compacted/deduped by the same planner; wave sequences and step
  links relocated.
- **AS_IS**: unchanged.

## 5. UI design (`kwsed/import_bank.py`, `kwsed/app.py`)

- `WholeBankChoice` gains `mode: ImportMode`.
- The summary gains a containment block:
  `contained: N · other bank: M (banks…) · ROM: K`.
- Key `m` cycles the mode; the status line names what will be written and
  what will be skipped before the gate can be armed.
- **b**: when the file holds companion banks, `x` opens a companion list
  (bank → the RAM bank the audit says it should fill) and runs the same
  fenced whole-bank write into that RAM bank.
- **c**: after a self-contained write, `n` offers the remainder with a new
  target; in hcard mode the target list includes the HCard banks, so the
  card can be switched between passes. Pass state (slots written, target)
  lives in the session and is echoed in the status.
- Help text corrected to name all three reference classes.

## 6. Tasks

| # | Task | Files | Tests |
|---|------|-------|-------|
| T1 | containment audit engine | `kws/perfcopy.py` (or `kws/containment.py`) | `tests/test_containment.py` |
| T2 | plan/build self-contained (a1, a2) | `kws/perfcopy.py` | `tests/test_selfcontained.py` |
| T3 | remainder + partition (c engine) | `kws/perfcopy.py` | `tests/test_selfcontained.py` |
| T4 | opt-in repoint helper (D2) | `kws/perfcopy.py` | `tests/test_containment.py` |
| T5 | screen: audit, `m` mode, help fix | `kwsed/import_bank.py` | `tests/test_import_bank.py` |
| T6 | app wiring: mode through the fenced write | `kwsed/app.py` | `tests/test_import_ui.py` |
| T7 | spill-over UI (b) | `kwsed/import_bank.py`, `kwsed/app.py` | `tests/test_import_ui.py` |
| T8a | HCard/card targets in the whole-bank screen | `kwsed/import_bank.py`, `kwsed/app.py` | `tests/test_import_ui.py` |
| T8b | remainder pass (c UI) | `kwsed/import_bank.py`, `kwsed/app.py` | `tests/test_import_ui.py` |
| T9 | docs: IMPORT_PLAN section, README keys, RESOLUTION_NOTES | docs | — |
| T10 | CLI parity (optional) | `kwsed/cli.py` | `tests/test_cli_*.py` |

Each task is deliberately small; T5–T8 touch one screen and its app hook
only. Run order T1 → T4, then T5 → T8b; T9/T10 last.

## 7. Conventions for every task

- Synthetic tests only; no hardware, no MIDI, no bridge construction.
- Fixture names invented — never real instrument, soundset, patch,
  performance or wave-sequence names (CLAUDE.md).
- New `.py` files carry the full GPL header block; new `.md` files the short
  HTML-comment SPDX header.
- `PYTHONPATH=. python -m pytest -q` green before handing back; commit
  messages plain, no AI attribution.

## 8. Out of scope

- Any change to `write_bank_bundle` wire behaviour or the fence.
- Automatic repointing of cross-bank references (D2 keeps it opt-in).
- Renumbering wave-sequence pools beyond what `plan_atomic_bank` already does.
