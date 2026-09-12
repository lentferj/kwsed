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
2. ~~**The card slot is bank 11.**~~ **Wrong -- see section 10.** Byte 11
   is ROM 10. The Card is System Exclusive byte **3**.
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

---

## 8. Multi mode: the Multiset dump, and a request code that is not derivable

### 8.1 Two different objects, both called "multi"

* A **performance** combines up to 8 **parts**, each a patch with its own
  MIDI channel, key range and level.
* A **Multiset** is the SR's MIDI multi mode: 32 of them, each assigning all
  16 MIDI channels.

Worth stating because the sibling p2ked hit the same ambiguity on the
Proteus and it cost a wrong reading of the manual there.

### 8.2 The Multiset dump, function `0x62`

Derived from the owner's `sr_multi.syx`, 7563 bytes on the wire, data at
offset **7** (two header bytes, unlike the banks' one):

```
3777 decoded bytes
   0 .. 2751   32 multisets x 86 bytes
2752 .. 3263   32 names x 16 bytes
3264 .. 3776   513 bytes, not identified
```

Each 86-byte multiset is **22 bytes of settings followed by 16 parts of 4
bytes** -- and 22 + 16*4 = 86 exactly, ending on the record boundary, which
is not what a wrong split does. The part fields, by their ranges over all
32 x 16 = 512 parts in the factory file:

| byte | range | reading |
|---|---|---|
| 0 | 1-2 | bank |
| 1 | 0-49 | performance number, exactly the 50-per-bank range |
| 2 | 3-127 | level |
| 3 | 0-1 | flag; 1 on channels 1-15, 0 on channel 16, in every record |

The alignment was found by scanning every offset from 16 to 25 and keeping
the one whose four columns give a plausible bank, a number inside 0-49 and a
level inside 0-127. Only offset 22 both does that and ends flush with the
record.

Two things this does **not** establish: bytes 0 and 3 are read from a file in
which all 32 multisets are factory defaults, all named `Multiset`, so the
name decoding is proven but unexercised on real names; and the trailing 513
bytes are unaccounted for.

### 8.3 `0x32` was inferred, probed, and is wrong

Edisyn has no Multiset support, but its five request/dump pairs give the
convention:

| object | request | dump | difference |
|---|---|---|---|
| single patch | `0x10` | `0x40` | 0x30 |
| single performance | `0x19` | `0x49` | 0x30 |
| patch bank | `0x1C` | `0x4C` | 0x30 |
| performance bank | `0x1D` | `0x4D` | 0x30 |
| wave sequences | `0x0C` | `0x54` | **0x48** |

Four of five sit 0x30 below their dump, which is Korg's family-wide
convention -- the 01/W documents the same relationship, down to mode request
`0x12` and mode data `0x42`. That predicts `0x32` for the Multiset dump
`0x62`, and the exception looked explicable: wave sequences would need
`0x24`, which is already the data-load-error reply, so the rule appears to
break where it would collide, and `0x32` collides with nothing known.

**Probed on hardware, 2026-09-12, and it was wrong.** All three forms --
`F0 42 34 28 32 F7`, with one argument, and with two -- returned silence.
The unit was unaffected: it answered an Identity Request and dumped RAM 1
normally straight afterwards.

Silence distinguishes nothing here, so the reading is either that the code
is different, or that Multi data is only served while the unit is in Multi
mode. The second would match behaviour this unit already shows, where a bank
request returns performances or patches according to its own state.

**Do not hunt for the code by trying neighbours.** Some values in that range
are commands rather than reads, and which ones is precisely what is not
known; kwsed's rule that it writes nothing to the unit is not compatible
with finding out empirically. The front panel dumps Multi data with no
request code at all, and that is the route to prefer.

### 8.4 The status codes are transcribed, not confirmed

`Function.NAMES` lists `0x21`-`0x26` as write/load/format status replies.
Those come from Korg's family convention, **not** from evidence here: no
dump in hand contains one, this unit has never returned one, and Edisyn
implements none of them. The docstring in `kws/messages.py` groups them
under a sentence claiming confirmation against dumps and against Edisyn,
which is true of the dump codes and overstated for these.

---

## 9. The reference graph, and the checksum that only building needs

Worked out to make `docs/BANK_BUILDER.md` possible. The layouts and the two
traps are there; this records how they were established and what went wrong.

### 9.1 Two independent derivations

**Structural.** Scanning every plausible (start, stride) across 35,550
performance records, one alignment gives 8 repeating groups ending flush
with the 181-byte record: 18 bytes from offset 37. In it, one column takes
**exactly 0-34 plus 255** -- the 35 patches a bank holds plus a sentinel,
with nothing outside, over roughly 280,000 samples.

**Against Edisyn.** `Part.read` lists those 18 bytes in order,
`PerformanceStructure.read` places them after 16 name and 21 effect bytes,
`PatchStructure.read` gives 16 + 74 + 4 x 84 = 426, and `Block.read` gives
32 headers, 501 steps, 32 names = 8784. Facts only; no code taken.

The chain walk was then checked against 657 wave sequence banks: 1-49 steps
per sequence, unused sequences walking to zero, and no bank exceeding the
501-step pool. One real bank uses 495 of 501, so the pool is a real
constraint and not a theoretical one.

### 9.2 Three wrong readings, and what they looked like

Worth recording because each produced a confident number rather than an
error.

1. **Byte order.** 16-bit fields are big-endian while the nibble encoding is
   low-nibble-first. Reading words the other way gave wave numbers that were
   all multiples of 256 -- and a claim that 98% of patches use a wave
   sequence.
2. **The wrong pivot.** Edisyn splits a wave number at 396 when *writing*,
   which is about its `waveNumExp` field and not about sequences. Testing
   `wave >= 396` gave 17%. The actual rule is `wave < 32`, the same rule a
   step uses, which is why a sequence can quote another sequence.
3. **The verified figure is 46%.**

The general lesson, which the earlier XV-2020 and X5D work had already
taught once each: a reading that produces plausible numbers is not thereby
correct, and the check is always an independent one -- a size that divides
exactly, a range that matches a known capacity, or a second source.

### 9.3 The checksum

A bulk dump ends with one byte before F7 that kwsed had always ignored.
Reading never needed it: the record arithmetic divides exactly whether or
not it is counted, so its absence was invisible for the entire life of the
project so far.

It is **the 7-bit sum of the nibble data**, confirmed against every bulk
dump in the owner's library. Building needs it -- a dump without it is one
byte shorter than what the instrument sends, and would presumably be
refused. `messages.frame_bulk` applies it.

This is a good example of a fact that only a new use exposes. Nothing was
wrong before; the code simply had never been asked the question.


---

## 10. The bank numbering: two different numbers, and a bug that lasted

**The order a person sees the banks in is not the order the wire numbers
them.** The instrument's twelve banks -- RAM 1-3, ROM 4-11, Card -- carry
these System Exclusive bytes:

| bank | RAM 1 | RAM 2 | RAM 3 | ROM 4 | ROM 5 | ROM 6 | ROM 7 | ROM 8 | ROM 9 | ROM 10 | ROM 11 | Card |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| byte | 0 | 1 | **4** | 5 | 6 | 7 | 8 | 9 | 10 | 11 | **2** | **3** |

kwsed used the display position as the wire byte from its first commit. So
`bank("CARD")` asked for byte 11, and byte 11 is ROM 10.

### Why it took so long to notice

Because nothing failed. Every read succeeded and returned a real bank full
of real names -- just not the one asked for. The symptom was subtler and
pointed the wrong way: **the card appeared never to change.** Swapping the
Hcard to another bank did not change it; a power cycle did not change it;
reformatting did not change it. Each of those correctly produced no change,
because ROM does not change, and each time the conclusion drawn was about
the card slot or the instrument's caching rather than about the number being
asked for.

Two plausible theories were built and tested on that false premise -- that
the instrument caches the card's contents, and that Multi data might only be
served in a particular mode -- and one of them consumed a power cycle of the
user's time.

### What settled it

The owner formatted a fresh empty bank on the card and read the instrument's
own display, which said `PERFORM CARD: 0 | UNNAMED`. So the front panel
could see an empty card while kwsed's "Card" was full of sounds. Those two
facts cannot both be about the same bank.

Edisyn carries the mapping outright:

```java
edisynToWSBank = { 0, 1, 4, 5, 6, 7, 8, 9, 10, 11, 2, 3 };
wsToEdisynBank = { 0, 1, 10, 11, 2, 3, 4, 5, 6, 7, 8, 9 };
```

Read against its bank list, the last entry -- the Card -- maps to byte 3.
Confirmed immediately on hardware:

```
byte  3  ->  35/35 unnamed          the freshly formatted card
byte 11  ->  Galaxis, Space Sailing ...   a ROM bank
byte  4  ->  Init x 35              RAM 3
byte  2  ->  Touch Tone, Deep Waves ...   ROM 11
```

### The lesson, which is not "read Edisyn more carefully"

The mapping was in a file this project had already read for other facts. It
was passed over because there was no reason to suspect the numbering needed
a mapping at all -- the display order and the byte order agree for the first
two banks, which is exactly enough to make the identity assumption look
confirmed.

The general form: **an assumption that is never stated is never checked.**
`Bank.number` carried two meanings and nothing in the code or the tests had
to say which. It is now two fields, `number` and `sysex`, and the tests pin
every one of the twelve.

### Still open

Whether a **Bank Select** wants the display position or the wire byte is
still not established, and it is now a sharper question than before. TODO 1.


---

## 11. Sending a bank: why rtmidi could not, and what does

The builder produced correct dumps for weeks before anything tried to send
one. The first attempt died in ALSA, not on the instrument.

**python-rtmidi cannot send a message this large.** Measured against a
virtual port: 8192 bytes go through, 16384 fails with "error sending MIDI
message to port". A patch bank is 29828. So every bank write failed before a
byte left the machine -- and the backup had already been taken, so the fence
behaved correctly on its first real outing.

### Three limits, none of them where it looked

1. **The kernel caps the client's output pool at 2000 cells.**
   `snd_seq_set_client_pool_output` accepts more, returns success, and
   clamps: asking for 100000 leaves it at 2000. "Make the buffer bigger" is
   not an available answer.
2. **There is a second, separate limit**, the userspace output buffer, and
   an event larger than it is refused with EINVAL by
   `snd_seq_event_output`. Raising only the pool leaves the dump rejected,
   with an error that points at the wrong thing.
3. **EAGAIN is normal.** A 30 kB dump is about eight seconds of MIDI at
   31250 baud; the far end drains it no faster than the wire allows and the
   sequencer refuses more until it has room.

### What works

A System Exclusive message does not have to be one sequencer event. Once it
reaches the wire it is a byte stream, so it can be handed over in many small
events whose payloads run together, and the device sees one continuous
message. `kws/alsa.py` sends in 256-byte events, retrying on EAGAIN.

**Verified byte for byte**, which matters because two earlier "successes"
were not: 29828 bytes sent through a virtual port and read back from its raw
side came out identical, `sent == received`, 29828 of 29828.

The two false successes are worth recording. `aseqdump` reported far fewer
bytes than were sent -- it is a userspace receiver with no flow control and
drops what it cannot keep up with, which reads exactly like a failing
sender. And an earlier run reported every send succeeding while nothing
arrived at all, because the events were going to a port with no subscriber.
Neither was the transport's fault, and both would have been taken for it.

### The trap that costs a segmentation fault

`snd_seq_ev_ext_t` is declared `__attribute__((packed))` in ALSA's headers.
Without `_pack_ = 1` ctypes aligns its pointer to offset 8, ALSA reads it
from 4, and the library dereferences whatever happens to be there. The
symptom is a crash with no Python traceback, which is a poor signpost to a
struct declaration.

### Still not done

**No bank has been written to the instrument.** The transport is proven
against a virtual port only; the write itself was blocked by this
environment's sandbox, not by anything in the code. See TODO.


---

## 12. The manual turned up, and settled several guesses

Korg publish the Wavestation SR Reference Guide, and its **appendix 4** is a
full System Exclusive specification. It was downloaded from korg.com and put
in the owner's `SYNTHS/WavestationSR/Manual/` folder. Everything below is now
documented rather than inferred.

### What it confirmed

* **Nibble encoding**, low four bits first -- exactly as derived.
* **The checksum**, the seven-bit sum of the nibble data.
* **`0x1C` + bank** and **`0x1D` + bank** for the patch and performance bank
  requests, and **`0x0C`** for wave sequences: all three as implemented.
* **`0x7F` means space** in a name. That had been worked out from the card
  images, where a patch called "Rich Syn Strings" stores `7F` between the
  words; appendix 4 states it outright for parameter change messages.
* The status replies **`0x21` write complete, `0x22` write error, `0x23`
  data load complete, `0x24` data load error**. kwsed had these from Korg's
  family convention and they were right. `0x26` is **not** in the
  Wavestation's list -- that one came from the X5 family -- so it is no
  longer named as a Wavestation code.

### What it corrected

**The Multi Mode Setup Dump Request is `0x06`**, not the `0x32` this project
inferred and probed. The reasoning behind `0x32` was that a request sits
0x30 below its dump, which four of the five visible pairs obeyed. The
documented request codes are 0x06, 0x07, 0x08, 0x0C, 0x0E, 0x0F, 0x10, 0x19,
0x1C, 0x1D -- there is no arithmetic relation to the dump codes at all, and
the four that appeared to follow one were a coincidence of a small sample.

Worth keeping as a lesson: a pattern that fits four of five cases is not a
rule, and the fifth exception was explained away here rather than treated as
the counter-example it was.

### What it opens up

* **`0x11` Patch Write Command** and **`0x1A` Performance Write Command**.
  A single patch or performance dump lands in the edit buffer; these commit
  it to a slot. That is the per-object write Edisyn's comment says does not
  work -- its message is malformed, passing the function code and bank where
  bank and number belong.
* **`0x0F` All Data Request**, one message for everything.
* **`0x5B` / `0x63` Multi Mode Setup Select**, which confirms 32 Multisets:
  0-15 by the first, 16-31 by the SR expanded form.
