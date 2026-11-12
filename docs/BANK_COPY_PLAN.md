# Whole-Bank Copy via the App (RAM ⇄ Card, RAM → RAM)

Status: implemented on branch copy-bank, 2026-11-12. Deviations: the CLI
requires --yes like every other destructive write; the bridge and demo
results carry the written bytes ("written") so the app updates caches and
the card archive without re-reading. The two hardware questions are
resolved, and the panel function turned out to be remotely drivable: four
documented parameter changes, measured (§1, §9). The copy window and CLI
offer the device move as an option (`m` / `--method device`), backed up and
verified by kwsed.

## 1. What the device offers, and why kwsed does not use it

The Wavestation SR's **MIDI/Global** menu has a **BANK MOVE** page (Reference
Guide) that moves bank data (WAVESEQ / PATCH / PERFORMANCE / ALL) between
banks, including to and from the Card. It is not panel-only: four documented
parameter changes drive it — `COPY_MACRO_SOURCE_BANK` (213),
`BANK_COPY_TYPE` (346), `GLOBAL_UTIL_DEST_BANK` (379) and
`EXECUTE_BANK_MOVE` (472), which executes on `+1`. The four were captured
from the unit's own panel action and replayed with no panel action;
`tools/probe_bank_copy.py move` is the reproduction, and §9 has the measured
results.

| | The unit's bank move, driven remotely | kwsed does the copy itself |
|---|---|---|
| Protocol | Four documented parameter changes (213/346/379/472), measured | Uses only documented requests + dumps |
| Backup | None — the destination is overwritten with no undo | Full envelope via `write_bank_bundle`'s fence |
| Verify | None | Read-back compare of every kind |
| Source | Left in place on the measured unit — behaviour, not a guarantee | Untouched: kwsed reads it |

**Decision: kwsed synthesizes the operation** — read the source bank in full,
write the destination through the existing fence. This is the same shape the
app already uses for imports and `copy-perf`. The device's own move is
reachable, but it has no backup and no verify, and on the measured unit it
left the source in place — observed twice, and not something to depend on.
The device function stays available from the panel.

## 2. Scope

Three directions, from one browser action (`B` — "Copy bank"; the source is
the bank under the cursor):

* RAM 1/2/3 → Card
* Card → RAM 1/2/3
* RAM → RAM (any writable source to any other writable destination)

All **three kinds always** (wave sequences, patches, performances). A partial
copy (performances without their wave-sequence pool) silently changes how the
destination sounds, so there is no kind selector. Slot-preserving partial
copying remains `copy-perf`'s job; whole-bank copying is this feature's job.

## 3. Bridge: `WavestationBridge.copy_bank` (`kws/bridge.py`)

```python
def copy_bank(
    self,
    source: int | str,
    destination: int | str,
    *,
    backup_dir: str,
    verify: bool = True,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
```

1. **Refusals before any traffic**: source or destination ROM;
   `source == destination`.
2. **Read the source, all three kinds** (`ALL_WAVESEQ_DUMP`,
   `ALL_PATCH_DUMP`, `ALL_PERFORMANCE_DUMP`), each length-checked against
   `_BUNDLE_SIZES`. Any read failure → raise, nothing sent: a copy that
   cannot be read is not one that should half-erase a destination.
3. **Remap the performance bank's patch targets** (§4): every part in the
   copied performances that names the **source bank itself** is rewritten to
   name the **destination bank**. This is what makes the device's copy behave
   like a real copy and not an alias.
4. **One `write_bank_bundle(destination, ...)` call** — the fence (ROM
   refusal, one destination backup envelope, waveseq → patches →
   performances send order, per-kind read-back verify) is already there.
5. Return the bundle result dict (`verified`, `kinds`, backup path).

Wave-sequence **numbers** need no remapping: each bank carries its own
32-slot pool, so a full-bank copy carries the pool 1:1. Patch records need no
remap either: their oscillator fields point at **waves in PCM ROM**, which do
not move when a bank is copied.

## 4. The remap (`kws/records.py`)

The hard part is already solved and documented at the top of
`kws/records.py`:

> **A part's bank number is split across two fields.** Four of its bits live
> in `bankNum` and the top two bits of `partMode` carry the rest, so the
> bank is `bankNum + ((partMode >> 6) & 3) * 4`. Rewriting only `bankNum`
> yields a bank that loads cleanly and plays sounds from somewhere else.

So the remap is a small pure function in `records.py`, next to the field
encoders it reuses:

```python
def remap_performance_banks(perf_bank: bytes, source: int, destination: int) -> bytes:
    """Point every part that names `source` at `destination`, both halves."""
```

* For each of the 50 performance records: `performance_parts(record)`; where
  `part.bank == source`,
  `set_performance_part(record, part.index, bank=destination, patch=part.patch)`
  — which correctly writes `bankNum` *and* the high bits in `partMode`.
* **Everything else stays**: parts pointing at ROM 4–11, at another RAM bank,
  or at the Card are untouched — the device does the same, and a RAM→RAM copy
  must not drag third-party references along.
* Banks here are **positions 0–11** (`banks.Bank.pos`: RAM1=0 … Card=11),
  matching `PartRef.bank`; the bridge translates wire byte ↔ position via
  `banks.by_sysex()` / `banks.Bank.sysex`.
* **Out of scope, deliberately**: Multiset pointers (`0x62`) are not
  remapped — the device does not remap them either, and Multisets are a
  separate bank of state the multi editor owns.

## 5. CLI: `kwscli copy-bank SRC DST`

`kwscli copy-bank RAM1 CARD` — two bank names through `banks.bank()`, one
`device.copy_bank(...)` call, print the result. Works for all three
directions for free.

## 6. UI (`kwsed/app.py`)

* **Key `B`** — "Copy bank", on the browser; source is the bank under the
  cursor (RAM 1–3 or Card — all three directions work from the same action).
* **`BankCopyScreen`** — a sibling of `SendScreen`, same deliberate three-step
  discipline (open sends nothing, arm sends nothing, `w` fires only while
  armed):
  1. **Read and show the destination's current names live** — "Card" means
     nothing; the names it is about to replace mean everything.
  2. **Arm** (`a`).
  3. **Fire** (`w`, only while armed).
  The destination is a picker over the other three writable banks; the screen
  shows that a backup envelope will be written first.
* **Gates**: write gate (`EditGate`) refused when locked; `card_mode = "none"`
  refuses when the Card is either end; `_copy_bank_worker` copies
  `_copy_perf_write_worker`'s exchange/gate/lock/thread pattern exactly.
* **After a verified write**:
  * destination **Card** → **auto-adopt**: re-digest the Card's new content
    and update the active card's record in `physical-cards.json` (label, id,
    favourites kept — the picker's "adopt" path, run automatically), refresh
    the browser row;
  * destination **RAM** → no archive involvement.
  * In both cases invalidate `self._sources` for the destination scope so
    imports and `copy-perf` re-read instead of trusting a stale cache.
* **Undo**: one envelope per bundle; `z` must restore the destination's old
  content — verify `_undo_write_bank` accepts the Card scope, fix there if
  not.

## 7. Tests

* **`remap_performance_banks`**: self-reference moved to the destination
  (including **Card ↔ RAM**, which exercises the split-field high bits in
  `partMode`), ROM and other-bank parts untouched, other fields
  bit-identical.
* **Bridge** (`Unit` answers per bank byte): happy path RAM1→Card and
  Card→RAM1; remapped bytes arrive at the destination; source read failure →
  nothing sent; ROM/self refusals; verify failure reported.
* **Fence** (`tests/test_write.py`): the backup envelope holds the
  destination's *old* content for all three directions.
* **App**: screen opens sending nothing; arm; fire writes; gate locked
  refuses; `card_mode = "none"` refuses; destination cache invalidated.
* **CLI**: argument validation, both directions.

## 8. Order of work

1. `remap_performance_banks` in `records.py` + tests (pure bytes, no device).
2. `copy_bank` in the bridge, using it + bridge/write tests.
3. `kwscli copy-bank` + test.
4. `BankCopyScreen`, worker, gates + app tests.
5. Card auto-adopt + destination cache invalidation.
6. Undo-over-Card verification.
7. Docs: a short section in `docs/CARDS.md` (what a copy does to the
   archive), a resolution note on why kwsed does not drive the panel
   function, TODO.md.

## 9. Hardware questions, answered (bench unit, 2026-11-12)

Both open questions were measured on a real Wavestation SR (global channel 5,
reproduced by `tools/probe_bank_copy.py move`):

* **Card → RAM remaps** — yes, symmetrically with RAM → RAM. Card (HCard
  bank 39) → RAM 3, type 3 moved all three kinds and rewrote the
  performance's part 0 from the source to the destination; part 1 (ROM 4)
  was untouched, and the source Card stayed byte-identical.
* **Kind order** — there is none to match. The unit's MIDI OUT during the
  panel action carried only the parameter-change echo of 213/346/379/472
  plus Active Sensing, no bulk dumps. kwsed's waveseq → patches →
  performances order is its own wire protocol; the per-kind read-back
  verify decides correctness.

Also measured: `BANK_COPY_TYPE` (346) is 0 performances, 1 patches, 2 wave
sequences, 3 all three. Banks travel as the SysEx byte, so RAM 1 is 0,
RAM 2 is 1, RAM 3 is 4 and the Card is 3. The move did not clear the
source, tested twice. The panel function has no backup and no read-back
verify.
