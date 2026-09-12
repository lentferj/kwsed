<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Resolution notes

Where every transcribed number came from, how the undocumented ones were
derived, and which of them are still guesses. [TODO.md](../TODO.md) says
*what* is open; this file says *how*.

---

## 1. Sources, and what each one is worth

| Source | Worth |
|---|---|
| System Exclusive dumps the instrument produced | **Evidence.** Sizes that divide exactly and names that decode are facts. |
| The live unit on 2026-09-12 | **Evidence**, for what was actually exercised. |
| Korg Wavestation SR Reference / Player's Guide | Scans, **no text layer**. Not searchable, so barely used. |
| Edisyn's Wavestation support | **Facts only** — see §2. Apache-2.0, so no code. |

The manuals being image-only is the single biggest constraint on this
project, and the reason §3 is a derivation rather than a transcription.

---

## 2. Edisyn: the licence question, and what was actually taken

Edisyn (Sean Luke and contributors) supports the Wavestation and is under
the **Apache License 2.0**. kwsed is **GPL-2.0-or-later**.

Apache-2.0 is compatible with GPL-**3**.0 and **not** with GPL-**2**.0.
Taking Edisyn code would therefore force this project to GPL-3.0-or-later —
a licensing change, not a detail, and one nobody asked for.

**What was taken:** three facts, each of which is independently evidenced by
dumps this instrument produced:

1. there are twelve banks, of which RAM 1-3 and the Card are writable;
2. the data encoding is nibble-based, **low nibble first**;
3. the performance and patch bulk dumps are the sizes §3 derives.

Facts about an interface are not copyrightable expression. Each is stated
here with its own independent evidence precisely so that a later reader can
check the claim instead of taking it on trust.

**What was not taken:** any code, any structure, any comment, any name.
`kws/messages.py`, `kws/banks.py` and `kws/dumps.py` say so in their
headers, and LICENSE says so at length.

If this project ever does want Edisyn code, the honest route is to relicense
to GPL-3.0-or-later deliberately, not to copy quietly.

---

## 3. The dump format, derived

### 3.1 The frame

```
F0 42 3n 28 <function> [bank] [number] <data…> F7
```

`42` is Korg, `3n` carries the global channel in its low nibble, `28` is the
Wavestation family. Confirmed on the wire: the Identity Reply from the live
unit carried manufacturer `42`, family `(0x28, 0x00)`.

Function codes in use:

| Code | Meaning |
|---|---|
| `0x10` | object request (one performance or patch) |
| `0x1C` | bulk request (a whole bank) |
| `0x49` | performance dump (one) |
| `0x4C` | all-patch dump (a bank) |
| `0x4D` | all-performance dump (a bank) |

### 3.2 Nibbles, low first — and *not* the X5's packing

The Wavestation sends each data byte as **two MIDI bytes, low nibble
first**. The sibling x5ded's X5D packs **eight bytes for every seven**
instead. Both are Korg, a few years apart, and the two schemes are not
interchangeable.

This is worth stating plainly because the wrong assumption is cheap to make
and expensive to find: a mis-decoded stream still yields printable
characters, so it *looks* like a character-set problem rather than a framing
one. x5ded lost an afternoon to exactly that shape of bug (a program really
called `Black Duck` decoding as `Black .u.k`), and the lesson carried here
was: **before trying transforms, print the raw body and count.**

`to_nibbles` / `from_nibbles` in `kws/messages.py` round-trip, tested.

### 3.3 Where the data starts

| Message | Offset |
|---|---|
| all-performance dump `0x4D` | 6 |
| all-patch dump `0x4C` | 6 |
| performance dump `0x49` | **7** |

The single dump is one byte later because it carries a *number* as well as a
bank. That lives in one place — `messages.DATA_OFFSET` — rather than being
open-coded at each call site, because a one-byte offset error is the exact
mistake that produces plausible nonsense.

### 3.4 The record sizes

From offset 6:

* a performance bank decodes to **9050** bytes, and `9050 / 50 = 181`;
* a patch bank decodes to **14910** bytes, and `14910 / 35 = 426`.

Both exact, nothing left over. Then every record begins with a legible
name — all 50 performances, all 35 patches. And a single-performance dump
from a file, taken from offset **7**, yields 181 bytes independently.

Three unrelated numbers landing exactly is not what a wrong reading does.

`parse_bulk()` **refuses** a dump that does not divide exactly, rather than
slicing it into records anyway. The size was derived, not documented, so a
unit or a model using another would otherwise produce names — and a name is
exactly the kind of output that looks like success.

### 3.5 Names

Performances store 15 characters and a NUL; patches store 16, space-padded.
`decode_name()` handles both, so no caller has to know which it is holding.

---

## 4. What the live unit established, and what it did not

Exercised on 2026-09-12 against the WS SR on
`U6MIDI Pro:U6MIDI Pro MIDI 1 44:0`, global channel 5, with a program card
fitted:

```
requested bank  0 -> reply says RAM 1 patch: 35 names
requested bank 11 -> reply says Card  patch: 35 names
```

Which settles three things:

1. **Discovery works**, and the Identity Reply carries the global channel —
   the one number that cannot be guessed, because a request on any other
   channel is met with silence rather than an error.
2. **The card slot is bank 11.** The user's card was read.
3. **The reply, not the request, says what came back.** Both requests were
   for a bank; both were answered with *patches*, which is the unit's choice
   and not the request's. `read_bank()` therefore takes the bank and the
   kind out of the reply. A late dump filed under the bank that was asked
   for is a silent, permanent error in a name catalog.

It did **not** establish what a Bank Select reaches (TODO 1), what decides
performances-or-patches (TODO 3), or anything inside a record (TODO 2).

---

## 5. The bug this project already had

`Slot.key` abbreviated the kind to its initial:

```python
return f"{self.bank.id}:{self.kind[0].upper()}:{self.number:03d}"
```

**"performance" and "patch" both begin with P.** So every patch key
collided with the performance of the same number in the same bank, and a
catalog quietly merged 50 performances with 35 patches — no exception, no
warning, just wrong names under half the numbers.

Caught by `test_performance_seven_is_not_patch_seven`, which exists because
the collision is invisible in any output you would look at by eye. The key
now carries the whole kind.

The general form, worth keeping: **an abbreviation is a hash, and a hash
needs a collision test.**

---

## 6. Guesses, labelled

- **`SEND_GAP = 0.010`** — inherited from the siblings, where it is also a
  guess. Labelled at the point of use. Never quote it as a measurement.
- **`DUMP_TIMEOUT = 20.0`** — reasoned, not measured: a patch bank is nearly
  30 kB, about eight seconds at 31250 baud, before the unit's own delay. The
  generosity is deliberate. During development a two-second wait made a
  request look failed, and the dump then arrived and was taken for the
  answer to the *next* request — which is why the input is drained before
  every request, too.
- **Bank Select numbering** — assumed to match the System Exclusive
  numbering. Untested. See TODO 1.

---

## 7. Rules carried in from the sibling projects

- **Workers do MIDI; the main thread does everything else.** In rxved a
  worker that merely asked the favourites store for its length raised out of
  the worker thread and ended the application, because SQLite refuses
  cross-thread use. A worker that only does MIDI cannot get this wrong.
- **`Widget._render` is Textual's.** Shadowing it in a custom widget makes
  every layout pass ask a `None` for its height. x5ded's key-hint widget did
  exactly that; here the method is `_render_hints`, with a test.
- **`DataTable` consumes Enter.** Bind selection through `RowSelected`, not
  a key binding, or the key silently does nothing.
- **Moving the cursor never sends MIDI**, enforced by a test, in every one
  of these projects.
- **A "not in the document" conclusion drawn from a layout-mangled PDF dump
  is not a finding.** In rxved that produced a confident, wrong claim that a
  parameter was absent from Roland's address map; it was there. With scans
  and no OCR, as here, the same trap is one step closer.
