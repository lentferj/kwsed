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

**Corrected 2026-11-13: the 16th part's fourth byte is the FX control
channel, not a flag.** Measured by setting MULTI:2 on the bench: the panel's
value 7 stored 6, and 16 stored 15, at byte 85 of that Multiset's 86-byte
record; the change is one byte. The panel's edit echoes parameter 29
(`MULTISET_FX_CONTROL_CHAN`), which is a globally addressed parameter that
edits the *current* Multiset -- which is why it looks per-Multiset on the
panel. KB stores 16 (0x10), measured the same day after setting MULTI:2 to KB and storing it. Byte 0 of the 22-byte head
is not the control channel; it is part of the FX head.

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

## 13. What a review of the write path found, and why each one hid

Six findings, all in code that had been exercised. None of them showed up
as an error; that is the common thread and the reason for the note.

**The refusal that only fired on some hosts.** `_send()` chunks a
bank-sized SysEx through the ALSA sequencer, because rtmidi silently
truncates above roughly 8 kB (section 11). Where no sequencer address can
be had, it refuses rather than let rtmidi try. That refusal was written
inside `if alsa.available()` -- `available()` being
`find_library("asound")` having found the shared library. But rtmidi's own
ALSA backend is linked, not dlopened: a host can carry a perfectly working
ALSA MIDI stack and still fail `find_library`. Such a host skipped the
refusal and took the silent-truncation path the refusal exists to prevent.
The condition is now about the one thing that decides the outcome -- is
there an address to chunk through -- and the libasound question is asked
only in service of that.

The reason this is worth writing down: the guard and the thing it guards
were the same idea in the author's head, so nesting one inside the other
read as a tautology rather than as a narrowing.

**Reads that accepted another bank's answer.** `read_patch` compared the
bank byte of an arriving dump with the one it requested; `read_bank` and
`read_raw` did not, and both docstrings said they did. A single-object
read on this unit can take ten seconds (TODO 3c), long enough to time out
and have the dump arrive during the *next* read -- which then filed it
under the bank it had asked for. Whole-bank reads are fast and reliable,
which is exactly why the bug survived: on the bench the wrong path is
never taken.

**The confirmation that named the wrong objects.** The fence requires
saying what is about to be destroyed, by name. `send` did read the
destination and print its contents -- but called `read_bank_names()`
without a kind, and the default is patch. Overwriting a performance bank
therefore listed that bank's *patches*. Every word on screen was true and
none of it was about what was going to change. A fence that prints names
is only a fence if they are the right names.

**A paragraph that outlived its fact.** `dump` printed three lines
explaining that the kind that comes back is the unit's choice, not the
request's, and that kwsed reports whatever arrived. That was true when
kwsed knew only `0x1C`. `0x1D` went in with performances (section 3) and
the paragraph stayed. Prose describing a limitation needs deleting at the
same commit that lifts it, or it becomes a confident statement of
something false.

**Sharing a wave-sequence step, twice.** In `bankbuild`, a Step record
reached by two different sequences was renumbered once per sequence and so
copied twice. The built bank played correctly, which is why nothing caught
it; the cost was duplicate steps and a `STEPS_PER_BANK` overflow on builds
that would have fitted. The renumber map is now keyed by `(source, old
step)` and links resolve in a second pass, since a chain can point forward
to a step the first pass has not yet reached.

**A wire byte read as a display position.** The demo device answered a
bank request by calling `banks.bank()` -- display order -- on a byte that
came off the wire. Byte 3 is the Card and resolved to ROM 4. The same
confusion as section 10, in the one place that has no hardware to
contradict it.

## 14. Quoting: the reference that survives being copied wrong

A wave sequence's step has a one-byte `wave` field at offset +7. Below 32
it is not a wave at all but another wave sequence *of the same bank*,
which the step plays in place of a sample. The manual gives the rule for
an oscillator's wave number and the same rule holds for a step's, which is
how one sequence comes to quote another -- and quoting is how a long
sequence gets assembled out of short ones, so it is common rather than
exotic.

**Measured, because "common" needed a number.** Over the 828 `.syx` files
in the library here: 657 wave sequence banks, 15679 non-empty sequences,
1752 of them quoting another. **11.2%**, spread over 453 of the 828 files.

### Why it went unnoticed for so long

`walk_sequence` had collected quotes from the start, into a field whose
comment reads "Sequences quoted by this one's steps, which must travel
with it." Nothing ever read the field. A grep for it across `kws/`,
`kwsed/` and `tools/` found only the three lines that produce it.

So the builder did two things wrong at once, and each hid the other:

1. It never pulled a quoted sequence into the plan, so the quoted sequence
   usually was not in the built bank at all.
2. It copied each step's bytes and then repaired only `forward`, `back`
   and `loop`. The `wave` field kept the *source* bank's sequence number.

The second is the interesting one. Every other reference in this format
fails loudly when it is copied wrong: a step link past the end of the pool
raises, a patch that points at a missing sequence is reported by
`closure`. A stale quote raises nothing, because **any byte is a legal
sequence number**. It addresses whatever sits at that number in the
destination -- normally a slot the build deliberately zeroed. The file is
well formed, the checksum is right, and `send` reads the bank back and
compares equal, because the bytes that went out are the bytes that came
back. The write fence does everything it promises and cannot see this at
all: the bytes are not what is wrong.

Six patches from one source file produced 36 references to zeroed slots
and 21 to unrelated sequences.

### The fix, and the second bug it uncovered

`walk_sequence` takes the bank it is walking, so a quote is
`(bank, number)` rather than `(None, number)`. `deps.quoted_closure`
expands a wanted set to include everything quoted, transitively, guarded
by a `seen` set because a sequence may quote one that quotes it back --
legal data, and an infinite loop without the guard. `closure` and `plan`
both go through it, so a quoted sequence costs a slot and its steps count
against the budget. `build` renumbers the step's `wave` through
`sequence_map`, and raises rather than writing a stale number.

Fixing that exposed a flaw in the *previous* fix, the one for shared steps
(section 13). Relocation was keyed `(source bank, step)`. A patch can name
the same sequence number in two different wave banks; a `Source` holds one
wave sequence bank, so both resolve to the same bytes and walk identical
steps -- but they are separate entries with separate destination slots,
each needing its quote pointed at its own copy. One shared physical step
cannot hold two different targets, and the second sequence written took
the first one's.

This was invisible until quoting was fixed. With the quote never
renumbered, both sequences were equally wrong, so sharing cost nothing.
The key is now `(source bank, wave bank, step)`, and `plan`'s step budget
counts in the same units, because a budget that counts sharing the build
does not perform is a budget that rejects plans that fit.

**Verified over the library**, not just the fixtures: 347 source banks
with quoting built, 7634 quoting steps in the output, all 7634 landing on
the sequence they quoted, none on a zeroed slot. Each of the four changes
was reverted in turn to confirm a test fails without it.

### A third thing, found while verifying

`Soft1.syx` and `PacificWinds.SYX` end a chain with a forward link of
`0x0700` instead of 0, in banks that are otherwise impeccable -- 8784
bytes, all 32 headers in range. `walk_sequence` stopped on a link of 0, on
a step already seen and at the size of the pool, but not on a link past
the end, so it raised out of `records.step` and took the builder with it.
It stops now. Whatever wrote those files, the instrument evidently stops
there too.

The general lesson, and the reason this section is long: the two reviews
of this code between them raised twenty-one items and none of them was
this. Both read the code for what it says. This was found by asking what
the *data* does -- by counting how many sequences in a real library quote
another, and then building a bank and reading back what the references
actually pointed at.

## 15. Bank move: reachable over MIDI, and still kwsed's own copy

The Reference Guide's **MIDI/Global** menu has a **BANK MOVE** page that
moves bank data -- WAVESEQ, PATCH, PERFORMANCE or ALL -- between banks,
including to and from the Card. Its confirmation prompt reads exactly
**"MOVE BANK DATA?"**. Earlier revisions of this note called it BANK COPY
and said it could not be reached over MIDI. Both were wrong.

It is four documented parameter changes: `COPY_MACRO_SOURCE_BANK` (213)
names the source, `BANK_COPY_TYPE` (346) the kind, `GLOBAL_UTIL_DEST_BANK`
(379) the destination, and `EXECUTE_BANK_MOVE` (472) executes on `+1`.
Banks are given as the System Exclusive byte -- RAM 1 = 0, RAM 2 = 1,
RAM 3 = 4, Card = 3 -- not as kwsed's positions. The type is 0
performances, 1 patches, 2 wave sequences, 3 all three. `EXECUTE_WRITE`
(466) is not the only write trigger in the table: 472 is a second one,
and it does name a source and a destination.

The four-change sequence was not guessed. It was captured from the unit's
own panel action -- the MIDI OUT during the move carries only the
parameter-change echo of the menu fields (213, 346, 379, 472) and Active
Sensing -- and then replayed with no panel action at all. Measured on a
bench SR (2026-11-12, global channel 5):

* **RAM 2 → RAM 3, type 3.** All three kinds arrived. The performance's
  part 0, which named the source, was rewritten to RAM 3; part 1, which
  names ROM 4, was untouched. The source RAM 2 stayed byte-identical in
  all three kinds.
* **Card (HCard bank 39) → RAM 3, type 3.** The same remap, all three
  kinds, and the source Card stayed byte-identical.
* **Type.** Measured against a distinct baseline in the destination: 0
  performances only, 1 patches only, 2 wave sequences only, 3 all three.
* **No bulk dumps.** MIDI OUT carried the parameter changes and nothing
  else, so there is no "panel kind order" to match: kwsed's waveseq →
  patches → performances order is its own wire protocol, and the per-kind
  read-back verify decides correctness.
* **The source is not cleared.** On this unit the move left the source in
  place -- tested twice, RAM → RAM and Card → RAM. That is measured
  behaviour, not a guarantee: the OS/firmware version was not recorded,
  and kwsed must not depend on it. The panel function offers no backup
  and no read-back verify either.

**Decision: kwsed still performs the copy itself.** Not because the panel
function is unreachable -- it is reachable, and the measurement above
proves it -- but because it has no backup and no verify, and because its
observed behaviour leaves the source behind, which is not a contract any
copy should rely on. kwsed reads the source in all three kinds, remaps the
performances' references to the source bank to the destination -- both
halves of the split `bankNum`/`partMode` field, because rewriting only one
yields a bank that loads cleanly and plays from somewhere else -- and
writes once through the existing fence: one destination backup envelope,
waveseq → patches → performances, per-kind read-back verify. The remap is
symmetric, measured in both directions, and matches what the device does.

Multiset pointers (`0x62`) are deliberately not remapped. The device does
not remap them either, and Multisets are a separate bank of state the
multi editor owns.

`tools/probe_bank_copy.py move` is the reproduction: with `--yes` it sends
the four changes and reads both banks back. The measurements above are its
output, committed as 4736d9b.

## 16. The card name was never in the `0x51` dump

For most of the HCard work this note assumed the System Setup dump carried
a card name at some offset, and that only the offset was unknown. It was
the one wire fact marked "confirm against hardware", with a read-only probe
(`tools/probe_card_name.py`) kept for the day a unit was available.

The probe was run on a bench SR (2026-11-12, global channel 5). The `0x51`
dump is the documented 34-field `system` structure -- small parameter
values, not ASCII -- and setting the card name with a Parameter Change did
not alter it. There is no name in the dump, at offset 0 or anywhere else.

The mistake was not the offset; it was treating "the HCard rewrites the
Wavestation's card name" as "the synth can be asked for that name". The
first is a panel behaviour, the second was never documented and is not
true. `CARD_NAME_OFFSET`, `CARD_NAME_LEN`, `read_card_name()` and the probe
were removed rather than left behind as a function that reads system
parameters and calls them a name. Card identity is the content digest
(`kws.cards.content_digest`), with first-slot fingerprints as the weaker
fallback -- a signal that never depended on the name we cannot read.

---

## 17. Self-contained whole-bank import: the 2026-11-14 diagnosis

On 2026-11-14 a two-bank ALL-data file was imported whole into the Card.
The result sounded wrong: performances whose parts named the other RAM bank
still named it after the write, so those slots pointed at whatever the Card
held there instead of the intended patches.

**Cause.** `kws.perfcopy.relocate_bank` moves **self-references only** — by
design, and matching the unit's own bank move (§15). References to another
writable bank (RAM 1-3 or the Card) and to ROM stay where they point. A
whole-bank import into the Card of a bank whose performances span two RAM
banks therefore does not become self-contained; it becomes a bank that
still references RAM banks the Card does not have. Nothing was lost — the
failure mode on the instrument is silent slots, not lost data — but nothing
sounded as authored either.

**Verification sequence.**

1. The card cache, read back after the import, matched `relocate_bank`'s
   output byte for byte. The write did exactly what the code promised; the
   code was not buggy, the expectation was.
2. A fresh capture of the same card bank (the one taken after C) was
   byte-identical to the import. The unit did not rewrite anything behind
   the write's back.
3. The RAM 1 labels seen before that capture did not come from the imported
   card bank. They came from other card archives captured the same day
   (`CARD/01`, `CARD/02`), whose performances genuinely reference RAM 1.
   Those archives were mistaken for the imported bank because they were
   captured in the same session.

**Conclusion.** `relocate_bank` is left untouched — its self-references-only
behaviour is correct and matches the device. The work that followed (the
containment audit, the as-is / self-contained 1:1 / self-contained packed
modes, companion spill-over, and the multi-pass remainder flow) is
information plus real self-contained modes, not an implicit second write and
not automatic repointing. Automatic repointing of cross-bank references
remains an opt-in helper only (plan D2).

---

## 18. Whole-bank import: a fence fix and the 2026-11-15 verification

**Bug fixed.** The whole-bank write used to hand the bridge a before-block
built from the cached bank. The bridge skips kinds that match a before-block,
so re-importing a bank whose cache matched sent no SysEx at all while the
status reported success, and the backup recorded cache bytes instead of what
was on the unit. It now lets the bridge read the unit first, so every kind
is read from hardware, written when it differs, and verified against the
read-back.

**Verification, 2026-11-15.** A two-bank ALL-data file was imported into a
freshly formatted Card, with RAM 3 as the patches-only overflow bank, and
both banks were read back. Every kind matched the intended bytes. The
Card's parts then named the Card (93), RAM 3 (109) and ROM 11 (10). No
performance was dropped. The strict self-contained split of the same file
needs three banks — the contained 16 performances fit the Card with 25
patches, while the other 34 need 56 patches in total — so it was not used
for that run.

**Staging note.** That run wrote into RAM 3, the bank named by
`preaudit_target_patches`; the screen warned about overwriting those staging
patch slots before it wrote, and the backup covered them.

---

## 19. Whole-bank import: the shared gate could authorize an unseen plan

**Verified gap.** The write gate is a single boolean: armed or locked. The
whole-bank screen also kept a screen-local fingerprint, captured when `w` armed
the gate on that screen. Overflow selection (`o`) and the preflight question
(`both` / `only` / `cancel`) are material choices that decide which physical
banks and kinds are written, and they can be made *after* the gate is already
armed. They were not in the fingerprint, and the fingerprint itself never
reached the application's write boundary. An armed gate could therefore start a
write whose banks and kinds differed from the plan the person approved when they
pressed `w`. Screen-level disarming on some edits was not equivalent to
validating the final request.

**Fix.** The plan is now data, not a screen state. `kwsed/import_intent` builds
an immutable `WholeBankIntent` from the same planners the write uses: source
identity and a digest of its records, mode, overflow kind, preflight answer,
session and pass slots, and an ordered list of physical bank writes with their
kinds and slot/remap digests. It carries a schema version and a deterministic
fingerprint (SHA-256 over canonical JSON). `PlanApproval` pairs that fingerprint
with a one-shot nonce.

The application rebuilds the same intent at `_import_whole_bank_write` before
any device read, backup or write, and requires an armed gate plus a matching,
unspent approval; the nonce is consumed there. A missing, foreign or replayed
token refuses with zero device calls. Companion writes and the preflight's
companion banks each carry and consume their own token at their own boundary.
Continuation passes re-derive their slots and mint their own approval. A
companion write sends patches and wave sequences only unless the loaded copy of
that bank is empty of performances (all Init), in which case it travels whole;
that branch is decided from loaded copies both at mint and at check.

**Counts only.** No performance, patch, wave-sequence, instrument or card name
is recorded by this change; only bank ids, kinds, slot numbers and counts are
part of a plan. This entry records the safety fix and its verification; it does
not describe a hardware run.

---

## 20. Whole-bank import: retries needed the landed banks, and the worker re-planned from live records

**Verified gaps (P7b).** Three, all found re-reading the plan-approval work:

1. *A retry's token could not name what had already landed.* P7a added
   `WholeBankIntent.skipped_banks` and wired nothing to it: the screen minted
   with `()` and the app rebuilt with `()`, so a partial attempt's remainder
   fingerprinted exactly like the first attempt. Any future divergence — the
   app deriving the landed banks from its session state while the screen did
   not — would have refused every retry.
2. *The worker re-planned from mutable sources.* The token was validated
   against the live records, then the worker planned and built from those same
   live objects (`bank.source`, the file's `Source` map). Any code that runs on
   the main thread between validation and the worker's planning — another
   device worker's completion, a later load re-pointing a session source's
   record list — could change what is sent while the fingerprint stood still.
3. *`c` minted a unit-write token.* Cache-only writes nothing to the unit, but
   the key minted an approval for the unit write first, so a bank whose unit
   write cannot be planned could not even be saved to the cache.

**Fix.** The session state that already records what landed now feeds the
intent builder: a failed patches-only target write leaves `overflow_written`
in the session, and a split or cards-only pass records its landed groups'
physical bank ids as `banks_written`; `_skipped_banks_for` folds both into one
sorted tuple, and the screen carries the same tuple in its pass state
(`ImportPassState.skipped_banks`), minted only for choices that continue that
session. The mint (`_mint_for_choice`, `_mint_whole_bank_approval`) and the
rebuild (`_whole_bank_intent`) both go through that one derivation, so they
cannot disagree: a token minted without the skip state, and the first
attempt's replayed token, are refused at the boundary with zero device calls.

The write's planner inputs are frozen immediately after validation, on the
main thread: every scope becomes a fresh `deps.Source` with copied record
lists and copied wave-sequence bytes, and the source bank becomes a frozen
stand-in over its own snapshot. The worker plans and builds from that snapshot
alone — `relocate_bank`, `build_self_contained`, `build_overflow_import` and
`build_split_partition` all read records through the map they are handed — so
its outputs depend only on records no other code can mutate. The
main-thread/worker split is unchanged: the worker still only talks to the
device, and the session, cache and undo updates stay in `done` on the main
thread.

`c` dismisses without minting again: cache-only writes no unit bank, so there
is no plan to approve, and the app takes the `write_to_unit=False` branch
before any approval check.

**Docs.** README and `docs/IMPORT_PLAN.md` §6.11 now state the token
granularity outright: an overflow or split sequence is approved as one ordered
plan (not one token per physical write), a companion chain mints one token per
answer, and every bank in a plan is still separately read, backed up and
verified.

**Counts only.** No performance, patch, wave-sequence, instrument or card name
is recorded by this change; only bank ids, kinds, slot numbers and counts are
part of a plan. This entry records the safety fix and its verification; it does
not describe a hardware run.

---

## 21. Whole-bank import: plan approval, follow-ups

The plan-approval work closed five review findings after its first
round. Each is a small correctness rule rather than a feature.

**Every planner input is fingerprinted.** The approved intent digests the
whole source map, not only the selected bank. An overflow or split copies
patch and sequence records out of sibling banks, so a change there changes
the bytes written; it now changes the fingerprint too.

**The worker plans from a frozen snapshot.** Validation happens on the
main thread, and the records it approved are copied at that moment.
Nothing reads the session's live sources afterwards, so a record changed
between approval and send cannot alter what is written.

**Retry state is part of the plan.** A bank that landed before a later
failure is recorded as skipped, and a retry's token names it. A token
minted for the first attempt cannot be replayed for the remainder, and a
patches-only overflow whose target failed re-offers the screen so the
retry sends the target only.

**Cache-only mints nothing.** Saving to the local cache writes no unit, so
it no longer requires a plan that could be planned for one; material that
cannot be placed on the device can still be filed.

**Token granularity is stated, not implied.** One approval covers one
ordered sequence (an overflow bank and its target, or a split's groups).
A companion chain mints one token per answer. Every bank is still
separately read, backed up and verified; the token never implies
atomicity.

Verified: 1478 tests pass; lint, formatting and strict typing are clean.

---

## 22. Affected performances: the gap a whole-bank write leaves behind

A whole-bank import replaces every patch slot in each bank it writes. A
performance that survives the write and points into a written bank keeps
its slot numbers but now names different sounds. Nothing in the import's
own write touches that performance, so "left untouched" performances are
still broken.

Measured on the unit:

```
RAM 2: 50 non-empty performances
their used parts: RAM 1 58 · RAM 2 57 · ROM 11 55
```

After writing the file's RAM 1 bank, 58 parts of RAM 2's performances point
at patches that are no longer the ones they were written against.

Three answers are offered before the write is approved:

1. **Green field** — clear the banks the import writes first (performances,
   patches and wave sequences, each backed up before clearing), so no
   surviving performance points at a replaced patch. The Card is never
   cleared; RAM 3 and the Card can still hold performances pointing at the
   replaced patches, and their sound can still change.
2. **Decide with counts** — read the banks the import does not replace, count
   per bank the performances whose parts name a replaced patch, and Init
   those slots (or leave them).
3. **Leave as is** — write nothing extra; the affected performances keep
   their slots and their sound changes (with a warning).

Green field clears only the banks the import writes and needs no read.
Declining it is the only time the unit is read for this question.

The Init records are bundled with the app in `kws/initbank.py`, not read
from a path at runtime: one 181-byte performance record named `Init`
(expanded to 50), one 426-byte patch record named `Init` (expanded to 35),
and a zeroed 8784-byte wave-sequence bank. See `docs/IMPORT_PLAN.md` §7.

---

## 23. Affected performances: the review fixes

An independent review of the first implementation found seven blockers.
Each fix and where it landed:

1. **The companion plan is resolved before any remediation write.** The
   preflight question is now asked when the choice is dismissed
   (`_import_whole_bank_chosen`), and only its answer goes on to the
   remediation question; a cancel at either has written nothing at all.
   The companion chain runs after the remediation's own writes
   (`_deferred_companion_step` carries a `companions_written` flag so the
   chain runs exactly once per plan), and the target write this frame
   validated follows it under the same token. The cleared and analysed
   sets include exactly the companions a "both" answer confirmed.
2. **One bank set behind the question.** `_remediation_written_labels`,
   `_remediation_cleared_banks` and `_remediation_flow_banks` all gate the
   companions on the preflight answer, so the named, cleared and analysed
   sets cannot disagree, and never name the Card.
3. **The paths that skipped the question now get it.** A companion-only
   write (`x`, the post-write popup's write), a Card target and a
   cards-only pass are all asked. Green field is offered only where a RAM
   bank would be cleared (`AffectedImportScreen(green_field=...)`); a
   pressed green-field key on a Card plan is refused with a line that
   says why. A companion plan minted after the answer carries the answer
   in its intent (`_companion_write_intent` takes the choice), and a
   remediation that empties the companion target decides the
   whole-bank/patches-only branch the same way at the mint and at the
   boundary (`_companion_write_patches_only`).
4. **The boundary is in the plan, not the UI.** `WholeBankIntent` refuses
   a `cleared_banks` entry that is the Card or a ROM bank, so
   `build_intent` refuses it too and the application refuses such a plan
   before any device I/O.
5. **Init only where something is known.** `AffectedCountsScreen` takes
   `allow_init`; when no affected slot is known (every candidate read
   failed) the key is not offered and pressing it is refused.
6. **The dead screen state is gone.** `WholeBankImportScreen._remediation`
   (never set, and so never the answer actually given) is deleted with
   its fingerprint entry and its resets; the application owns the answer.
7. **The Init records are pinned.** `kws/initbank.py`'s docstring states
   why byte literals were chosen over a resource file, and
   `tests/test_initbank.py` asserts the two records' SHA-256 digests
   (performance `769d9575…`, patch `79a5cec5…`, the T0 extraction's
   values), so a silent edit of the embedded bytes is refused.

---

## 24. Init Bank: hardware verification

Verified on the unit: `N` opens the menu; space ticks kinds; `w` arms the shared gate; `enter` shows the confirmation naming the banks and kinds; the per-kind fenced write changes only the selected kind, leaving the others byte-identical. A single-bank test ticked only Patches in one RAM bank: that bank's performances and wave sequences stayed unchanged while all 35 patch slots became `Init`; the other banks were untouched. The gate and confirmation were both exercised. Afterwards the bank was restored from its backup envelope, so the unit ended where it started. Counts only: 1 bank, 1 kind, 35 patch slots.

### Init Bank: Multi Mode Setup (4th line)

- Added a global **Multi Mode Setup** toggle to the Init menu (4th line). The Init Multi dump is embedded and pinned (derived from the on-unit 2–31 template, sets 0–15 bank 0/perf `m+c-1` level 127, sets 16–31 bank 1/perf 0/level `m+c-1`), written as one backed-up, verified `write_multi`.
