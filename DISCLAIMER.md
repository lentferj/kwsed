<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Disclaimer

## Some of this is verified against hardware. Some is not.

Verified on **2026-09-12** against a real Wavestation SR (global channel 5,
reached over a DIN cable behind a U6MIDI Pro interface, with a program card
fitted):

- **Discovery.** The unit answers a Universal Identity Request, and the
  reply carries Korg's manufacturer ID, the Wavestation family ID `28h`, and
  **the global channel the unit is set to** — the one number that cannot be
  guessed, since every request on another channel meets silence.
- **The frame layout** `F0 42 3n 28 <function> … F7`.
- **The nibble encoding**, low nibble first, and the per-message data
  offsets.
- **Bank dumps.** A bank request returned complete banks, decoded into 35
  patch names each, from RAM 1 and from the **Card** — confirming the card
  slot is addressed as bank 11.
- **The record sizes**, 181 per performance and 426 per patch, which the
  dumps divide into exactly.

**Not verified:**

- **What a Bank Select actually reaches.** `select()` sends Bank Select and
  a Program Change, but which of the twelve banks a given Bank Select
  selects has not been established. Treat selection as a best effort; the
  interface says so rather than implying otherwise.
- **Which kind a bank request returns.** One request code asks for a bank
  and the unit answers with performances *or* patches — its choice, not the
  request's. kwsed reads the kind from the reply. Whether the unit's current
  mode decides it is untested.
- **Everything inside a record past the name.** The manuals to hand are
  scans with no text layer, so the parameter layout has not been read.
- **The single-performance dump** (`0x49`) decodes from a file but has not
  been requested from the unit.
- **`SEND_GAP`**, a guess inherited from the sibling projects.

### Why the test suite is not the evidence

kwsed has a passing suite and a demo device, and for anything in the
unverified list their agreement proves only internal consistency — the demo
was written from the same reading as the code it answers. The sibling rxved
shipped a device-ID conversion that was wrong for every value it was ever
given, with a green suite, until somebody plugged in a synthesizer.

What the tests do establish: the nibble encoding round-trips, each message's
data offset is the right one, a bank's performances and patches cannot
collide in the catalog, and the cursor never sends MIDI.

## What kwsed does to the instrument

**It does not write to the unit's memory.** Nothing stores, renames or
erases anything — not even to the four writable banks.

It plays the instrument in one place, and that place says so: `Enter` in the
TUI and `kwscli select`.

**Reading a bank makes no sound.** One System Exclusive message; nothing is
selected.

## Edisyn

Edisyn has Wavestation support and is Apache-2.0, which is **not** compatible
with this project's GPL-2.0-or-later. It was therefore read only as a
reference for interface facts — the bank list, the nibble encoding, the dump
sizes — each of which is independently evidenced by dumps the instrument
produced. No Edisyn code appears here. See LICENSE and
docs/RESOLUTION_NOTES.md §2.

## AI assistance and human authorship

kwsed was created by its **human author, Jan Lentfer
(<jan.lentfer@web.de>)**, working with Anthropic's **Claude**, following the
pattern of the author's sibling **rxved**, **p2ked**, **x5ded**, **eosed**,
**s3ked**, **k2kremote** and **mpc2emu** projects.

**The ideas, the direction and the sources are human** — including pointing
out Edisyn and its licensing, which shaped how it could be used.

**Claude assisted with the execution:** deriving the record layout from the
dumps, transcribing the frame format and bank table, writing the transport,
the demo device, the CLI, the Textual browser, the tests and this
documentation.

## No warranty

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE.
