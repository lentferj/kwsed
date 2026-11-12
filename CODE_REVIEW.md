# kwsed Code Review

**Date:** 2026-11-12  
**Scope:** Full codebase review covering architecture, correctness, performance, security, maintainability, and consistency.

---

## Executive Summary

kwsed is a well-architected, production-quality terminal browser for the Korg Wavestation SR. The codebase demonstrates deep understanding of the hardware's SysEx protocol, careful handling of failure modes, and strong defensive programming practices. Key strengths include:

- **Protocol fidelity**: Exact SysEx framing, nibble encoding, and bank byte handling verified against hardware
- **Write fence discipline**: Every destructive operation backs up, writes, reads back, and verifies
- **Atomic copy semantics**: Performance-first copying with private patch copies prevents cross-contamination
- **Comprehensive test coverage**: Synthetic doubles that model hardware behavior including error replies

---

## 1. Architecture & Design

### Module Organization

| Module | Responsibility | Dependencies |
|--------|---------------|--------------|
| `kws/bridge.py` | MIDI transport, SysEx framing, write fence | `alsa`, `backup`, `banks`, `dumps`, `messages`, `records` |
| `kws/perfcopy.py` | Performance-first atomic bank planning/building | `banks`, `deps`, `dumps`, `records`, `relations` |
| `kws/deps.py` | Dependency graph, quoted sequence closure | `records` |
| `kws/records.py` | Binary record parsing/mutation | (stdlib only) |
| `kws/banks.py` | Bank definitions, scope/number/sysex mappings | (stdlib only) |
| `kws/importers.py` | File format readers (SYX, MIDI, SQS, LIB, ZIP, IMG) | `banks`, `deps`, `dumps`, `fat12`, `messages`, `multi`, `records`, `sounddiver`, `soundquest` |
| `kws/multistage.py` | Multi-card staging for Multisets | `banks`, `deps`, `multi`, `perfcopy`, `relations` |
| `kws/bankbuild.py` | Patch-first favourites bank builder | `banks`, `deps`, `records` |
| `kws/backup.py` | Backup envelopes, restore logic | `favorites`, `vinsynlib.backup` |
| `kws/cards.py` | HCard 40-bank table & archive | `banks`, `favorites` |
| `kws/physical.py` | Physical Program Card archive | `banks`, `favorites` |
| `kws/messages.py` | SysEx frame, functions, nibble encoding | (stdlib only) |
| `kws/scan.py` | TUI screens for empty catalog & card sweep | `textual` |
| `kws/catalog.py` | Thin binding to vinsynlib.catalog | `vinsynlib.catalog` |
| `kws/bankstore.py` | Per-scope bank cache (JSON + base64) | `banks`, `deps`, `favorites`, `records` |
| `kws/livenames.py` | Hardware-read name cache | `favorites`, `vinsynlib.livenames` |
| `kws/undo.py` | Undo log binding | `vinsynlib.undo` |
| `kws/fat12.py` | FAT12 floppy image reader | (stdlib only) |
| `kws/alsa.py` | ALSA sequencer sender for large dumps | `ctypes` |
| `kws/relations.py` | Cross-bank relations, structural digest | `banks`, `deps`, `dumps`, `multi`, `records` |
| `kws/multi.py` | Multiset (32×16) parse/encode | (stdlib only) |
| `kws/config.py` | Settings & UI state (TOML) | `banks`, `vinsynlib.config` |
| `kws/favorites.py` | Favorites DB binding | `vinsynlib.favorites` |

### Separation of Concerns

**Excellent**: The codebase maintains strict layering:
- **Wire layer** (`bridge.py`, `messages.py`, `alsa.py`): Pure MIDI/SysEx, no business logic
- **Record layer** (`records.py`, `dumps.py`): Byte-level field access, no MIDI
- **Dependency layer** (`deps.py`, `relations.py`): Graph walks, structural digests
- **Planner layer** (`perfcopy.py`, `bankbuild.py`, `multistage.py`): Allocation, budgeting, atomic builds
- **Storage layer** (`backup.py`, `bankstore.py`, `cards.py`, `physical.py`, `livenames.py`): Persistence
- **UI layer** (`kwsed/app.py`, `kwsed/import_*.py`, `kwsed/target_*.py`): Screens, bindings, workers

**Concern**: `kws/importers.py` spans multiple format readers (SYX, MIDI, SQS, LIB, ZIP, IMG, FAT12). While manageable at 720 lines, it could be split into format-specific modules for maintainability.

### Dependency Flow

```
User Input (TUI)
    ↓
Screen Workers (import_*, target_*)
    ↓
Planners (perfcopy, multistage, bankbuild)
    ↓
Dependency Walker (deps, relations)
    ↓
Record Mutators (records)
    ↓
Wire Sender (bridge → alsa/rtmidi)
    ↓
Backup/Verify (backup)
    ↓
Persistence (bankstore, cards, physical, livenames)
```

No circular dependencies detected. All imports flow downward through the layers.

---

## 2. Correctness

### Edge Cases & Protocol Compliance

#### ✅ Bank Byte vs Display Position (`kws/banks.py:98-112`)
**Critical correctness fix documented in code**: The 12 banks are **not** numbered 0-11 on the wire in display order. The Card is SysEx byte **3**, RAM 3 is **4**, ROM 11 is **2**. The code correctly uses `by_sysex()` to map dump bank bytes back to banks, preventing the historical bug where "every Card read was really ROM 10".

#### ✅ SysEx Frame Offsets (`kws/messages.py:226-240`)
`DATA_OFFSET` correctly distinguishes:
- Bulk dumps (ALL_PATCH_DUMP, ALL_PERFORMANCE_DUMP, ALL_WAVESEQ_DUMP): offset 6
- Single-object dumps (PERFORMANCE_DUMP, SINGLE_PATCH_DUMP): offset 7 (carries bank + number)
- MULTISET_DUMP: offset 7 (carries two header bytes)
- SYSTEM_SETUP_DUMP: offset 5 (no bank/number)

**Verified**: Tests confirm reading from wrong offset yields plausible but wrong data.

#### ✅ Nibble Encoding (`kws/messages.py:266-290`)
Low-nibble-first encoding/decoding (`from_nibbles`/`to_nibbles`) is correctly implemented and tested. This is a common source of bugs (values become multiples of 256 if reversed).

#### ✅ 16-bit Big-Endian Fields (`kws/records.py:129-136`)
`_u16`/`_put_u16` correctly handle big-endian words. The module docstring explicitly warns about the nibble/endian mismatch trap.

#### ✅ Part Bank Split (`kws/records.py:168-192`)
Part's bank number split across `bankNum` (2 bits) and `partMode` (top 2 bits). `set_performance_part` writes **both halves**, avoiding the bug where rewriting only `bankNum` "yields a bank that loads cleanly and plays sounds from somewhere else."

#### ✅ Patch Bank_Exp Split (`kws/records.py:264-280`)
`set_patch_wave` correctly handles the split between `waveNum`/`waveNumExp` and updates `PATCH_NUM_WAVES` to activate oscillators.

#### ✅ Step Loop Sentinel Preservation (`kws/perfcopy.py:590-607`)
`_step_link` preserves `0xFFFF` sentinel ("no loop link") instead of mapping to 0. The comment explains: "naive `moved.get(..., 0)` turned 'no loop link' into 0 -- pool step 0, which is another sequence's step or an empty one -- and a sequence that loops into an empty step is a quiet or truncated sequence."

#### ✅ Wave Sequence Quote Resolution (`kws/deps.py:206-236`, `kws/records.py:330-343`)
Quoted sequences (wave < `ROM_WAVE_BASE`) are transitively closed and renumbered during relocation. `set_step_wave` explicitly documents: "Below `ROM_WAVE_BASE` this field names a wave sequence... so it needs renumbering when steps are moved into a new pool for exactly the same reason the links do."

#### ✅ Checksum Inclusion (`kws/messages.py:367-391`, `kws/bridge.py:1194-1197`)
Checksum covers header bytes + nibble data. `write_multi` verifies: "The unit's header is 00 01, so the header-inclusive sum differs from the nibbles-only one: a checksum that omitted the header would be rejected by the unit with CHECKSUM ERROR."

### Data Integrity Risks

#### ⚠️ `CARD_NAME_OFFSET = 0` Unconfirmed (`kws/bridge.py:120-131`)
```python
CARD_NAME_OFFSET = 0
CARD_NAME_LEN = 16
```
**Comment**: "CONFIRM AGAINST HARDWARE: the offset is the one wire fact this feature does not yet have. It is 0 until the read-only probe is compared against a real unit."

**Risk**: If the actual offset differs, `read_card_name` may read garbage or miss the name entirely. The fallback logic (accept first 16 bytes only if they "look like a name") mitigates but doesn't eliminate risk.

**Mitigation**: The probe tool `tools/probe_card_name.py` exists but hasn't been run against hardware. This should be prioritized.

#### ⚠️ `SINGLE_OBJECT_TIMEOUT = 15.0` Hardcoded (`kws/bridge.py:95-105`)
Measured at ~9.9s consistently, but 15s may be insufficient on slower interfaces or under load. The comment explains the history: "The old value... was DEFAULT_TIMEOUT, two seconds, which cut every one of them off."

**Suggestion**: Make this configurable or adaptive with exponential backoff.

#### ⚠️ Multiset Unmodelled Tail (`kws/bridge.py:1173-1246`)
```python
# The 0x62 structure is longer than the part kwsed models: the
# unit's own dump decodes to 3777 bytes, of which the first 3264
# are the Multisets and their names. The rest is preserved from the
# pre-write read rather than zeroed.
```
**Risk**: If the unmodelled tail (513 bytes) contains meaningful data that changes between reads, verification may report "tail differs" without failing the write. This is acknowledged but could mask hardware state changes.

#### ⚠️ Wave Sequence Bank Truncation (`kws/deps.py:295-309`)
```python
elif packet.function == WAVESEQ_DUMP:
    data = packet.data()
    if len(data) >= rec.WAVESEQ_BANK_SIZE:
        source.waveseq = data[: rec.WAVESEQ_BANK_SIZE]
    else:
        source.waveseq = data if data else None
```
Short wave sequence dumps are accepted (truncated or partial). This could lead to incomplete step pools. The `load_syx` function in `deps.py` rejects short dumps (`len(data) >= rec.WAVESEQ_BANK_SIZE`), creating an inconsistency.

### Error Handling

#### ✅ Refusal vs Timeout Distinction (`kws/bridge.py:210-219`, `699-703`)
```python
if packet.is_error:
    raise DeviceRefused(packet.function)
```
Error replies (WRITE_ERROR, DATA_LOAD_ERROR) raise immediately with context, rather than waiting out the timeout. This is critical for UX: "a refusal is a different problem from silence and points somewhere different."

#### ✅ Stale Reply Rejection (`kws/bridge.py:712-713`, `779-780`)
Both `read_bank` and `read_patch` verify `packet.bank == entry.sysex` and `packet.number == requested_number`. This prevents late replies to earlier requests from being filed under the wrong bank/slot — the root cause of historical "Card 12 came back holding Card 3" bugs.

#### ✅ ROM Write Prevention (`kws/bridge.py:829-830`, `1017-1018`)
```python
if not entry.writable:
    raise DeviceError(f"{entry.label} is ROM and cannot be written.")
```
Checked before any MIDI traffic.

#### ✅ Backup-Before-Write Enforcement (`kws/bridge.py:847-856`, `1029-1035`)
```python
try:
    before_bytes = bytes(self.read_patch(entry.sysex, number, timeout=timeout))
except Exception as exc:
    raise DeviceError(
        f"could not read... before writing it, so nothing was sent ({exc})."
    ) from exc
```
**No write proceeds without a successful pre-read backup.** This is the core of the write fence.

---

## 3. Performance

### Unnecessary Allocations

#### ✅ Bytearray for Mutation (`kws/perfcopy.py:339-341`, `kws/bankbuild.py:226-228`)
```python
patches = bytearray(template_patches)
performances = bytearray(template_performances)
waveseq = bytearray(template_waveseq)
```
Mutations use bytearray; only final output is `bytes()`. Good.

#### ⚠️ Repeated `bytes()` Conversions in Hot Paths
`kws/bridge.py:879-881`:
```python
self._send(m.single_patch_dump(entry.sysex, number, data, channel=self.channel))
self._send(m.execute_write(channel=self.channel))
```
`data` is already `bytes` (validated at line 837), but `m.single_patch_dump` calls `list(to_nibbles(data))` which iterates. Acceptable for single-patch writes.

#### ⚠️ `plan_atomic_bank` Complexity (`kws/perfcopy.py:142-286`)
Marked `# noqa: C901` (too complex). The function does planning, budget checking, dependency closure, and commit in one pass. Could be split into phases for readability, though performance is fine for interactive use (≤50 performances).

### Redundant I/O

#### ✅ Input Drain Before Every Request (`kws/bridge.py:401-403`, `690`, `753`, `938`, `1111`)
```python
def _drain(self) -> None:
    while self._in.get_message() is not None:
        pass
```
Prevents stale replies. Called before every request. Correct and necessary.

#### ✅ Cache-First Bank Loading (`kwsed/app.py`, `bankstore.py`)
Banks loaded from local JSON cache before requesting from hardware. `bankstore.has_complete_bank()` avoids redundant reads.

#### ⚠️ Repeated `read_patch` in `write_patch` Verification (`kws/bridge.py:911-919`)
```python
for attempt in range(2):
    if attempt:
        time.sleep(0.25)
    after = self.read_patch(entry.sysex, number, timeout=timeout)
```
Two full read-backs (each ~10s) for verification. The retry handles "unit still committing" but adds ~10s worst case. Could be optimized with a shorter timeout for the second attempt since we know the slot.

### Caching Effectiveness

#### ✅ `RelationGraph` Caches (`kws/relations.py:273-274`, `283-321`, `323-362`)
```python
self._perf_cache: Dict[PerfId, PerformanceRelation] = {}
self._multi_cache: Dict[int, List[MultiChannelRelation]] = {}
```
Performance relations and multi-channel relations cached per session. Good for dependency pane.

#### ✅ `bankstore.load_all()` (`kws/bankstore.py:261-280`)
Loads all cached banks at once for browser population. Single directory scan.

#### ⚠️ No Invalidation Strategy for `bankstore` (`kws/bankstore.py`)
Cache files are written atomically but never automatically invalidated. If hardware state changes externally (another editor, front panel), the cache becomes stale. The UI provides "Read names" (R) to refresh, but no TTL or version check exists.

**Suggestion**: Add a "cache age" display or auto-refresh option.

---

## 4. Security/Safety

### Input Validation

#### ✅ Bank/Slot Range Checks (`kws/banks.py:239-246`, `kws/records.py:184-188`, `260-263`, `317-320`, `349-351`)
All public APIs validate bank numbers (0-11), performance slots (0-49), patch slots (0-34), sequence slots (0-31), step indices (0-500).

#### ✅ SysEx Payload Validation (`kws/messages.py:394-414`)
```python
for byte in payload:
    if not 0 <= byte <= 0x7F:
        raise ProtocolError(...)
```
Every byte in a SysEx frame must have bit 7 clear.

#### ✅ Nibble Validation (`kws/messages.py:275-279`)
```python
for part in (low, high):
    if not 0 <= part <= 0x0F:
        raise ProtocolError(...)
```

#### ✅ Filename Sanitization (`kws/bankstore.py:54-69`)
```python
name = str(scope_id).replace("/", "__")
name = name.replace(os.sep, "__")
if os.altsep:
    name = name.replace(os.altsep, "__")
name = os.path.basename(name)
```
Prevents directory traversal in cache filenames.

#### ⚠️ `config.py` TOML Injection Guard (`kws/config.py:412-421`, `424-444`)
```python
def _toml_scalar(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return None
```
Only scalar values written; tables/lists rejected. Prevents the "dotted key = TOML table" freeze bug.

### Hardware Write Protection

#### ✅ Write Fence (Backup → Send → Verify) (`kws/bridge.py:828-929`, `1000-1096`, `1132-1247`)
**Every destructive write follows this sequence:**
1. **Pre-read** destination slot/bank → backup file
2. **Send** dump + EXECUTE_WRITE (or equivalent)
3. **Wait** for WRITE_COMPLETED/WRITE_ERROR (optional, 2s)
4. **Read back** destination
5. **Compare** byte-for-byte with sent data
6. **Report** mismatch with first differing byte offset

#### ✅ Atomic File Writes (`kws/bankstore.py:220-234`, `kws/cards.py:654-674`, `kws/physical.py:283-301`)
```python
temp = where + ".tmp"
with open(temp, "w", ...) as handle:
    json.dump(...)
os.replace(temp, where)
```
Write-then-rename prevents half-written files on crash/power loss.

#### ✅ ROM Bank Write Refusal (`kws/bridge.py:829-830`, `1017-1018`)
```python
if not entry.writable:
    raise DeviceError(f"{entry.label} is ROM and cannot be written.")
```

#### ✅ Write Gate (`kwsed/gate.py`, `kwsed/app.py`)
```python
class EditGate:
    def __init__(self, *, armed: bool = False):
        self.armed = armed

    def toggle(self) -> bool:
        self.armed = not self.armed
```
UI requires explicit arming (`w` key) before any write (`ctrl+s`). `--allow-write` starts armed.

#### ⚠️ `save_backup=False` Opt-Out in `write_patch` (`kws/bridge.py:817-820`, `869-869`)
```python
save_backup = True  # default
# ...
if save_backup:
    # ... backup logic
```
Used by audition staging to avoid read-per-slot cost. **Documented risk**: "With it off nothing is read before sending and no envelope is written, so the write cannot be undone." The default keeps the fence intact.

### Backup Integrity

#### ✅ Envelope Format (`kws/backup.py` → `vinsynlib.backup`)
JSON with base64 blocks, format byte, never-overwrite filename, lenient listing. Includes device_id (channel), source ("kwsed"), timestamp.

#### ✅ Backup Content Verification (`tests/test_bridge.py:355-369`)
```python
packet = m.parse(backup.load(result["backup"]).blocks["dump"])
assert packet.data()[:8] == b"Previous", "the backup holds the old patch"
```

---

## 5. Maintainability

### Code Clarity

#### ✅ Extensive Inline Documentation
Module docstrings explain **why** decisions were made, not just what:
- `bridge.py`: "A bank dump takes seconds, not milliseconds... the reply says which bank it is; the request is not to be trusted for that."
- `records.py`: "Two traps... A part's bank number is split across two fields... 16-bit values are big-endian, while the nibble encoding on the wire is low-nibble-first."
- `perfcopy.py`: "Where `bankbuild` is patch-first and lets performances share patches, this module is performance-first..."

#### ✅ Type Hints Throughout
All modules use `from __future__ import annotations` and modern type hints (`Mapping`, `Sequence`, `Tuple`, `dataclass`). Good IDE support.

#### ⚠️ Complex Functions (C901)
| Function | Lines | Module |
|----------|-------|--------|
| `plan_atomic_bank` | 145 | `perfcopy.py` |
| `build_atomic_bank` | 178 | `perfcopy.py` |
| `plan_slot_import` | 298 | `perfcopy.py` |
| `build_slot_import` | 155 | `perfcopy.py` |
| `plan_multi_staging` | 232 | `multistage.py` |
| `build` | 156 | `bankbuild.py` |
| `action_fire` | 69 | `app.py` (MultiEditScreen) |

Marked `# noqa: C901`. While functional, these would benefit from decomposition into smaller helpers.

### Test Coverage

#### ✅ Comprehensive Test Suite (36 test files)
| Area | Test Files | Key Coverage |
|------|------------|--------------|
| Bridge/MIDI | `test_bridge.py`, `test_write.py` | Read/write fence, stale reply rejection, refusal handling, progress reporting |
| PerfCopy | `test_perfcopy.py` | Atomic allocation, intra/inter-perf dedup, wave sequences, budgets, slot import |
| Dependencies | `test_deps.py` | Closure, quoted sequences, walk_sequence stops, load_syx |
| Bankstore | `test_bankstore.py` | Round-trip, merge, wrong size rejection, corrupt tolerance, atomic write |
| Importers | `test_importers.py`, `test_import_*.py` | Multi-format, companions, audit |
| Hardware | `test_hardware.py` | Integration (requires hardware) |
| Cards | `test_cards.py`, `test_physical.py` | HCard/physical card archives, identity resolution |
| Config | `test_config.py` | TOML guards, migration, UI state separation |
| Messages | `test_messages.py`, `test_messages_refusals.py` | Frame parsing, nibble encoding, error codes |
| App/UI | `test_app.py`, `test_app_ui.py` | Screen flows, key bindings, workers |
| Relations | `test_relations.py` | Cross-bank deps, structural digest |
| Multistage | `test_multistage.py` | Multi-card staging for Multisets |
| Undo | `test_undo.py` | Undo log integration |

#### ✅ Synthetic Hardware Double (`tests/test_bridge.py:109-294`)
The `Unit` class models:
- Correct reply per request type
- Slot mutation on write (read-back sees new data)
- Error modes: `wrong_slot`, `write_error`, `read_error`, `foreign`
- Multi-message sequence (0x55, 0x5E, 0x62)

**This is the gold standard for MIDI tool testing.**

#### ⚠️ Missing: Property-Based Tests
No `hypothesis` tests for record mutation round-trips, nibble encoding, or dependency closure properties.

### Documentation

#### ✅ `docs/RESOLUTION_NOTES.md` Referenced
Multiple modules reference this for protocol decisions (e.g., Multi request code 0x06 vs inferred 0x32).

#### ✅ `docs/HCARD.md` Referenced
HCard multiplexing logic documented in sections 2 and 6.

#### ⚠️ `TODO.md` Referenced but Not Reviewed
`records.py:39` mentions "see TODO.md" for parameter layout. Should be checked for stale items.

---

## 6. Consistency

### Naming Conventions

#### ✅ Consistent Prefixes
- `read_*` / `write_*` / `send_*` in `WavestationBridge`
- `plan_*` / `build_*` / `copy_*` in planners
- `is_*` predicates (`is_empty_performance`, `is_user`)
- `_` prefix for module-private (`_DUMP_FOR_KIND`, `_step_link`)

#### ✅ Bank Identification
- `Bank.number` = display position (0-11)
- `Bank.sysex` = wire byte (0,1,4,5,6,7,8,9,10,11,2,3)
- `Bank.id` = short key ("RAM1", "CARD", "ROM4")
- `scope_id` = session key ("RAM1", "CARD/07", "CARD/c1")

#### ⚠️ Mixed `bank_id` vs `scope_id`
`deps.Source` uses `bank_id`; `relations.PerfId`/`PatchId` use `scope_id`. They represent the same concept but different names. `import_source.py:91` maps between them via `source_bank_number()`.

### Error Handling Styles

#### ✅ Exception Hierarchy
```
DeviceError (midi.DeviceError)
  └── DeviceRefused (function-specific)
MidiUnavailable (RuntimeError)
ProtocolError (ValueError)  -- wire format violations
SoundDiverError/UnsupportedFormat/UnsupportedBody (ValueError)  -- file format
BackupError (RuntimeError)
Fat12Error (ValueError)
```

#### ✅ Early Validation, Late Failure
Validation at API boundaries (constructors, public methods) with descriptive messages including valid ranges.

### Patterns

#### ✅ Dataclass for Structured Data
All major data structures use `@dataclass(frozen=True)` or mutable `@dataclass` with clear field semantics.

#### ✅ Context Managers for Progress/Resources
`_progress_scope`, `WavestationBridge` as context manager (via `open`/`close`).

#### ⚠️ `Any` Type in Bridge (`kws/bridge.py:273-274`, `349`, `448`)
```python
def __init__(self, midi_in: Any, midi_out: Any, ...)
```
`rtmidi` objects are `Any` due to optional dependency. Could use `Protocol` for structural typing.

---

## Key Questions Answered

### 1. Silent Data Corruption Risks?

**Yes, several:**

| Risk | Location | Severity |
|------|----------|----------|
| Unconfirmed `CARD_NAME_OFFSET` | `bridge.py:130` | Medium — misidentifies card bank |
| Short wave sequence dump accepted | `importers.py:270-271` | Low — partial bank, detected on use |
| Multiset unmodelled tail | `bridge.py:1173-1246` | Low — reported but not failed |
| `SINGLE_OBJECT_TIMEOUT` too short | `bridge.py:105` | Medium — could timeout on slow hardware |
| No cache invalidation | `bankstore.py` | Medium — stale names possible |

**Mitigations in place**: Read-back verification catches most write corruptions; structural digests detect content drift; "looks like a name" heuristic for card name.

### 2. Write Fence Protects Against All Failure Modes?

**Mostly yes, with caveats:**

| Failure Mode | Protected? | Notes |
|--------------|------------|-------|
| Power loss during write | ✅ | Atomic backup file + read-back verify |
| Hardware refuses write | ✅ | `DeviceRefused` raised, backup exists |
| Write succeeds but data corrupt | ✅ | Read-back comparison catches mismatch |
| Stale reply mistaken for current | ✅ | Bank/slot matching in read loops |
| ROM bank write attempted | ✅ | Pre-check refuses before any MIDI |
| Backup write fails (disk full) | ✅ | `BackupError` raised, write aborted |
| `save_backup=False` opt-out | ⚠️ | **Explicit opt-out** for audition; documented |
| `verify=False` opt-out | ⚠️ | **Explicit opt-out**; read-back skipped |
| Multi-write tail mismatch | ⚠️ | Reported but write succeeds |

**The fence is comprehensive for the default path.**

### 3. Sequence/Step Remapping Correct for All Edge Cases?

**Strong evidence of correctness:**

- ✅ Step link sentinel (`0xFFFF`) preserved via `_step_link`
- ✅ Quoted sequences transitively closed and renumbered
- ✅ Step pool sharing across sequences via `moved` map keyed by `(source_bank, wave_bank, step)`
- ✅ Forward/back/loop links remapped to new pool indices
- ✅ Sequence header `link` field updated to new first step
- ✅ Empty sequence slots zeroed (null link)

**Test coverage**: `test_perfcopy.py` has dedicated tests for:
- `test_moved_steps_keep_their_no_loop_sentinel`
- `test_waveseq` (quoted sequences)
- `test_relocation`
- `test_wave_sequences` (slot import with pinned sequences)

### 4. Race Conditions in Worker/Thread Model?

**No significant races found:**

- **Bridge sends**: `_send` uses a progress ticker thread but joins it before returning (`bridge.py:434-437`). The actual send (`_send_now`) is synchronous.
- **TUI workers**: `vinsynlib.ui.work.BusyGate` (`app.py:66`) serializes workers via `exchange.acquire(blocking=False)`.
- **Progress callbacks**: `_report` swallows exceptions (`bridge.py:321-322`): "A UI callback that raises must not turn into a failed write."
- **Cache writes**: Atomic rename (`bankstore.py:220-234`, `cards.py:654-674`) prevents torn reads.

**Potential issue**: `bridge.py:430-437` ticker thread has 0.5s join timeout. If send takes longer, ticker may not cleanly stop. But ticker is daemon and only updates progress — harmless.

### 5. Cache Invalidation Strategy Sound?

**No automatic invalidation.** The cache is explicitly user-managed:
- "Read names" (R) forces re-read from hardware
- Import writes to cache only
- No TTL, no version stamp, no hardware checksum comparison

**Acceptable for a browser** where user explicitly refreshes, but worth documenting.

### 6. Unimplemented TODOs Affecting Correctness?

| TODO | Location | Impact |
|------|----------|--------|
| `CARD_NAME_OFFSET` confirmation | `bridge.py:124-126` | Card identity resolution may misfile |
| Multi request code 0x06 vs 0x32 | `bridge.py:1107-1109` | **Resolved** — 0x06 confirmed |
| Parameter layout decoding | `records.py:39` | Limits editing, not browsing |
| HCard remote bank switch | `multistage.py:94` | Physical limitation, documented |
| Active card reading | `cards.py:430` | Cannot auto-detect; manual capture |

**Only `CARD_NAME_OFFSET` is a correctness risk.** Others are feature gaps or documented hardware limitations.

### 7. Hardware Quirks Handling?

| Quirk | Handled? | How |
|-------|----------|-----|
| Slow single patch read (~10s) | ✅ | `SINGLE_OBJECT_TIMEOUT = 15.0`, retry logic |
| Bank dump arrives during next request | ✅ | Drain before request + bank byte matching |
| Unit refuses bank dump (data format error) | ✅ | `DeviceRefused` raised immediately |
| EXECUTE_WRITE vs documented write command | ✅ | Uses Edisyn's path (no front-panel prompt) |
| Memory protect on ROM banks | ✅ | Pre-check refuses write |
| Card slot only answers when card present | ✅ | Special timeout message for Card bank |
| 30 kB dump exceeds rtmidi ALSA limit | ✅ | Falls back to `kws.alsa.Sender` chunked send |
| MIDI baud rate pacing (31250) | ✅ | `BYTES_PER_SECOND = 2800` (10% margin) |
| Kernel pool cap at 2000 cells | ✅ | `CHUNK = 256`, direct dispatch (`event_output_direct`) |
| EAGAIN on full driver buffer | ✅ | Retry loop with 30s deadline in `alsa.py` |

**Excellent hardware awareness throughout.**

---

## Recommendations

### High Priority

1. **Confirm `CARD_NAME_OFFSET` against hardware** — run `tools/probe_card_name.py` and update constant. This is the last unverified wire fact.

2. **Make `SINGLE_OBJECT_TIMEOUT` configurable** — add to config with default 15s, allow override for slow interfaces.

3. **Split `kws/importers.py`** — separate format readers into `importer_syx.py`, `importer_midi.py`, `importer_sqs.py`, `importer_lib.py`, `importer_container.py` (ZIP/IMG/FAT12).

4. **Decompose C901 functions** — `plan_atomic_bank`, `plan_slot_import`, `plan_multi_staging` into phase-based helpers.

### Medium Priority

5. **Add cache age/staleness indicator** — show "cache from 3 days ago" in browser; optional auto-refresh.

6. **Property-based tests** — use `hypothesis` for nibble round-trip, record mutation invariants, dependency closure properties.

7. **Document `save_backup=False` / `verify=False` risks more prominently** — in `bridge.py` docstrings and CLI help.

8. **Type `midi_in`/`midi_out` with `Protocol`** — replace `Any` in `WavestationBridge.__init__`.

### Low Priority

9. **Unify `bank_id` / `scope_id` terminology** — pick one name for session bank keys.

10. **Extract `TODO.md` items** — review and close/resolve stale items.

11. **Consider `multiprocessing` for bank reads** — parallel bulk dump requests (hardware allows concurrent?) — likely not, serial MIDI.

---

## Positive Notes

- **Exceptional test doubles** — the `Unit` class in `test_bridge.py` is a model for MIDI tool testing
- **Protocol archaeology documented** — `RESOLUTION_NOTES.md` references explain *why* offsets/functions are what they are
- **Write fence is a benchmark** — backup → send → verify with read-back comparison is the right pattern
- **Atomic copy semantics** — performance-first with private patches prevents the "edit one, change both" class of bugs
- **Hardware-quirk handling** — pacing, chunking, drain-before-request, late-reply rejection all correct
- **Corrupt-file tolerance** — caches warn and continue rather than crashing
- **Clean layering** — wire/record/dependency/planner/storage/UI separation maintained throughout

---

## Conclusion

kwsed is a **high-quality, production-ready codebase** with deep domain knowledge encoded in its design. The architecture correctly models the Wavestation SR's constraints, the write fence provides strong safety guarantees, and the test suite exercises critical paths including failure modes. 

The primary correctness risk is the unconfirmed `CARD_NAME_OFFSET`, which should be resolved against hardware. Beyond that, the codebase is maintainable, well-documented, and demonstrates expert-level understanding of both the hardware protocol and safe software practices for hardware control tools.

**Overall Rating: A- (would be A with `CARD_NAME_OFFSET` confirmed)**