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

## 3b. Multi mode: the data is decoded, the way to ask for it is not

**Status:** open. Decoding done; retrieval blocked.

"Multi" means two things on this instrument, and they are not the same
object -- the same double meaning the sibling p2ked had to untangle:

* a **performance** combines up to 8 **parts**, each a patch with its own
  MIDI channel, key range and level. That is the multi-timbral *sound*.
* a **Multiset** is the SR's MIDI multi mode: **32** of them, each assigning
  all **16 MIDI channels**.

The Multiset dump is function **`0x62`**, and its layout is worked out --
see docs/RESOLUTION_NOTES.md section 8. kwsed can decode a `.syx` containing
one today; it cannot ask the unit for one.

**Blocked on:** the request code. `0x32` was inferred from Korg's convention
that a request sits 0x30 below its dump, and **probed on hardware with no
argument, one and two: silence in every form** (the unit was unaffected and
still dumps banks normally). So either the code is different -- the
wave-sequence request `0x0C` for dump `0x54` already proves the arithmetic
is not universal -- or Multi data is only served while the unit is in Multi
mode, which would match how a bank request returns patches or performances
according to the unit's own state rather than the request's.

Next, in order of cost: retry the three `0x32` forms with the unit switched
to Multi mode; failing that, dump Multi from the front panel (Global ->
Dump), which needs no request code at all. Do **not** work through
neighbouring function codes looking for a hit -- some codes in that range
are commands rather than reads, and which ones is exactly what is not known.

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
