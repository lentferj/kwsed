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

## 3c. Single-object reads are unreliable, and nobody knows why

**Status:** open. It blocks single-patch *writes*, and nothing else.

A whole-bank read works every time. A **single patch** read (`0x10` request,
`0x40` reply) mostly does not:

* the request alone draws nothing, even after twenty seconds;
* sending any other message afterwards can shake the reply loose, and the
  reply that arrives is the answer to an *earlier* request;
* nudging the unit with an identity request worked once and then stopped.

The replies themselves are correct when they come -- the bank and number
bytes identify the slot that was asked for, and the patch data decodes. It
is the delivery that is erratic.

This is why `write_patch` exists but should not be relied on. It is fenced
the same way a bank write is, and its first step is to read the slot it is
about to replace, so an unreliable read means it refuses rather than writes
blind. That is the correct behaviour and it is also, at the moment, the
usual behaviour.

**Blocked on:** understanding the delivery. Worth trying: whether the unit
answers single-object requests only in a particular mode, and whether the
sibling x5ded's priming trick -- a request that draws silence, sent first --
has an equivalent here. **Do not** work around it by matching any patch
dump that arrives: an answer to an abandoned request is indistinguishable
from the right one, and that mistake already produced a read of Card 12 that
returned the patch from Card 3.

**Not blocked:** whole-bank writes, which are verified and work.

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
