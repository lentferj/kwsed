# FX Editing (Performance and Multiset Effects)

Status: core implemented 2026-11-12; the hardware confirmation (step 0)
is still open. Done: `kws/fx.py` (tables and the 21-byte codec), the
Multiset block in `kws/multi.py`, read-only `kwscli fx`, the read-only then
editable `FxScreen` for performances and Multisets, and the fenced writes
through `write_performance` / `write_multi`. Not done: CLI writes
(`--set`, `--copy-from`, `--control-channel`), Copy FX in the UI, the
hardware probe, and the step-0 confirmation.

## 1. What the device offers

A Wavestation **performance** carries its effects, not its patches:

```
Performance, 181 bytes = 16 name + 21 effects + 8 parts x 18
```

The 21 effect bytes are the only unmodelled region that matters for
editing a sound (`TODO.md` item 2). They hold **two processors** (FX 1 and
FX 2), the routing between them, and each processor's type and parameters.

There are **56 effect types**, including "Off", organised into **31
parameter groups**: effects in one group share a parameter layout, so the
screen can ask the model for the rows instead of hard-coding them. Eight
of the 56 are SR-only extended effects. Edisyn's Wavestation editor carries
the machine-readable tables (`FX_GROUP`, `FX_PARAMETERS`, `FX_INDICES`,
`FX_PCL_LIST`, `FX_PRESETS`); the manuals are image-only scans, so those
tables are the transcription source, taken as **facts only** under the
policy in `docs/RESOLUTION_NOTES.md` §2.

The stored bytes are committed by a **performance write** — a single
performance dump into the edit buffer, then `EXECUTE_WRITE` — which
`WavestationBridge.write_performance` already performs through the fence
(one backup envelope, read-back verify, one undo record). Parameter
changes (`0x41`/`0x42`/`0x43`) can also address FX parameters live, but
whether the SR commits those to the stored performance without a
performance write is unmeasured.

### In a Multiset

A **Multiset has its own FX block, and it is the same 21 bytes.** Korg's
Multi Mode Setup structure is documented as:

```
byte 0        FX control channel
bytes 1-21    the effects block (21 bytes)
bytes 22-85   16 channels x 4 bytes
```

which is exactly the arithmetic `kws/multi.py` already uses
(`PART_BASE = 22`, `MULTISET_SIZE = 86`, names at 2752). kwsed currently
keeps those 22 bytes opaque (`Multiset.settings`): the data survives a
round trip, but it cannot be read or edited.

**Which pair is heard depends on the MIDI mode.** In OMNI/POLY the
performance's effects are heard; in MULTI/MONO the Multiset's own effects
are heard and **override** the performances' (Player's Guide §7.1), and
the panel only lets you edit the set currently in use. The **FX control
channel** (byte 0) selects which channel's performance supplies the
modulation/control for the Multiset's effects (§5.9), and the panel's
**Copy FX from Performance** (§5.8) copies a performance's 21 bytes into
a Multiset.

So FX editing has **two targets sharing one codec**: a performance
(bytes 16-36) and a Multiset (bytes 1-21, plus the control channel at 0).
The unit's MIDI mode is readable (`MIDI_MODE_PARAMETER` 4), so the editor
can say which set is heard and which one it is editing.

## 2. Scope

**In scope**, for a performance in a writable bank (RAM 1–3 or the Card),
and for a Multiset:

- FX 1 and FX 2: type (56 names), per-type parameters, and the
  series/parallel routing and mix between them.
- The **same block in a Multiset**, plus its FX control channel, written
  through `write_multi`.
- Reading the record, editing it in memory, one fenced write, read-back
  verify, and one undo.
- Copying a performance's FX block into a Multiset (the panel's
  "Copy FX from Performance"), through the same fence.
- A CLI reader/writer for scripts, and a TUI screen for editing.

**Out of scope, deliberately:**

- Patch settings and the 74 non-reference bytes of a patch. Patches carry
  no FX on this instrument; a patch's "effect" is whatever the performance
  running it applies.
- Wave sequencing and the step pool (a separate editing job).
- Live automation of FX parameters while a note sounds.
- ROM performances: refused, as every write is.

**The 21-byte layout is transcribed, not guessed.** Edisyn's record
builder (`KorgWavestationPerformance.buildPerformance`) writes it field by
field, and the arithmetic closes exactly:

```
byte 0    FX 1 type (bits 0-5, stored = type + 2)
          bit 6: sign of FX 1 mix-mod amount
          bit 7: series routing flag
byte 1    FX 2 type (bits 0-5, stored = type + 2)
          bit 6: sign of FX 2 mix-mod amount
          bit 7: always 1 (extended-effects enable)
byte 2    FX 1 mix amount (low nibble) / FX 2 mix amount (high nibble)
byte 3    FX 1 mix-mod source (low) / FX 2 mix-mod source (high)
byte 4    |FX 1 mix-mod amount| (low) / |FX 2 mix-mod amount| (high)
bytes 5-12   FX 1 parameter block, 8 bytes
bytes 13-20  FX 2 parameter block, 8 bytes
```

Extended effects (types 48-55) are encoded as type byte 2 plus
`type - 47` in the low bits of the processor's first parameter byte
(byte 5 for FX 1, 13 for FX 2); normal types store `type + 2`.

Step 0 is therefore a **confirmation on the unit**, not a derivation: read
a performance, check the bytes against the panel and a known factory
sound, then write one value and read it back. A Multiset carries the same
block at bytes 1–21; its byte 0 is the FX control channel, not part of the
block.

## 3. What to take from the templates

| Template | What it does | What kwsed should take |
|---|---|---|
| **ensqsqed** (`ensqsqed/fx.py`, `sq/presets.py`) | A full FX screen over one preset; the model owns the rows (`Effect.rows`, `display`, `with_value`); type changes are a separate, confirmed edit; the screen owns no device | The **model-driven table** and the **type-change confirmation**; the "screen says what it did, the app says whether it landed" split |
| **rxved** (`xv/effects.py`, `rxved/screens.py` `EffectsScreen`) | Per-algorithm parameter tables with display strings; block/head rows then algorithm rows; a type change rebuilds the table; writes go through a worker and re-read | The **parameter table shape** (offset, label, range, display table) and the **rebuild-on-type-change** behaviour |
| **p2ked** (`p2ked/masteredit.py`, `p2ked/app.py`) | A table of named parameters with ranges and notes; read every value first, then build the screen; one write per edit through the gate and undo log; the app sends, the screen only offers | The **read-all-then-edit** worker and the **gate + undo-log** discipline |

The family already shares the rest (gate, legend, modals, work pattern), so
the FX screen should look and behave like `MultiEditScreen` and the copy
screens, not like a new invention.

## 4. Data model — `kws/fx.py`

Pure bytes and tables, no MIDI, no Textual:

```python
@dataclass(frozen=True)
class EffectType:
    number: int          # 0-55
    name: str            # "Small Hall", "Stereo Vocoder-Delay 2"
    group: int           # 0-30, which parameter layout applies

@dataclass(frozen=True)
class EffectParam:
    offset: int          # 0-8 inside the processor's parameter block
    key: str             # "wet_dry", "decay", "mod_source"
    label: str           # what the screen prints
    low: int
    high: int
    bias: int = 0        # stored = displayed + bias
    names: Optional[Tuple[str, ...]] = None   # enumerated values
    mask: int = 0xFF     # several parameters share a byte
    shift: int = 0

class Effect:            # one processor
    type: int
    raw: bytes           # 9 bytes
    def rows(self) -> Tuple[EffectParam, ...]
    def display(self, row) -> int | str
    def with_value(self, row, value) -> "Effect"

class FxBlock:           # one 21-byte block, performance or Multiset
    series: bool
    fx1: Effect
    fx2: Effect
    @classmethod
    def from_bytes(cls, block: bytes) -> "FxBlock"
    def to_bytes(self) -> bytes

class MultisetFx:        # a Multiset's block plus its control channel
    control_channel: int
    block: FxBlock
    @classmethod
    def from_record(cls, record: bytes) -> "MultisetFx"
    def to_record(cls, record: bytes) -> bytes
```

- Offsets next to the existing ones: `PERFORMANCE_FX_BASE = 16` in
  `records.py`, `MULTISET_FX_BASE = 1` in `multi.py`; `FX_SIZE = 21`.
  `FxBlock.from_bytes`/`to_bytes` work on the slice, so the two records
  share one codec and one table set.
- `kws/multi.py` replaces the opaque `Multiset.settings` with the modelled
  `control_channel` and `fx` fields. Its 22 bytes are now fully decoded;
  the round trip must still be byte-identical when nothing is edited.
- Every range, mask, shift and display name lives in the tables; the
  screen and the CLI must not carry a second copy.
- Enumerated values display by name; signed values show their sign;
  values with a non-linear display table (the family has one) go through
  the table, never through a formula.
- Property test: for every type, every row's mask fits its byte, masks in
  one byte do not overlap, and `with_value(row, display(row))` is a
  no-op.

## 5. Step 0 — derive and confirm the layout (before code)

The method that worked for the rest of the record: derive a candidate
structurally, then check it field by field against Edisyn and the unit.

1. Read a spread of performances (factory RAM/ROM and Card) and compare
   bytes 16–36 with the transcribed layout above.
2. Group them by the byte that looks like the type (0–55), and check the
   group count and parameter ranges against Edisyn's `FX_GROUP`,
   `FX_PARAMETERS` and `FX_PCL_LIST`.
3. Identify the routing byte: compare performances whose panel shows
   series vs parallel; confirm the 0/1 (or two-value) encoding.
4. Confirm the parameter block is 9 bytes per processor and that the
   bit fields in `FX_PCL_LIST` land inside it.
5. Do the same for a Multiset: extract bytes 1–21 of its 86-byte record,
   confirm the block has the same meaning as a performance's, and
   identify the FX control channel in byte 0 (edit it, read the Multiset
   back).
6. Write **one** performance with a known FX change through the existing
   `write_performance`, read it back, and confirm the changed byte and
   the unchanged neighbours. Repeat for a Multiset through `write_multi`
   on a spare set.
7. Record the result in `docs/RESOLUTION_NOTES.md` with the wrong turns,
   as the other layouts are recorded.

If the split is wrong, the model changes and the rest of the plan is
unaffected; the screen is built from the tables, not from the hypothesis.

## 6. Bridge

No new stored-write path is needed:

- `read_performance(bank_sysex, number)` already returns the 181 bytes.
- `write_performance(bank, number, record, backup_dir=..., verify=True)`
  already backs the slot up, dumps it, commits with `EXECUTE_WRITE`, and
  reads it back. One call is one undo record.
- `read_multi()` and `write_multi(data, backup_dir=..., verify=True)` do
  the same for the whole Multi Mode Setup. A Multiset FX edit rewrites
  the 21-byte block in the parsed 32-record dump and writes the setup
  once: one backup, one verify, one undo — slower than a performance
  write because the whole setup travels, but the same fence.

If a **live** path is added later, it is a separate method:

```python
def write_performance_effect(self, bank, number, parameter, value, ...)
```

sending one `parameter_change` and re-reading the performance to verify.
It must be measured first: the SR may keep the change in the edit buffer
until a performance write, and a live change that is not stored is a
different promise from a stored edit. The default path stays the stored
one; the live path is opt-in and gated like the staging writes.

## 7. UI

**Browser key `x`** (free; `X` is already card sweep) on a **performance**
row opens `FxScreen`; the same key inside the Multiset editor (`e`) opens
it for the selected Multiset. Refused on a patch row ("patches carry no
effects; a performance's FX apply to it"), on ROM, and under the same
card-mode / HCard read-only gates as the copy actions.

The screen starts with a **mode banner**, because which block is heard
depends on the unit's MIDI mode (readable at parameter 4):

- OMNI/POLY: "performance FX are heard; the Multiset's are not".
- MULTI/MONO: "the Multiset's FX are heard and override the
  performances'".

Editing a block that is not currently heard is allowed — it is stored,
and the banner says so rather than letting the user think it is live.

`FxScreen` is a `MultiEditScreen`-shaped modal:

```
routing    Parallel        [Series | Parallel]
FX 1       Small Hall      [type: 56 names, confirmed on change]
  wet/dry  Dry -> Wet
  decay    2.4 s
  ...
FX 2       Off
  ...
```

- Rows come from `FxBlock`'s rows; a type change rebuilds the
  parameter rows and asks for confirmation first (the number and the
  bytes belong together; the unit reinterprets the old bytes).
- `enter` / a digit / `+`/`-` edit a value, as on the other screens.
- `w` toggles the write gate; `a` arms; `w` fires is **not** the copy
  screen's two-key dance: this screen edits in memory and writes one
  performance, so it uses the same arm/fire pair as `MultiEditScreen`.
- `z` undo is the app's existing log; one performance write is one
  record.
- The screen owns no device: `on_read` and `on_write` callbacks, the app's
  workers do the MIDI, and the status line says whether the write landed.
- Opening the screen sends nothing; it reads the record (or uses the
  cached source) before showing values. The footer says what was read and
  whether it is cached.
- For a Multiset, one extra row above FX 1: **FX control channel**
  (1–16). It is part of the Multiset's record, not of the block.
- A **Copy FX from Performance** action takes a performance's 21-byte
  block into the Multiset (the panel's §5.8 command), through the same
  fence; the reverse direction is the same codec and is offered too.
- The write is one `write_performance` for a performance, one
  `write_multi` for a Multiset; the footer says which, because the
  second is a whole-setup write and takes longer.

## 8. CLI

```
kwscli fx RAM1 12                 # print routing, both types and parameters
kwscli fx --multi 7               # the Multiset's block and control channel
kwscli fx types                   # the 56 names and their groups
kwscli fx RAM1 12 --set fx1.type="Small Hall" \
                  --set fx1.decay=24 --yes
kwscli fx --multi 7 --copy-from RAM1:12 --yes
kwscli fx --multi 7 --control-channel 3 --yes
```

- `--set` names a row by its table key; unknown keys and out-of-range
  values are usage errors before any traffic.
- Writing requires `--yes`, like every other destructive command; a
  performance write goes through `write_performance`, a Multiset through
  `write_multi`, so both are backed up and verified.
- `--copy-from BANK:NUMBER` copies a performance's 21 bytes into the
  target (performance or Multiset); `--control-channel` is Multiset-only.
- `fx` with no `--set`/`--copy-from`/`--control-channel` is read-only and
  needs no gate.

## 9. Tests

- **Pure** (`tests/test_fx.py`): 56 types map to 31 groups; every group's
  rows fit 9 bytes with non-overlapping masks; `FxBlock` round-trips and
  touches only its slice; `MultisetFx.from_record`/`to_record` round-trip
  the whole 86-byte record and preserve the control channel; display/
  encode round-trips for every row, including signed and enumerated
  values; hypothesis over raw bytes for the codec.
- **Bridge**: `write_performance` and `write_multi` are already fenced
  and tested; add one FX-specific test each that a changed FX byte
  arrives and its neighbours do not, and that a Multiset write preserves
  the other 31 records.
- **App** (`tests/test_app_ui.py` or a new `test_fx_ui.py`): opening the
  screen sends nothing; the mode banner matches OMNI/POLY vs MULTI/MONO;
  a type change asks first; an edit plus fire sends exactly one
  performance write (or one `write_multi` for a Multiset); the locked
  gate refuses; `z` restores the previous record; a patch row refuses;
  Copy FX from Performance lands the same 21 bytes.
- **CLI** (`tests/test_cli.py`): `fx` prints the blocks; `--multi` prints
  a Multiset's block and control channel; `--set` without `--yes` sends
  nothing; an unknown row is a usage error; `--copy-from` and
  `--control-channel` round-trip; a demo write verifies.
- **Hardware** (`tests/test_hardware.py`, opt-in): read one performance,
  change one FX value, write, read back; assert the byte and the
  unchanged neighbours. This is the same shape as the existing spare-slot
  write test.

## 10. Order of work

1. **Step 0**: confirm the transcribed 21-byte block in both records
   (performance bytes 16–36, Multiset bytes 1–21 plus the control
   channel) on the bench; record it. No code.
2. `kws/fx.py`: tables, codec, `FxBlock`, `MultisetFx`, tests; model the
   Multiset's 22 bytes in `kws/multi.py`. Pure bytes.
3. `kwscli fx` read-only (`fx`, `fx --multi`, `fx types`) and tests.
4. `FxScreen` + read worker + type chooser + confirmation + mode banner,
   reachable from a performance row and from the Multiset editor; app
   tests for "opens sending nothing".
5. Write paths: `write_performance` and `write_multi`, arm/fire, gate,
   undo; Copy FX from Performance; app and CLI `--set`/`--copy-from`/
   `--control-channel` tests.
6. Card/HCard gates, cache refresh after the write, README keys and help.
7. Docs: `README.md`, `TODO.md` item 2, `docs/RESOLUTION_NOTES.md` (the
   layout derivation), and a short "what a write reaches" note.

Each step is a commit that passes `make check` and the full suite.

## 11. Open questions and risks

- **The routing byte** is the one unconfirmed piece; step 0 settles it or
  changes the model.
- **The eight extended effects** may be selectable only in extended mode;
  confirm whether the panel and MIDI both reach them.
- **Type changes** keep the old parameter bytes until the unit
  reinterprets them (Edisyn's note; ensqsqed measured the same class of
  behaviour). The confirmation must say this rather than implying a clean
  reset.
- **Live parameter changes** may not commit without a performance write;
  do not promise live editing until measured.
- **Display tables** come from Edisyn's transcription; a value that the
  manual prints differently is a table bug, not a code bug. Keep the
  table in one place.
- **Card performances** are the same record; the gates are the existing
  ones, not new ones.
- **Effects on patches**: none. If a future measurement says otherwise,
  it changes scope, not the codec.
- **The FX control channel's exact meaning** (modulation/control source
  only, or also which performance's FX is used) is from the manual, not
  measured; step 0 changes it and reads the Multiset back, but the
  listening test in MULTI mode is the real proof.
- **A Multiset write is a whole-setup write.** `write_multi` sends all 32
  records; that is slower than a performance write and one undo restores
  the whole setup, not just the edited Multiset. The screen says so.
- **Which block is heard depends on the MIDI mode**, so a stored edit can
  be inaudible until the mode changes. The banner is the only honest way
  to present that; do not imply "written = heard".
- **The panel's Copy FX direction** is documented as performance →
  Multiset; the reverse is offered by kwsed because it is the same 21
  bytes, but the panel's behaviour is the one to match if they differ.


## Progress (2026-11-12)

Implemented and tested without the device:

- `kws/fx.py` + `kws/fx_tables.py`: 56 types, 31 groups, the PCL-joined
  parameter model (multi-field high bits and signs), the 21-byte block
  codec with lossless round-trip for arbitrary bytes.
- `kws/multi.py`: the Multiset's byte 0 control channel and bytes 1-21 as
  an `FxBlock`; round-trip tests include the all-zero demo block.
- `kwscli fx` (read): performance, Multiset and type list.
- `FxScreen`: read-only, then editable; type changes are confirmed; `a`/`w`
  write one performance through the fence; `x` in the Multiset editor
  writes the whole setup through `write_multi`.
- Tests: codec/property, multiset round-trip, CLI, app UI (read, edit,
  write, gate locked, failure, Multiset entry and write).

Still to do:

- CLI writes (`--set`, `--copy-from`, `--control-channel`).
- Copy FX from a performance into a Multiset in the UI (the panel's §5.8).
- `tools/probe_fx.py` and the hardware step-0 confirmation: read a
  performance and a Multiset, compare the bytes with the panel, write one
  value, read back. Until then the codec is transcribed, not measured.
