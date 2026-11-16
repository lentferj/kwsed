<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Import UX & SysEx Transfer Optimization Plan

## 1. Root-Cause Diagnosis

### 1.1 Why the Wavestation SR shows `"RECEIVING SYSEX"` 8–12 times for one performance
On the Korg Wavestation SR, **every incoming SysEx message** — including a 7-byte read request (`0x1D` Performance Bank Request, `0x1C` Patch Bank Request, `0x0C` Wave Sequence Bank Request) — causes the front-panel LCD to display `RECEIVING SYSEX`.

Currently, copying a single performance into a bank performs up to **12 full-bank SysEx transactions** (~195 KB over 31.25 kbps MIDI DIN, taking ~65–85 seconds):

1. **Preview / Context Load (3 SysEx requests + 3 bulk dumps, ~21 s)**
   `_copy_perf_load_worker` calls `_read_source_offline(dest_scope)` and requests all three bank kinds (`0x1D` performances 18.1 KB, `0x1C` patches 29.8 KB, `0x0C` wave sequences 17.5 KB).
2. **Redundant Pre-Write Backup Read (3 SysEx requests + 3 bulk dumps, ~21 s)**
   When `write_bank_bundle` is called seconds later, it unconditionally calls `read_raw` for `0x0C`, `0x1C`, and `0x1D` **again** to build the backup envelope — ignoring that `_copy_perf_load_worker` just read the exact same destination bank into `self._sources[dest_scope]`.
3. **Unconditional 3-Kind Bulk Send (3 full-bank dumps, ~21 s)**
   `write_bank_bundle` always sends `0x54` (`ALL_WAVESEQ_DUMP`), `0x4C` (`ALL_PATCH_DUMP`), and `0x4D` (`ALL_PERFORMANCE_DUMP`), even when `waveseq` (or `patches`) is completely unchanged (for example, when the imported performance only uses ROM waves).
4. **Unconditional 3-Kind Verify Read-Back (3 SysEx requests + 3 bulk dumps, ~21 s)**
   `write_bank_bundle` requests all three full-bank dumps a third time to compare against the sent frames.

### 1.2 Why `append` / `overwrite` fails on real banks (`"needs X patch slot(s), only 0/1 of 35 left"`)
1. **Re-planning existing performances without cross-performance patch sharing:**
   A Wavestation bank holds **50 performances** and **35 patches**, so performances in any populated bank naturally share patches (and frequently reference built-in `ROM 4..11` patches).
   `copy_into_bank` currently passes `existing + chosen` into `plan_atomic_bank`, which attempts to allocate **private, unshared** patch copies in the destination bank for every **existing** performance in that bank, and even tries to copy referenced `ROM` / other-bank patches into the destination bank's 35 patch slots. After ~12–15 existing performances, all 35 patch slots are exhausted and the planner rejects both the remaining existing performances and the new performance being imported.
2. **Moving existing performances instead of preserving slot numbers:**
   Re-packing `existing` performances from slot `00` shifts their slot numbers if there were gaps, breaking Multiset references and user expectations.
3. **`Init` slots treated as occupied:**
   Factory-initialized RAM banks fill unused performance slots with `"Init"` / `"INIT Perf."` (with Part 0 pointing to `0:0` and non-zero common bytes) and unused patch slots with `"Init"` / `"INIT Patch"`. Treating those as active performances/patches makes an empty or partially used bank appear 50/50 full.

### 1.3 Why the TUI import UX falls short of `ensqsqed`
- **No direct Whole-Bank hardware import in TUI:** `I` → `Whole bank` only wrote to the local disk cache (`~/.local/share/kwsed/banks`) instead of offering `ensqsqed`'s `BankImportScreen` to preview the bank, pick a target writable bank (`RAM 1`, `RAM 2`, `RAM 3`, `Card`), and write the entire bank to the unit in one fenced operation.
- **No Multi-Selection / Slot-Mapping screen:** `I` → `Performance` only let the user pick **one** performance from a list and then opened a separate text modal (`CopyPerformanceScreen`). In `ensqsqed` (`SingleImportScreen`), the user gets a **two-pane mapping screen** (source sounds on the left, target bank slots on the right) where they can map **multiple** sounds to specific target slots (free, `Init`, or occupied/overwrite) and import the entire selection in a single batch write.

---

## 2. Target UX & Architecture

### 2.1 Entry Point (`I` in Browser)
Pressing `I` opens `ChoiceScreen("Import", ["Whole bank", "Single / multiple performances"])`, followed by `FilePickerScreen` (remembering `import.folder` in `ui-state.toml`).
- If the chosen file/container (`.zip`, `.img`, 12-bank `.syx`, `0x50` All Data) holds multiple banks, a bank selector lets the user pick which source bank to open (and `s` inside the import screen switches to another source bank in the same file).
- If the file holds a single bank, it opens the chosen import screen directly.

### 2.2 Flow 1: Whole-Bank Import (`kwsed/import_bank.py` — `BankImportScreen`)
Modeled directly on `ensqsqed/import_bank.py`:
- **Header:** file path, bank label/scope, summary (`50 performances · 35 patches · wave sequences: yes/no`).
- **Table (`#import-table`):** scrollable `DataTable` listing all slots in the source bank (`#`, `Performance`, `Parts`, `Patch`).
- **Footer controls:**
  - `Target bank: RAM 2 · b changes it` (cycles or opens choice for `RAM 1`, `RAM 2`, `RAM 3`, `Card`).
  - `Write gate: locked (press w)` / `Write gate: ARMED`.
  - Hint: `b target bank · s source bank · w write gate · c cache only · ctrl+s write bank · esc / q cancel`.
- **Automatic Interior Bank Relocation:**
  When writing a source bank whose original bank number is `S` into destination bank `D` (`D.number != S`):
  - Every active `PartRef` in the 50 performances whose `part.bank == S` is rewritten to `D.number` (references to `ROM 4..11` are preserved as-is).
  - Every `WaveRef` in the 35 patches whose `wave_ref.is_sequence` and `wave_ref.bank == S` is rewritten to `D.number`.
- **Execution (`ctrl+s`):**
  - Requires armed write gate (`w`) + confirmation naming the target bank.
  - Writes `waveseq` (if present), `patches`, and `performances` in one fenced `write_bank_bundle` call, updates `self._sources`, `bankstore`, and `catalog` live names, and records one `z` undo step.

### 2.3 Flow 2: Multi-Select / Slot-Mapped Performance Import (`kwsed/import_single.py` — `SingleImportScreen`)
Modeled on `ensqsqed/import_single.py`:
- **Two-Pane Layout:**
  - **Left Pane (`#source-table` — File Performances):**
    Columns: `#` (`00..49`), `target` (`--` or assigned `00..49` in bold yellow), `name`, `deps` (e.g. `2P 1W`).
  - **Right Pane (`#target-table` — Target Bank Slots):**
    Header: `Target bank: RAM 2 · b changes it · r reread`.
    Columns: `slot` (`00..49`), `state` (`free`, `Init`, `used`, or `← 05` when staged), `name` (shows `new ← old` when overwriting an occupied slot, or `new` when filling a free/`Init` slot).
- **Live Budget & Validation Bar:**
  - Displays live destination usage for the current assignment map:
    `Assigned: N · Performances: X/50 · Patches: Y/35 · Wave Seqs: Z/32 · Steps: W/501`
  - Every time the user assigns or unassigns a performance, the slot-preserving planner runs in memory (instant, 0 MIDI traffic). If an assignment would exceed the 35-patch, 32-wave-sequence, or 501-step budget, the screen refuses that assignment immediately on the status line and explains why (e.g. `needs 3 patch slots, only 1 of 35 free in RAM 2`).
- **Ergonomic Keys for Fast Multi-Selection:**
  - `Space`: **toggle auto-assign** on the focused source performance — assigns it to the next unclaimed `free`/`Init` slot in the target bank (and advances the cursor down one row so pressing `Space` repeatedly selects a run of performances in seconds). Pressing `Space` on an already-assigned row unassigns it.
  - `Enter` or digits `0–9`: open `NumberPrompt("target slot (00-49)", seed)` to assign the focused source performance to **any** specific slot `00..49` — whether `free`, `Init`, or `used` (**overwrite**).
  - `u` / `Backspace` / `Delete`: unassign the focused source performance (`A` / `U`: auto-assign all / clear all).
  - `b`: choose target writable bank (`RAM 1`, `RAM 2`, `RAM 3`, `Card`).
  - `s`: switch source bank (when importing from a multi-bank file/ZIP/IMG).
  - `r`: force a fresh read of the target bank from hardware.
  - `w`: toggle the write gate in place.
  - `ctrl+s`: execute the batch import for all assigned performances at once.

---

## 3. Slot-Preserving, Patch-Sharing Dependency Planner (`kws/perfcopy.py`)

To make single and multi-performance import into populated banks work reliably without false `"0 of 35 patches left"` failures:

1. **Accurate Empty/Init Detection (`is_empty_performance`, `is_empty_patch`):**
   - `is_empty_performance(record)`: returns `True` when no parts are used, OR when the trimmed lower-case name is in `("", "init", "init perf", "init perf.", "unnamed")` and every used part points to patch `0` (in bank `0` or the bank's own number).
   - `is_empty_patch(record)`: returns `True` when `not any(record)` or the trimmed lower-case name is in `("", "init", "init patch", "init patch.", "unnamed")` with default oscillator waves.

2. **Slot-Preserving Batch Planner (`plan_slot_import` / `build_slot_import`):**
   Given `sources`, `target_scope`, and `assignments: Mapping[int, PerfId]` (mapping `target_slot -> source_perf_id`):
   - **Untouched performance slots stay in place:**
     Every destination slot `00..49` not in `assignments` keeps its existing 181-byte performance record at its exact slot index. Overwritten slots (`slot in assignments`) are replaced at that exact `slot`.
   - **Surviving destination patch & wave-sequence usage:**
     - Inspect all surviving (non-empty, non-overwritten) performances in `target_src`: every active part with `resolve_part_scope(part.bank, target_scope) == target_scope` pins `part.patch` (`0..34`) in the destination patch bank.
     - Inspect all pinned destination patches: every oscillator wave reference with `w.is_sequence` and `resolve_part_scope(w.bank, target_scope) == target_scope` pins wave sequence `w.wave` (`0..31`) and its transitive quoted sequences/steps in the destination wave-sequence bank.
     - Any destination patch slot `0..34` **not** referenced by a surviving destination performance (including patches that were only used by an overwritten performance, or `Init` patches) is **free**!
     - Any destination wave-sequence slot `0..31` **not** referenced by a pinned destination patch is **free**!
   - **ROM references stay in ROM:**
     - If an imported performance's part references a built-in ROM bank (`ROM 4..11`, where `not banks.bank(scope).writable`), and that bank is not the file's own synthetic/relocated bank, leave `part.bank` and `part.patch` pointing to ROM — consuming **0** RAM patch slots and requiring **0** ROM reads from hardware.
     - Likewise, if an imported patch oscillator references a wave sequence in a ROM bank (`ROM 4..11`), leave `wave_ref.bank` and `wave_ref.wave` pointing to ROM — consuming **0** RAM wave-sequence slots.
   - **Structural Deduplication Across the Batch & Destination:**
     - Before allocating a free destination patch slot for an imported part's patch (after relocating its wave-sequence references), check if an identical 426-byte patch already exists in a pinned destination slot or a slot already allocated in this batch. If so, point the part at that existing slot; otherwise claim the next free destination patch slot (`0..34`).
     - Likewise, reuse any identical wave sequence in the destination bank before claiming a free sequence slot (`0..31`) and step chain from the 501-step pool.

---

## 4. Cutting `"RECEIVING SYSEX"` Events & Transfer Time (`kws/bridge.py`, `kwsed/app.py`)

### 4.1 Where the 9–12 SysEx transfers go
| Phase | Current Behavior (1 Perf) | New Behavior (1 or N Perfs) | Saved |
|---|---|---|---|
| **1. Open import/copy screen** | Reads 3 dumps (`0x1D`, `0x1C`, `0x0C`) if not in `_sources` | Uses `_sources` / `bankstore` if already known; otherwise reads once (`3` dumps) and caches in `_sources` + `bankstore` | **0 reads** on subsequent imports |
| **2. Pre-write backup in `write_bank_bundle`** | Unconditionally reads all 3 dumps (`0x0C`, `0x1C`, `0x1D`) from hardware again | Accepts `before: Optional[Mapping[str, bytes]]` constructed from the already-read destination `Source` in memory; only reads from hardware if missing | **Saves 3 full-bank reads (~21 s, 3 `RECEIVING SYSEX` banners)** |
| **3. Send bank dumps** | Sends all 3 dumps (`0x54`, `0x4C`, `0x4D`) unconditionally | Compares built `waveseq`, `patches`, `performances` against pre-write bytes and **only sends kinds whose bytes actually changed** (e.g. skips `0x54` when no wave sequences changed; skips `0x4C` if patches already present) | **Saves 1–2 full-bank writes (~6–15 s, 1–2 `RECEIVING SYSEX` banners)** |
| **4. Post-write verification** | Reads back all 3 dumps (`0x0C`, `0x1C`, `0x1D`) | Only reads back the kinds that were actually sent (and updates `_sources` + `bankstore` + live names in place) | **Saves 1–2 full-bank reads (~6–15 s, 1–2 `RECEIVING SYSEX` banners)** |
| **5. Importing N performances** | `N ×` separate single-performance operations (`9N–12N` transfers) | **1** batched write for all `N` assigned performances | **Saves `N - 1` entire bundle writes** |

### 4.2 Concrete Transport Changes in `kws/bridge.py`
1. Add optional `before_blocks: Optional[Mapping[str, bytes]] = None` parameter to `WavestationBridge.write_bank_bundle` (and `DemoDevice.write_bank_bundle` / `Unit` test double):
   - For each kind in `payloads`: if `before_blocks` already supplies the raw framed dump (or if `payloads[kind] == existing_decoded_bytes`), skip the pre-write `read_raw` for that kind and save `before_blocks[kind]` into the backup envelope.
   - If `payloads[kind]` is byte-for-byte identical to the pre-write decoded data for that kind, omit `kind` from the wire send and verify loops (while keeping at least the changed kind(s)).
2. Keep full read-back verification on every kind that *is* sent over the wire, preserving kwsed's safety guarantee (catching any dropped MIDI bytes) at a fraction of the wire time.

---

## 5. Implementation Steps (Subagent Tasks)

### Step 1 — Slot-Preserving Multi-Performance Planner & `Init` Detection (`kws/perfcopy.py`, `tests/test_perfcopy.py`)
- Broaden `is_empty_performance` (`"init"`, `"init perf"`, `"init perf."`, `"unnamed"`) and add `is_empty_patch`.
- Implement `plan_slot_import` and `copy_slots_into_bank(sources, target_scope, assignments: Mapping[int, PerfId], destination_bank=...)`:
  - Preserves untouched destination performance slots in place (`00..49`).
  - Pins only patches and wave sequences referenced by surviving destination performances.
  - Leaves `ROM 4..11` part and wave-sequence references in ROM without copying them into RAM slots.
  - Deduplicates imported patches and wave sequences across the batch and against pinned destination slots.
- Add `relocate_bank(source: deps.Source, destination_bank: banks.Bank) -> AtomicBuilt` for whole-bank imports to a different bank number.
- Add unit tests in `tests/test_perfcopy.py` covering:
  - Importing into a bank with 15 active performances (referencing shared RAM patches + ROM patches) and 35 `Init` slots.
  - Overwriting occupied slots in place without shifting other slots and freeing patches only used by the overwritten slots.
  - Batch-importing multiple performances in one plan.

### Step 2 — Fast Bundle Write: Pre-Read Backup Reuse & Unchanged-Kind Skipping (`kws/bridge.py`, `kwsed/demo.py`, `tests/test_write.py`)
- Extend `write_bank_bundle` to accept `before_blocks: Optional[Mapping[str, bytes]] = None`.
- Skip pre-write `read_raw` for any kind already provided in `before_blocks`.
- Skip sending and reading back any kind (`waveseq` or `patches`) whose payload bytes are identical to the pre-write bank bytes.
- Add tests in `tests/test_write.py` verifying that:
  - Passing `before_blocks` performs **0** pre-write `read_raw` calls while still writing a complete, restorable backup envelope.
  - When `waveseq` is unchanged, only `patches` and `performances` are sent and verified (cutting MIDI messages from 9 down to 4).

### Step 3 — Whole-Bank Import Screen (`kwsed/import_bank.py`, `tests/test_import_bank.py`)
- Create `BankImportScreen` modeled on `ensqsqed/import_bank.py`:
  - Displays source file path, source bank selector (`s` when the file has multiple banks), summary, and a `DataTable` of the bank's performances and patches.
  - `b` selects target writable bank (`RAM 1`, `RAM 2`, `RAM 3`, `Card`); `w` toggles the app's `EditGate`; `c` saves to local cache only; `ctrl+s` triggers a fenced whole-bank write to the unit (using `relocate_bank` + `write_bank_bundle`).
- Add unit/pilot tests in `tests/test_import_bank.py`.

### Step 4 — Two-Pane Multi-Select Performance Import Screen (`kwsed/import_single.py`, `tests/test_import_single.py`)
- Create `SingleImportScreen` modeled on `ensqsqed/import_single.py`:
  - Left pane (`#source-table`): `#`, `target`, `name`, `deps`.
  - Right pane (`#target-table`): `slot` (`00..49`), `state` (`free`, `Init`, `used`, `← NN`), `name`.
  - Live budget bar (`Performances`, `Patches`, `Wave Seqs`, `Steps`) powered by `plan_slot_import`.
  - Keys: `Space` (auto-assign to next free/`Init` slot and advance), `Enter` / `0-9` (assign to any slot `00..49` including overwrite), `u`/`Backspace`/`Delete` (unassign), `A`/`U` (assign all / clear all), `b` (change target bank), `s` (switch source bank in multi-bank file), `r` (reread target bank), `w` (toggle write gate), `ctrl+s` (start batch import).
- Add unit/pilot tests in `tests/test_import_single.py`.

### Step 5 — App & CLI Integration + Documentation (`kwsed/app.py`, `kwsed/cli.py`, `README.md`, `tests/test_import_ui.py`)
- Wire `I` in `kwsed/app.py` to open `ChoiceScreen("Import", ["Whole bank", "Single / multiple performances"])` → `FilePickerScreen` → `BankImportScreen` or `SingleImportScreen`.
- Wire `y` (`action_copy_performance`) to use the slot-preserving planner and pass `before_blocks` to `write_bank_bundle` (or open `SingleImportScreen` pre-populated with the selected performance).
- Update `kwscli copy-perf` to use the slot-preserving planner and support `--slot TARGET_SLOT` for explicit slot placement/overwrite.
- Update `README.md` and run the full quality gate (`pytest`, `mypy`, `quality/check_mypy.py`, `ruff`).

---

## 6. Self-contained whole-bank import (2026-11-14)

Self-contained import is a whole-bank write that makes a bank sound as
authored when its performances reference only itself and ROM. It was added
because `kws.perfcopy.relocate_bank` moves **self-references only** — by
design, matching the unit's own bank move — so a performance whose parts
name another writable bank keeps naming it after the write, and lands on a
slot that holds something else. The whole-bank write still touches exactly
one bank; the fix is information plus real self-contained modes, not an
implicit second write.

### 6.1 Containment audit (`kws/containment.py`)

`audit_containment(sources, source_scope)` classifies every non-empty
performance in the source bank by where its used parts point. Empty (init)
slots are ignored. Three counts are reported, and they add up to the bank's
non-empty performances:

- **contained** — performances with no reference to another writable bank.
  This is the plan's D1: self-plus-ROM counts as contained, because ROM
  banks are always present on the instrument. ROM-assisted performances are
  included here.
- **other bank** — performances that reference at least one other writable
  bank (RAM 1-3 or the Card). `parts_per_bank` counts how many used parts
  name each such bank, so the screen can say which.
- **ROM** (`rom_assisted`) — the subset of `contained` whose parts also
  point at ROM. Reported separately so ROM references are never hidden
  behind the contained total.

The screen prints this as
`contained: N · other bank: M (banks …) · ROM: K`.

### 6.2 The three modes (`kws/perfcopy.py::ImportMode`)

`m` cycles them in this order: as-is, self-contained 1:1, self-contained
packed.

- **as-is** — the existing `relocate_bank` path, byte for byte. Writes the
  whole bank (every performance, patch and wave sequence), skipping
  nothing. Self-references move to the target; references to other writable
  banks and to ROM stay where they point.
- **self-contained 1:1** — writes only the contained performances, each at
  its own source slot; every other slot is cleaned. Patches are pinned to
  what is used; ROM references stay in ROM.
- **self-contained packed** — the same contained performances, re-slotted
  `0..n-1` in source order, with patch slots compacted and deduplicated in
  first-use order (one slot per distinct source patch). Wave sequences and
  step links are relocated. ROM references stay in ROM.

Both self-contained modes skip the performances that reference another
writable bank. A bank that cannot hold them all splits into passes (§6.4).

### 6.3 Companion spill-over (`x`)

When the file holds more than one bank, `x` offers the writable RAM
companions, one at a time. Each is the same fenced whole-bank write as
`ctrl+s`, into the RAM bank the source's parts actually name (plan D2) by
default; the suggested target may be changed before writing. ROM and Card
companions are not offered — ROM cannot be written, and card targets belong
to the HCard work. Nothing is repointed automatically: `repoint_external_refs`
exists as an opt-in helper and is never applied by a companion write.

### 6.4 Multi-pass remainder (`n`)

After a self-contained write, `n` offers the contained performances no
earlier pass has written, into a fresh target — another card bank after a
switch, or a RAM bank. `partition_self_contained` greedily splits the
remainder into bank-sized passes by performance count (50) and distinct
patch count (35); a performance that alone exceeds the patch budget still
gets its own pass. The first pass keeps source slots under 1:1;
continuation passes pack through `Pass.perf_slot_map`.

Pass state lives in the session under a per-visit key: `_session_key` carries
a fresh token per screen visit, so re-importing the same file starts over
instead of continuing a dead session's remainder, while a re-offered screen
inherits the earlier passes' written-slot map. The status line echoes
`pass so far: N written (target: count) · next pass: M remaining`. With
nothing left, `n` says so and writes nothing.

Card targets carry a slot guard: a write to `CARD/xx` is refused unless the
unit's card slot actually holds that card, so a pass cannot overwrite the
wrong physical card while being filed under the requested archive scope.

### 6.5 Key bindings

`WholeBankImportScreen.BINDINGS` (quoted as coded):

- `escape` / `q` — cancel
- `s` — source bank
- `b` — target bank
- `m` — mode
- `o` — overflow (patches only, or a self-contained split)
- `n` — next pass
- `x` — companions
- `w` — toggle gate
- `ctrl+s` — write
- `c` — cache
- `?` — help

The target list is the four physical writable banks only (RAM 1-3 and the
Card); archived card banks are never targets, because the unit has one card
slot.

The on-screen hint reads
`s source · b target · m mode · o overflow · n next pass · x companions · w gate · ctrl+s write · c cache · ? help · esc/q cancel`.

### 6.6 Overflow kinds behind `o`

After the RAM bank is chosen (`o` is only offered when parts name another
writable bank), three kinds are offered:

- **patches only** — the target keeps every performance and the patches
  they name in their own bank; the patches they name in other writable
  banks go to the overflow bank, and the parts are repointed there. The
  overflow bank is written first, then the target, so a performance that
  names it finds its patches in place. `plan_overflow_import` reports a
  rejection (and nothing is written) when the external patches do not fit
  the overflow bank's patch slots, when their wave sequences do not fit its
  sequence slots, or when the destination bank's patch, sequence or step
  budget is exceeded (via `plan_atomic_bank`). A step-budget overflow on
  the overflow side is not checked at plan time; it surfaces as a
  `ValueError` while building.
- **self-contained split** — the performances whose parts leave their own
  bank move to the overflow bank together with every patch they name;
  patches used by performances in both banks are duplicated so each bank is
  self-contained. The target keeps the contained performances.
- **cards only** — no RAM bank is written at all. Every non-empty
  performance is packed into self-contained groups that each fit one card
  (`pack_self_contained_into`); one group is written per pass, one card at
  a time, so the import visits card after card. The physical Card slot is
  the target; archived card banks are never targets.

### 6.7 Partition rule for the self-contained split

The split is planned over the target plus the offered RAM bank with
`partition_self_contained_split`. It is partitioned across as many banks
as it needs, greedily, by performance count (50) and distinct patch count
(35); a performance whose patch set alone exceeds the patch budget still
gets its own pass. A partition never assigns two groups to one bank. When
the banks offered cannot hold every group, the groups that fit are written,
the unwritten source slots are kept in the import session, and the screen
asks for the card to be switched: `n` then packs one more group into
whatever card is in the slot, repeating until nothing remains. Nothing is
dropped and no earlier group is overwritten. A split that cannot be planned
at all (a patch it names is not loaded, or one performance alone exceeds a
bank) is refused before any write. The patches-only kind is refused too
when the destination bank's patch, sequence or step budget is exceeded
(via `plan_atomic_bank`) or the overflow bank's patch or wave-sequence
budget is exceeded at plan time (`plan_overflow_import` reports those
rejections); a step-budget overflow on the overflow side surfaces as an
error during the build. It is not "never refused on size".

A third kind, **cards only**, never touches a RAM bank: every non-empty
performance is packed into self-contained groups that each fit one card,
the first group is written to the card in the slot, and each later group
goes to the next card after a switch (`n`). It visits as many cards as the
content needs and drops nothing.

### 6.8 Whole-bank write and the cached bank

Whole-bank writes pass no `before_blocks` to the bridge, so the bridge
reads the unit first. Handing it a before-block built from the cached bank
let the bridge skip kinds that matched, which on a cache hit sent no SysEx
at all while the status reported success, and recorded cache bytes instead
of the unit's in the backup. See `docs/RESOLUTION_NOTES.md` §18. A cache is
not evidence of what the unit holds; the bridge must read the destination
from the unit itself before writing.

### 6.9 Preflight question (before a plain write)

On `ctrl+s`/`n`, for a plain write (as-is / 1:1 / packed) with no
overflow of its own, when the source bank's audit shows parts naming
another writable bank that the same file carries, and the target is not
that bank, the app asks before anything is sent. It is asked every time,
regardless of what the unit already holds. The three answers are:

1. Write both — the companion bank's patches and wave sequences only
   (its performances untouched), then the target as requested (two fenced
   writes, two backups).
2. Write only this bank — just the target; external parts keep pointing
   where the file points (an explicit, informed choice).
3. Cancel — nothing; the import screen is reopened with guidance to use
   `o` for a self-contained/cards-only pass or `x` for companions.

Ordering: companion first, always. A performance must not be written
before the patches it names exist. After choosing 2, no post-write
nagging: the answer was explicit.

### 6.10 Device worker and progress

Whole-bank writes do not run synchronously on the UI thread. `_import_whole_bank_write`
builds the plan on the UI thread, then hands the write to `_device_worker`
(`kwsed/app.py`), which runs it on a background thread with a progress
bar under the status line and reports back via `call_from_thread`. Each bank
written is still its own fenced write with backup and read-back.

---

## Implementation status (2026-11-11)

- [x] **Init/empty detection** — `Init`, `INIT Perf.`, `Init Patch` performance/patch slots are free; `is_empty_patch` added.
- [x] **Slot-preserving planner** — `plan_slot_import` / `build_slot_import` / `copy_slots_into_bank` preserve untouched slots, pinned shared/ROM references, free `Init` slots, exact target slots, batch assignments, and sequence/step budgets. `relocate_bank` handles whole-bank interior references.
- [x] **Fast bundle writes** — `write_bank_bundle(..., before_blocks=...)` reuses an already-read destination, skips unchanged `waveseq`/`patches`/`performances`, and only verifies what it sent. A single populated-bank performance import is now one bundle with 2–4 wire operations instead of 9–12.
- [x] **Whole-bank screen** — `kwsed/import_bank.py::WholeBankImportScreen`: source-bank switch, target-bank switch, cache-only `c`, fenced `ctrl+s`, read-only preview.
- [x] **Multi-selection screen** — `kwsed/import_single.py::SingleImportScreen`: two panes, `Space` auto-assign, exact `Enter`/digit slot assignment including overwrite, `u`/`A`/`U`, live budgets, gate, one batch write.
- [x] **Browser `I` integration** — Whole bank (cache or unit) and Single/multiple performances; `y` copy and `kwscli copy-perf` use the slot-preserving planner and `before_blocks`.
- [x] **Tests** — planner adversarial tests, screen pilot tests, app-flow tests, fast-bundle tests.
- [x] **Same-scope and multi-source single import** — imported banks can be held under unique session aliases with the real `Source.bank_number`; several files, including several RAM1 banks, can be assembled into one target bank with assignments preserved across `s`.
- [x] **Long writes off the UI thread** — whole-bank writes run through `_device_worker` with a progress bar; see §6.10.
- [ ] **Optional CLI `--slot` / explicit replace mode** — append and clean are supported; explicit slot replace is in the TUI copy screen, not yet in `kwscli copy-perf`.
