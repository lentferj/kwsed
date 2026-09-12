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

## 2. Nothing inside a record past the name is decoded

**Status:** open by design for now.

A performance is 181 bytes and a patch 426; kwsed reads the name and stops.
The parts of a performance — eight of them, each with a patch, a MIDI
channel, a key range and a level — are the obvious next thing to show, and
are the reason the sibling rxved has a multi-mode screen.

**Blocked on:** the parameter layout. The manuals to hand are scans with no
text layer and no OCR tool is installed, so the layout has not been read.
Two ways forward:

1. OCR the Reference Guide's System Exclusive appendix.
2. Derive it: change one parameter on the unit, dump the performance again,
   and diff. Slow, but it produces evidence rather than a reading.

Do **not** guess offsets from plausible-looking byte values. A name that
decodes is proof; a channel number that happens to be in 0-15 is not.

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

## 6. kwsed does not write

**Status:** deliberate. Recorded so that adding a write is a decision, not
an omission somebody quietly fills.

Four of the twelve banks are writable (RAM 1-3 and the Card). kwsed stores
nothing, renames nothing and erases nothing.

If that changes, it follows the sibling projects' fence, which exists
because the alternative has cost people their sounds:

1. a modal **arm-then-fire** screen — opening it sends nothing, arming sends
   nothing, firing is a *different* key and works only while armed, and
   moving the cursor disarms;
2. never one keystroke from the browser;
3. **back up the destination first, unconditionally** — if that read fails,
   nothing is written;
4. say what is about to be overwritten **by name**, not by slot number;
5. verify by reading back and comparing, and report what did not match
   rather than claiming success.

---

## 7. The catalog cannot tell a stale name from a changed one

**Status:** open, minor.

A name read from the unit that disagrees with the catalog gets a `*`. In a
**ROM** bank that means the catalog is wrong. In **RAM or the Card** it
means somebody saved their own work, which is not an error at all. kwsed
marks both the same way.

**Blocked on:** nothing. `banks.Bank.writable` already carries the
distinction; the TUI just does not use it for this yet.
