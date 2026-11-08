<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# TODO

*What* is open. *How* to resolve it, and where every transcribed number came
from, is in [docs/RESOLUTION_NOTES.md](docs/RESOLUTION_NOTES.md).

---

## 1. Which bank a Bank Select actually reaches — RESOLVED

**Status:** resolved from Korg's documentation (Player's Guide §4.8 and
Reference Appendix 3 §1.3). `select` is no longer a best effort.

The SR's twelve banks are six MIDI banks addressed by CC32 (LSB) 0-5; CC0
(MSB) is always 0; each MIDI bank holds two internal banks of 50, so
Program Change 0-49 selects the first and 50-99 the second:

- RAM1  -> CC32=0, PC base 0
- RAM2  -> CC32=0, PC base 50
- RAM3  -> CC32=2, PC base 0
- ROM4  -> CC32=2, PC base 50
- ROM5  -> CC32=3, PC base 0
- ROM6  -> CC32=3, PC base 50
- ROM7  -> CC32=4, PC base 0
- ROM8  -> CC32=4, PC base 50
- ROM9  -> CC32=5, PC base 0
- ROM10 -> CC32=5, PC base 50
- ROM11 -> CC32=1, PC base 0
- CARD  -> CC32=1, PC base 50

The SR has no patch mode: a Program Change always selects a Performance (or
a Multiset in Multi mode), never a patch, so `select()` refuses a patch
slot. The SR's "Bank Select" MIDI-receive parameter must be ON for this to
work; kwsed cannot read it.

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
   written. Backups use the family envelope, and each block is the exact
   System Exclusive message a `.syx` file holds, so they can be restored by
   any tool including the instrument's own front panel.
4. **Say what is about to be destroyed, by name.** "Card" means nothing; the
   names it is about to replace mean everything.
5. **Verify by reading back and comparing**, and report a mismatch rather
   than claiming success -- a bulk dump is unacknowledged.

`kwscli backup save` reads a bank into an envelope and `kwscli backup
restore` writes it back through the same fence; both are behind `--yes`.

**Still not done:** writing a single performance or patch rather than a
whole bank. It is not a dict to widen: it is a new destination and gets the
same fence.

---

## 7. The catalog cannot tell a stale name from a changed one — FIXED

**Status:** **fixed 2026-11-06.** `kws/catalog.py` keeps one wording for
each meaning (`disagreement_words`), picked by `Bank.writable`, with the
same strings x5ded uses: "saved over the factory preset" against RAM 1-3
and the Card, "the printed list is wrong; the unit is right" against the
eight ROM banks.

**What actually changes on screen.** The browser's detail line now says
which it is whenever the slot it shows differs, instead of reaching
"read only" and stopping there; the status after `r` or a scan names the
meaning beside the count ("3 differ: saved over the factory preset");
`kwscli dump` and `kwscli list` print a `*` and, when any is shown, a
line under the table saying what it means for that bank; the Device
report and the help carry the same two sentences. Live names (T4) feed
the unit side and the scan (T8) reads bank by bank through the same
`_adopt`, so both land on the correct bank kind automatically.

A name read from the unit that disagrees with the catalog gets a `*`. In a
**ROM** bank that means the catalog is wrong. In **RAM or the Card** it
means somebody saved their own work, which is not an error at all. kwsed
marks both the same way.

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

**Status:** closed 2026-11-06 — uses `with open(...)` now. **Verified.**

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

**Status:** closed 2026-11-06 — config module is now a vinsynlib shim; escaping lives in the library.

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

**Status:** open, cosmetic — closes with the backup-envelope binding (T5): `data_dir()` will be unified when `kws/backup.py` becomes a shim over `vinsynlib.backup`. **Verified** -- `kws/favorites.py:79` and
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

## 20. `tests/test_bankbuild.py` sets `first = step` and never uses it — FIXED

**Status:** resolved 2026-10-03. The fixture now returns the start step and
asserts on it; see `TestTheHeaderSaysWhereAChainStarts` in
tests/test_bankbuild.py. The F841 suppression is gone.

```python
first = step  # in make_waveseq
for k, wave in enumerate(waves):
    at = r.STEP_BASE + step * r.STEP_SIZE
```

`step` is the running counter the loop below advances, and `first`
snapshots it once before the first iteration — so it holds the step a
chain *starts* at, which is exactly the thing a wave-sequence fixture
ought to be checking. Nothing reads it. Ruff reports it (`F841`), and the
`per-file-ignores` entry for the file is there to say so.

It was kept rather than deleted, because a dead local that looks like the
remains of an assertion is evidence, and deleting the line would remove the
evidence without answering the question. Two ways out, and the second is
the better one:

* delete it, if the fixture genuinely has nothing to say about where a
  chain starts; or
* **assert on it** — that a sequence's header links to the first step its
  chain occupies. That is a real property of the format, and it is already
  what `plan()` and `build()` assume. `test_relocated_sequences_keep_their_steps`
  walks the chain but never checks the header that starts it.

This is the only suppression in the tree that records a question about a
test rather than a choice about the linter, which is why it is written
down here instead of being resolved by deleting a line.

**Blocked on:** nothing but deciding, and then writing the assertion.

## 21. `messages.object_request` is never called — FIXED

**Status:** resolved 2026-10-03. Deleted, along with the `OBJECT_REQUEST`
alias it existed to justify. `single_patch_request` says what it is.

`object_request(bank, number, channel)` has no call site outside
`kws/messages.py`, and it does exactly what `single_patch_request` does --
`OBJECT_REQUEST` is `0x10` and `SINGLE_PATCH_REQUEST` is `0x10`, the same
code under two names. So it is a second spelling of a request the project
already has a correctly named builder for.

vulture does not flag it, because vulture reports unused *module-level
definitions* only at a confidence this file sits below; a public function
in a library module reads as an API rather than as litter.

Two ways out:

* delete `object_request`, and with it the `OBJECT_REQUEST` alias, since
  `single_patch_request` says what it is and nothing calls the other; or
* keep it, if a generic per-object request is expected to be used once a
  single-performance request is implemented -- see item 4, which is still
  open and would need `SINGLE_PERFORMANCE_REQUEST` (0x19) rather than
  this.

Deleting it is the tidier of the two, and the alias it drags out with it
is the thing most likely to be read as meaning something else later.

**Blocked on:** nothing but the decision.

## 22. The MidiQuest `.sqs` format is unread — shared across the projects

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

## 23. The HCard card-name: reading is closed, writing is possible — parked

**Status:** read path closed by the documentation; write path designed, not
built.

The card name is **not readable** over MIDI, and that is now established
rather than suspected:

- The System Setup dump (`0x51`) is exactly the 34-field `system` structure
  of Reference Appendix 4 §5.8 and has no name field.
- The accompanying expanded dumps (`0x5C`, `0x61`) are 20 and 9 bytes
  (§5.9, §5.10) and have none either.
- The dump-request list (§1.1) has **no parameter-read request**.
- `CARD_NAME` exists only as **parameter #1** in the parameter table
  (§5.15), and Parameter Change (`0x41`) is a *set* message. Appendix 3
  lists no parameter-change transmission, so the SR never reports it.

`read_card_name()` therefore cannot work; the archive falls back to a local
label (`cards.json` `label`) plus content evidence (item 25).
`CARD_NAME_OFFSET` is dead as a read and should be removed when the method
is next touched; `tools/probe_card_name.py` remains useful only for
confirming the dump's shape.

**Writing** it is documented: Appendix 4 §2.1.17 — function `0x41`,
parameter #1, the value as 7-bit ASCII with `0x7F` = space and a NUL
terminator, up to 15 characters. `frame(0x41, [1, 0, *ascii, 0])` is the
whole message. Before shipping, two things need a real unit: whether a
Parameter Change commits the name to the card (the NAME page auto-saves on
exit; a parameter change never "enters" the page), and that there is **no
read-back and no backup** — the old name cannot be read, so the operation is
not automatically reversible. The HCard's Dynamic Update may also rewrite
the name on a bank switch.

**Blocked on:** a separate write-verification session. Writing the card name
is a card write, outside the read-only HCard session.

## 24. HCard write protection is invisible to the synth

**Status:** documented, deliberately not implemented here.

An HCard bank can be write-protected on the card itself, and the
Wavestation cannot see that flag. A write to a protected bank fails its
read-back comparison, so `write_bank` reports a mismatch rather than
success. The browser should eventually say "the card may have this bank
write-protected" when that happens, instead of only the generic mismatch.

This is about writing *to* an HCard bank, which is the same `CARD` fence
kwsed already has. No part of the read-only HCard support changes it.

**Blocked on:** a separate write-verification session, deliberately outside
this one.

## 25. Which HCard bank is active cannot be read — identify by content

**Status:** implemented — fec2c49 (content digest and digest resolution in
`kws/cards.py`), 9f97d16 + 550eafd (verification, active-bank marker and
switch prompt in the app), and the `kwscli cards` output this item
describes. The original design text is kept below.

Neither the SR nor kwsed can see which of the forty banks is in front of the
synth: the card name is unreadable (item 23), the SR's `CURRENT_BANK` /
`CURRENT_PROG` parameters are write-only in the documented SysEx, and the
HCard itself has no MIDI. The active bank is a property of the card's front
panel. The workable mechanism is therefore **evidence plus assertion**:

1. **Content digest.** At capture, hash the decoded performance and patch
   dumps (e.g. SHA-256 over both payloads) and store the digest with the
   bank in `cards.json`. A fresh read can then be matched against every
   captured bank without trusting the (unreadable) name.
2. **Verification against the asserted bank.** `C` on a card sub-bank row
   passes the row number as the expected bank, as the sweep already does.
   Compare the digest with that bank's stored digest:
   - match -> confirmed;
   - matches a *different* captured bank -> the card is on that bank (or
     the two are identical); say so and file accordingly;
   - no match -> new or changed content; file under the asserted number and
     mark it **unverified**.
3. **Active-bank marker.** Remember the last confirmed/asserted bank and
   mark it in the pane. A later read whose digest no longer matches the
   stored digest means the bank changed since capture (or a different bank
   is active); say which, rather than silently trusting the row.
4. **"Switch on the HCard" prompt.** When the user acts on a card row that
   is not the known-active bank, say so and ask them to select that bank on
   the HCard first — it cannot be switched remotely. The sweep already
   works this way; this generalises it to single actions.

**Only open point:** the recorded limit — two banks captured with identical
content are indistinguishable by digest and `resolve_by_digest` returns
`None` for them rather than guessing (docs/HCARD.md §6, "Limits"). A bank
never captured cannot be identified and a bank edited on the unit no longer
matches its stored digest are inherent to the evidence scheme and are stated
in the UI, not open work.

**Blocked on:** nothing. It is read-only and covered by the existing
doubles in `tests/`.

## 26. Multiset selection and the browser view — hardware check pending

**Status:** implemented; one message not yet confirmed on a unit.

The 32 Multisets are now browser rows (keys `m` / `e`): `m` reads the SR
Expanded Multi Mode Setup (`0x06` → `0x55`, `0x5E`, `0x62`) and jumps to
them, `⏎` makes one current, and `e` edits it as a 16-channel table whose
write goes through the existing fenced `write_multi` (backup + read-back).

The **select** message is documented (Reference Appendix 4 §2.1.20/§2.1.21:
`0x5B` for 0-15, `0x63` for 16-31) but has not been exercised against the
unit. Confirm on hardware: select a Multiset from the browser with the unit
in MULTI mode and see it become current; if the unit ignores it, check the
message number and the mode first. No memory is written by the select.

Also confirm the editor's write round trip on hardware when the separate
write session happens: the `0x62` dump is sent by `write_multi`, which
already backs up and verifies.
