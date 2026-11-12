# FX Editing (Performance Effects)

Status: design draft, 2026-11-12. Nothing here is implemented yet; step 0
confirms the byte layout before any of it is built.

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

## 2. Scope

**In scope**, for one performance in a writable bank (RAM 1–3 or the Card):

- FX 1 and FX 2: type (56 names), per-type parameters, and the
  series/parallel routing and mix between them.
- Reading the performance, editing it in memory, one fenced write,
  read-back verify, and one undo.
- A CLI reader/writer for scripts, and a TUI screen for editing.

**Out of scope, deliberately:**

- Patch settings and the 74 non-reference bytes of a patch. Patches carry
  no FX on this instrument; a patch's "effect" is whatever the performance
  running it applies.
- Wave sequencing and the step pool (a separate editing job).
- Multisets: they select performances and carry no FX of their own.
- Live automation of FX parameters while a note sounds.
- ROM performances: refused, as every write is.

**The layout is a hypothesis until step 0 confirms it.** The working
assumption is:

```
offset 16  routing / chain     1 byte   (series vs parallel, mix)
offset 17  FX 1 type           1 byte   (0-55)
offset 18  FX 1 parameters     9 bytes  (packed, group-dependent)
offset 27  FX 2 type           1 byte
offset 28  FX 2 parameters     9 bytes
```

That is 21 bytes and it matches Edisyn's storage-block indices (0–8 per
processor) plus a type byte per processor. The routing byte is the one
piece without a citation; the plan treats it as the first thing to prove.

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

class PerformanceFx:
    series: bool
    fx1: Effect
    fx2: Effect
    @classmethod
    def from_record(cls, record: bytes) -> "PerformanceFx"
    def to_record(self, record: bytes) -> bytes
```

- `FX_BASE = 16`, `FX_SIZE = 21` next to the existing record offsets in
  `records.py`, and `from_record`/`to_record` rewrite only those bytes.
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

1. Read a spread of performances (factory RAM/ROM and Card) and extract
   bytes 16–36.
2. Group them by the byte that looks like the type (0–55), and check the
   group count and parameter ranges against Edisyn's `FX_GROUP`,
   `FX_PARAMETERS` and `FX_PCL_LIST`.
3. Identify the routing byte: compare performances whose panel shows
   series vs parallel; confirm the 0/1 (or two-value) encoding.
4. Confirm the parameter block is 9 bytes per processor and that the
   bit fields in `FX_PCL_LIST` land inside it.
5. Write **one** performance with a known FX change through the existing
   `write_performance`, read it back, and confirm the changed byte and
   the unchanged neighbours.
6. Record the result in `docs/RESOLUTION_NOTES.md` with the wrong turns,
   as the other layouts are recorded.

If the split is wrong, the model changes and the rest of the plan is
unaffected; the screen is built from the tables, not from the hypothesis.

## 6. Bridge

No new stored-write path is needed:

- `read_performance(bank_sysex, number)` already returns the 181 bytes.
- `write_performance(bank, number, record, backup_dir=..., verify=True)`
  already backs the slot up, dumps it, commits with `EXECUTE_WRITE`, and
  reads it back. One call is one undo record.

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
row opens `FxScreen`. Refused on a patch row ("patches carry no effects;
a performance's FX apply to it"), on ROM, and under the same card-mode /
HCard read-only gates as the copy actions.

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

- Rows come from `PerformanceFx.rows()`; a type change rebuilds the
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
- Opening the screen sends nothing; it reads the performance (or uses the
  cached source) before showing values. The footer says what was read and
  whether it is cached.

## 8. CLI

```
kwscli fx RAM1 12                 # print routing, both types and parameters
kwscli fx types                   # the 56 names and their groups
kwscli fx RAM1 12 --set fx1.type="Small Hall" \
                  --set fx1.decay=24 --yes
```

- `--set` names a row by its table key; unknown keys and out-of-range
  values are usage errors before any traffic.
- Writing requires `--yes`, like every other destructive command; the
  write goes through `write_performance`, so it is backed up and verified.
- `fx` with no `--set` is read-only and needs no gate.

## 9. Tests

- **Pure** (`tests/test_fx.py`): 56 types map to 31 groups; every group's
  rows fit 9 bytes with non-overlapping masks; `from_record`/`to_record`
  round-trip and touch only bytes 16–36; display/encode round-trips for
  every row, including signed and enumerated values; hypothesis over raw
  bytes for the codec.
- **Bridge**: `write_performance` is already fenced and tested; add one
  FX-specific test that a changed FX byte arrives and its neighbours do
  not.
- **App** (`tests/test_app_ui.py` or a new `test_fx_ui.py`): opening the
  screen sends nothing; a type change asks first; an edit plus fire sends
  exactly one performance write; the locked gate refuses; `z` restores
  the previous performance; a patch row refuses.
- **CLI** (`tests/test_cli.py`): `fx` prints the blocks; `--set` without
  `--yes` sends nothing; an unknown row is a usage error; a demo write
  verifies.
- **Hardware** (`tests/test_hardware.py`, opt-in): read one performance,
  change one FX value, write, read back; assert the byte and the
  unchanged neighbours. This is the same shape as the existing spare-slot
  write test.

## 10. Order of work

1. **Step 0**: derive and confirm the 21-byte layout on the bench; record
   it. No code.
2. `kws/fx.py`: tables, codec, `PerformanceFx`, tests. Pure bytes.
3. `kwscli fx` read-only (`fx`, `fx types`) and tests.
4. `FxScreen` + read worker + type chooser + confirmation; app tests for
   "opens sending nothing".
5. Write path through `write_performance`, arm/fire, gate, undo; app and
   CLI `--set` tests.
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
