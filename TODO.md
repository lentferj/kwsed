<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# TODO

*What* is open. *How* to resolve it, and where every transcribed number came
from, is in [docs/RESOLUTION_NOTES.md](docs/RESOLUTION_NOTES.md).

---

## 1. Which bank a Bank Select actually reaches is not established

**Status:** open. Blocks nothing; makes `select` a best effort.

`WavestationBridge.select()` sends Bank Select MSB `0`, LSB = the bank's
System Exclusive number, then a Program Change. That the System Exclusive
numbering (RAM 1 = 0 … Card = 11) is also the Bank Select numbering is an
**assumption**, untested.

It is also unknown whether a Program Change selects a *performance* or a
*patch* — that most likely depends on the unit's current mode, which kwsed
does not read.

**Blocked on:** a session at the desk. Send a Bank Select and a Program
Change, then read the unit's display and compare against the catalog. One
pass over the twelve banks settles it.

---

## 2. The record interiors: references done, parameters not

**Status:** the parts that matter for building banks are **done**; the rest
is open.

**Decoded and verified** (see docs/BANK_BUILDER.md and
docs/RESOLUTION_NOTES.md section 9): a performance's 8 parts and the patch
each points at; a patch's 4 oscillators and the wave or wave sequence each
names; the wave sequence bank's 32 headers, shared pool of 501 steps, and
names. `kws/records.py` reads and rewrites exactly those fields.

**Still open:** everything else. A performance's 21 effect bytes, a patch's
74 settings bytes and the bulk of each 84-byte oscillator, and most of a
wave sequence step. kwsed copies them verbatim and does not interpret them,
which is enough to build banks and not enough to edit sounds.

**Blocked on:** nothing but effort. The method that worked is in the
resolution notes: derive a candidate structurally from many records, then
check it field by field against Edisyn, which reads the same records. Do not
guess offsets from plausible-looking values -- a byte that happens to fall
in 0-127 proves nothing, and an early reading here was wrong in exactly that
way.

---

## 3. The unit decides whether a bank request returns performances or patches

**Status:** understood, handled, not fully explained.

One request code asks for a bank; the unit answers with an all-performance
dump or an all-patch dump. kwsed reads the kind from the reply, so this is
not a bug — but **what** decides it is unknown. Most likely the unit's
current mode.

**Blocked on:** the same desk session as item 1. Switch the unit between
Performance and Patch mode and repeat one bank request each way.

---

## 3b. Multi mode — the request code is 0x06, SOLVED 2026-09-12

**Status:** resolved from Korg's own documentation.

The Multi Mode Setup Dump Request is **`0x06`** and takes no argument. One
request draws three replies -- `0x55` Multi Mode Setup, `0x5E` Expanded and
`0x62` SR Expanded -- which appendix 4 says always travel together. Only the
last carries all 32 Multisets. `WavestationBridge.read_multi` sends it and
returns that one.

Verified on the unit: 7563 bytes, 3777 decoded, and Multiset 1 reads as
channels 1-16 pointing at RAM 1 performances 0-15 at full level.

The earlier guess of `0x32` was wrong, and wrong for an instructive reason:
it came from the convention that a request sits 0x30 below its dump, which
holds for four of the five pairs that were visible and does not hold here.
The documented requests are 0x06, 0x07, 0x08, 0x0C, 0x0E, 0x0F, 0x10, 0x19,
0x1C and 0x1D -- all in the 0x0x and 0x1x range, with no arithmetic relation
to their dumps at all.

---

## 3c. Single-object reads: slow, and still not explained

**Status:** open. It blocks single-patch *writes*, and nothing else.

A whole-bank read works every time. A single patch read (`0x10` request,
`0x40` reply) does not, and measurement has narrowed it without settling it.

**What is established:**

* The unit answers a single patch request after about **9.9 seconds** --
  consistently, to two decimal places, for 861 bytes that take a third of a
  second on the wire. Five requests in a row, all answered, all at 9.89s.
* `SINGLE_OBJECT_TIMEOUT` is therefore 15 seconds. The old value was 2, so
  every reply was cut off and then arrived during the *next* request, which
  is how a read of Card 12 came back holding the patch from Card 3 and why
  the whole thing looked erratic rather than merely slow.
* The replies are correct when they come: the bank and number bytes name the
  slot that was asked for, and the data decodes.

**What is not:** the same slot answers five times running in one session and
not at all in the next, with the same request, the same gap and a 14-second
window. It is not per-slot, it is not the gap between requests, and it is
not the timeout. Something about the unit's state decides it and that
something has not been found.

**Blocked on:** an explanation. Worth trying at the desk: whether the unit's
current mode or edit level decides it, since a single patch dump is
documented to land in the edit buffer and the Edit Patch level is where a
patch is selected. Do **not** work around it by accepting any patch dump
that arrives -- an answer to an abandoned request is indistinguishable from
the right one, which is the mistake that produced the wrong-slot reads.

**Not blocked:** whole-bank reads and writes, which are verified and work.

---

## 4. The single-performance dump has never been requested from the unit

**Status:** open.

`dumps.parse_performance()` decodes function `0x49` correctly from a file.
It has not been asked for over the wire, so the request form for it is
unexercised.

**Blocked on:** hardware time. Low value while item 2 stands — a performance
whose interior is undecoded is 181 bytes and a name, which the bank dump
already gives.

---

## 5. `SEND_GAP` is a guess

**Status:** open, low priority.

`kws/bridge.py` sets `SEND_GAP = 0.010`, inherited from the sibling
projects, where it is also a guess. It is labelled as such at the point of
use. `DUMP_TIMEOUT = 20.0` is *not* arbitrary — a patch bank is nearly 30 kB
on the wire, about eight seconds at 31250 baud before the unit's own delay —
but it has not been measured either.

**Blocked on:** nothing but the wish to measure rather than assume.

---

## 5b. The bank write — DONE 2026-09-12

**Status:** resolved against the hardware.

A built patch bank was written to the Card and read back identical: 14910
decoded bytes, no differences, all eight patches present. python-rtmidi
cannot send a message that size on ALSA, so `kws/alsa.py` does it in
256-byte sequencer events paced to the wire.

The first attempt is the part worth remembering. It reported success and
stored a stream with 672 bytes missing from the middle -- every patch after
the sixth shifted earlier by 336 bytes, the tail lost -- because the whole
29828 bytes went out in 0.00 seconds and the interface dropped what it could
not hold. Nothing errored. The read-back comparison caught it, which is the
only reason it was noticed at all.

**Still to do:** the performance and wave-sequence banks have not been
written, only patches. Same path, same fence.

---

## 6. Writing: one operation, fenced

**Status:** done for whole banks; deliberately limited.

kwsed writes in exactly one place, `WavestationBridge.write_bank`, and it
replaces a whole bank. Four banks accept it -- RAM 1-3 and the Card -- which
kwsed's own table and Edisyn's `getWriteableBanks()` independently agree on;
the eight ROM banks are refused before anything is read or sent.

The fence, which is the rule for any destructive feature added later:

1. A modal **arm-then-fire** screen (`SendScreen`) or, on the command line,
   `--yes`. Opening sends nothing; arming sends nothing; firing is a
   *different* key and works only while armed.
2. **Never one keystroke from the browser.**
3. **Back up first, unconditionally.** The destination is read in full and
   written to a file before a byte goes out; if that read fails, nothing is
   written. Backups are plain `.syx` plus a JSON sidecar, so they can be
   restored by any tool including the instrument's own front panel.
4. **Say what is about to be destroyed, by name.** "Card" means nothing; the
   names it is about to replace mean everything.
5. **Verify by reading back and comparing**, and report a mismatch rather
   than claiming success -- a bulk dump is unacknowledged.

**Still not done:** writing a single performance or patch rather than a
whole bank, and restoring a backup from within kwsed (the files are plain
`.syx`, so the instrument can already take them back). Neither is a dict to
widen: both are new destinations and get the same fence.

---

## 7. The catalog cannot tell a stale name from a changed one

**Status:** open, minor.

A name read from the unit that disagrees with the catalog gets a `*`. In a
**ROM** bank that means the catalog is wrong. In **RAM or the Card** it
means somebody saved their own work, which is not an error at all. kwsed
marks both the same way.

**Blocked on:** nothing. `banks.Bank.writable` already carries the
distinction; the TUI just does not use it for this yet.

---

## 8. A built bank dropped the sequences its sequences quote — FIXED

**Status:** **fixed 2026-09-20.** Was the most severe thing on this list:
the bank builder produced a file that played wrong and reported success.

A wave sequence's step carries a `wave` field. Below `rec.ROM_WAVE_BASE`
(32) it does not name a wave -- it names *another wave sequence in the
same bank*, which the step plays in place of a sample.
`deps.walk_sequence` already collected those into `quotes`, documented as
"must travel with it", and **nothing read the field**. So the builder
neither pulled a quoted sequence into the plan nor renumbered the
reference when it copied the step, though it renumbered the same kind of
reference in a patch.

**How common, measured.** Across the 828 `.syx` files under
`~/Dokumente/SYNTHS/WavestationSR`: 657 wave-sequence banks, 15679
non-empty sequences, **1752 of them (11.2%) quoting another**, in 453 of
the 828 files. Ordinary practice, not an edge case -- quoting is how a
long sequence is assembled from short ones.

**What it cost.** Building from six patches of one such file gave a bank
holding 36 step references to slots the build had zeroed (silence) and 21
to slots holding an unrelated sequence (the wrong sound). Every one is a
silent failure: the file is well-formed, the checksum is right, and the
read-back after a write compares equal, because the bytes that went out
are the bytes that came back. It is the content that was wrong, which is
the one failure the write fence cannot catch.

### What the fix is

* `walk_sequence` takes the `bank_id` it is walking, so a quote is a real
  `SequenceId` instead of `(None, n)` -- the other half of what was item
  12.
* `deps.quoted_closure` expands a set of wanted sequences to include
  everything they quote, transitively, with a `seen` set because quoting
  can be circular. `closure` and `plan` both go through it, so a quoted
  sequence costs a slot and its steps count against the budget.
* `build` renumbers each copied step's `wave` through `sequence_map` when
  it is below `ROM_WAVE_BASE`, exactly as the patch loop renumbers an
  oscillator's reference, and raises rather than writing a stale number if
  the plan somehow did not carry it.

### And one thing the fix exposed

Relocation was keyed `(source bank, step)`. Two *wave banks* reached
through one source share that key, resolve to the single wave sequence
bank a `Source` holds, and so walked identical steps -- which meant one
physical step in the destination shared by two sequences that need
*different* quote targets. Whichever was written second took the other's.
The key is now `(source bank, wave bank, step)`, and `plan`'s step budget
counts in the same units so it cannot disagree with what the build shares.

Invisible before this item: with the quote never renumbered at all, both
sequences were equally wrong, so sharing the step cost nothing.

### Verified

Across the whole library: 347 source banks with quoting built, **7634
quoting steps in the built output, all 7634 landing on the sequence they
quoted**, none on a zeroed slot. `tests/test_bankbuild.py` carries
`TestASequenceThatQuotesAnother`; each of the four fixes was reverted in
turn to confirm a test fails without it.

---

## 8b. `walk_sequence` raised on a link past the end of the pool — FIXED

**Status:** fixed 2026-09-20, found while verifying item 8.

Three files of the 828 -- `Soft1.syx` and `PacificWinds.SYX` -- end a
chain with a forward link of `0x0700` rather than 0, in wave sequence
banks that are otherwise perfectly well formed: 8784 bytes, all 32 headers
in range. `walk_sequence` stopped on a link of 0, on a step already seen
and at the size of the pool, but not on a link past the end, so it raised
out of `records.step` and took the bank builder with it.

It now stops, as it does for the other three cases. Whatever wrote those
files, the instrument evidently stops there too.

---

---

*Items 9 onward came from a code review by MiMo V2.5 Free on 2026-09-19.
Each has been checked against the code; the verdict line says what that
check found. Three did not survive it.*

---

## 9. `dumps.read_syx_file` does not close the file it opens

**Status:** open, real, one line. **Verified.**

`raw = open(path, "rb").read()` at `kws/dumps.py:187`. Called per `.syx`
by `extract_catalog`, `build_bank`, `deps.load_syx` and `cli._cmd_send`.

The review says a glob of 50 files leaks 50 handles; on CPython it does
not -- the handle is refcounted shut as the expression ends, which is why
this has never been felt. It is still wrong: it relies on a detail of one
implementation, and it raises `ResourceWarning` under `-W error`, which is
how the test suite should be run.

**Blocked on:** nothing. `with open(path, "rb") as handle: raw = handle.read()`.

---

## 10. `identity_reply_fields` does not range-check the manufacturer byte

**Status:** **rejected as stated.** The case cannot occur; a different one
can.

The review asks for a check that `data[5] < 0x80`. A byte of 0x80 or above
cannot appear inside a SysEx message: 0xF7 ends it and any other status
byte aborts it, so rtmidi never hands one over, and the function has
already required `data[0] == F0` and `data[-1] == F7`. The check would
never fire.

The real gap in that function is a different one the review did not
reach: an MMA *extended* manufacturer ID is three bytes beginning `00`, so
for such a device `data[6]` and `data[7]` are not family bytes and the
`family` field is meaningless. It costs nothing here -- every caller
branches on `is_korg`, which a leading `00` fails -- so this is a note
rather than a fix.

**Blocked on:** nothing; nothing to do. Kept so the next reader does not
re-raise it.

---

## 11. `config._update` escapes only `\` and `"` for TOML

**Status:** open, real, unreachable in practice. **Verified.**

`kws/config.py:82`. TOML basic strings also interpret `\n`, `\t`, `\r`,
`\b`, `\f` and `\uXXXX`, so a value containing one would not survive a
round-trip. The only strings written are MIDI port names and bank ids;
ALSA will not produce a port name containing a control character.

Worth fixing because it is three lines and the failure would be baffling,
not because it is going to happen.

**Blocked on:** nothing.

---

## 12. `walk_sequence` did not know which bank it is walking — FIXED

**Status:** **fixed 2026-09-20**, with item 8, whose root it was.

`deps.walk_sequence` returns `SequenceChain(bank_id="", ...)` at
`kws/deps.py:108` and, four lines above, appends `(None, info["wave"])` to
`quotes`, whose declared type is `SequenceId = Tuple[str, int]`. Both are
the same omission: the function is handed a bare `Sequence[int]` and has
no way to say which bank it came from, so it fills in a placeholder twice.

The review calls this cosmetic because the callers set the bank from their
own context. They do for `bank_id`. Nobody sets it for `quotes`, because
nobody reads `quotes` at all -- which is item 8.

`walk_sequence` now takes `bank_id`, defaulting to the empty string for
callers that want only the step list, and fills in both the chain's bank
and its quotes from it.

---

## 13. `Function.SINGLE_PERFORMANCE_REQUEST` is defined but never sent

**Status:** open, cosmetic, expected. **Verified** -- `0x19` appears only
in the enum and in `REQUEST_FOR_DUMP`. `SINGLE_PATCH_REQUEST` is sent from
`messages.py:483`; its performance counterpart has no caller because
single-object reads are still under investigation.

**Blocked on:** TODO 3c. A comment at the enum entry saying so would stop
the next reader filing this again.

---

## 14. `patch_dependencies` annotated a 3-tuple list as `List[SequenceId]` — FIXED

**Status:** **fixed 2026-09-20**, but **not the way the review proposed.**

The review has the symptom right and the remedy backwards. It suggests
`SequenceId` was never updated to match the 3-tuple. It should not be:
`SequenceId = Tuple[str, int]` is correct for `SequenceChain.quotes` and
for `walk_sequence`'s local, which are genuinely `(bank, sequence)`.
Widening it to three elements would mis-type both.

The actual defect is narrower -- one local annotation, `out: List[SequenceId]`
at `kws/deps.py:122`, inside a function whose signature correctly says
`List[Tuple[str, int, int]]` and which appends
`(bank_id, wave.bank, wave.wave)`. The annotation is simply wrong about
its own variable.

Fixed by correcting that one local annotation. `SequenceId` is unchanged,
and the three-element key now has a name of its own, `deps.SequenceKey`,
because item 8 made the distinction load-bearing rather than decorative.

---

## 15. `deps.WAVESEQ_DUMP` duplicates `Function.ALL_WAVESEQ_DUMP`

**Status:** **rejected.** The premise is false.

The review states that `deps.py` "already imports `messages as m`". It
does not -- `kws/deps.py` imports `records as rec` and nothing else from
the package. The local `WAVESEQ_DUMP = 0x54` exists precisely so that the
dependency walker does not depend on the message layer, which is a
sensible direction for the dependency to run.

There is a smaller oddity the review did not catch: the constant is
defined at line 216, *after* its use at line 207. That is legal, since the
use is inside a function body, but it reads as a mistake. Move it up.

**Blocked on:** nothing; move the constant, keep it.

---

## 16. `data_dir()` is implemented twice

**Status:** open, cosmetic. **Verified** -- `kws/favorites.py:79` and
`kws/backup.py:71`, same platform rules, independently written.

They do agree today. The reason to merge them is that the next change to
the rules will be made in one of the two.

**Blocked on:** nothing.

---

## 17. The `Unit` test double answers patch requests only

**Status:** open, real coverage gap. **Verified.**

`tests/test_write.py:70` intercepts `BULK_REQUEST` and always replies with
`ALL_PATCH_DUMP`. A performance or wave-sequence read against it goes
unanswered. So `write_bank` is exercised for one kind of three, and the
kinds differ in dump function, record size and count -- and the bug fixed
in `5cdaa21` was precisely a kind that defaulted to patch when it should
not have.

**Blocked on:** nothing but writing them.

---

## 18. `from_nibbles` drops a trailing odd byte without saying so

**Status:** open, by design, agreed. **Verified** -- `range(0, len - 1, 2)`
at `kws/messages.py:248`. The instrument always sends an even count, so
this only fires on a truncated file, where it silently shortens the last
record rather than reporting the truncation.

The strict per-nibble range check immediately below raises on bad data; an
odd *length* is bad data of the same kind and is the one case that passes
quietly. Raising there would be more consistent than the docstring note
the review suggests.

**Blocked on:** a decision on which way -- raise, or warn.

---

## 19. Test coverage gaps noted by the review

**Status:** open, minor.

* `walk_sequence` with a self-loop (step 0's forward link at step 0).
* `from_nibbles` with a nibble of exactly `0x0F`, the upper boundary of
  the strict check.
* `write_bank` for performance and wave-sequence kinds -- see item 17.

The fourth the review lists, `identity_reply_fields` with a manufacturer
byte at or above `0x80`, is not worth a test: item 10 explains why the
input cannot occur. A test for a state the transport cannot produce
asserts only that the test double can produce it.

**Blocked on:** nothing but writing them.

## 20. The MidiQuest `.sqs` format is unread — shared across the projects

**Status:** open, and not specific to this instrument.

The owner's instrument folders hold `.sqs` files alongside the `.syx`
dumps. They are **MidiQuest's** own bank format, not the instrument's: an
IFF-style chunked file with `LIST`, `FORM`, `BANK`, `CHNL`, `SHDR`,
`FILE`, `TRAN`, `RECV`, `XTRA`, `MPIN`, `MPOT`, `BODY`, `AUDI`,
`BDEF` and `TDEF` chunks, sizes big-endian, and contents that are not
simply the raw SysEx.

The same format turns up for **every** instrument in the collection, so
decoding it once would serve rxved, kwsed, p2ked, x5ded and nanosyned
alike. Worth doing as shared work rather than five times over.

**Blocked on:** nothing but the decoding. Nothing here depends on it.
