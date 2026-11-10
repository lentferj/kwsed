<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# Building a favourites bank

The Wavestation's program structure is a graph, not a list, and that is why
assembling a bank of favourites by hand is painful. This is what the graph
actually looks like, what a builder has to do about it, and which parts are
verified.

## The dependency graph

```
Performance (181 bytes)
  16 name | 21 effects | 8 parts x 18
    part -> bank = bankNum + ((partMode >> 6) & 3) * 4
            patch = patchNum        (255 = part switched off)
        |
        v
Patch (426 bytes)
  16 name | 74 settings | 4 oscillators x 84
    oscillator -> waveBank (+2), wave = waveNum (+3, 16-bit) + waveNumExp (+83)
            wave <  32 : wave sequence `wave` in waveBank
            wave >= 32 : a wave in PCM ROM, which cannot be moved
        |
        v
Wave sequence bank (8784 bytes)
  32 headers x 16 | 501 steps x 16 | 32 names x 8
    header -> link: the first step
    step   -> fLink / bLink / lLink, and a wave number under the same rule,
              so a sequence can quote another sequence
```

Every size divides its record exactly -- `16 + 21 + 8*18 = 181`,
`16 + 74 + 4*84 = 426`, `32*16 + 501*16 + 32*8 = 8784` -- which is the check
that the split is right, because a wrong one leaves a remainder.

### How this was established

Two independent derivations that agree.

**Structurally, from the owner's dumps.** Scanning every plausible (start,
stride) over 35,550 performance records, only one gives 8 repeating groups
that end flush with the record: 18 bytes from offset 37. In that alignment
one column takes **exactly the values 0-34 plus 255** across all of them --
the 35 patches a bank holds, plus a sentinel, and nothing outside.

**Field by field, against Edisyn.** Its `Part.read` lists 18 bytes in order,
its `PerformanceStructure.read` puts them after 16 name and 21 effects
bytes, and its `Block.read` lays out the wave sequence bank as 32, then 501,
then 32. Edisyn is Apache-2.0 and cannot be used as code here; it is read
for interface facts only, and each fact above is independently evidenced by
the dumps. See LICENSE and RESOLUTION_NOTES section 2.

The chain walk was then checked against **657 wave sequence banks**: step
counts come out between 1 and 49 per sequence, unused sequences walk to
zero, and no bank's walked total exceeds the 501-step pool. One real bank
uses 495 of 501.

### Two traps

**A part's bank is split across two fields.** Four bits in `bankNum`, the
top two bits of `partMode` for the rest. Rewrite only `bankNum` and you get
a bank that loads cleanly and plays sounds from somewhere else. Nothing
reports an error.

**16-bit fields are big-endian, while the nibble encoding on the wire is
low-nibble-first.** Reading a word the other way gives values that are all
multiples of 256 and look like real data. This cost a wrong claim during
development -- that 98% of patches use a wave sequence. The verified figure,
from the rule `wave < 32`, is **46%**.

## What the builder does

**Patch first**, because the budgets bind on patches. You choose patches;
`kws.bankbuild.plan` works out what they drag along, packs them, and then
reports which performances have become complete -- every used part pointing
at a patch that made it in. Choosing performances first means discovering
late that they cannot be satisfied.

**Four budgets, not one:**

| | per bank |
|---|---|
| performances | 50 |
| patches | **35** |
| wave sequences | 32 |
| wave sequence steps | **501, shared** |

The step pool is the one that bites, and it is shared across the whole bank,
so a single long sequence can crowd out several short ones.

**Greedy, in your order.** A solver would fit more patches into 35 slots,
but it would be choosing which of your favourites to drop. Taking them in
your order and saying plainly where the budget ran out leaves that decision
where it belongs. Every rejection says which budget stopped it.

**On a template.** Output starts as a real dump of a real bank -- an empty
one from the instrument is ideal -- and slots are replaced in it. Nothing
invents the contents of a record: an initialised patch is the instrument's
idea of one, not this program's, and the difference is the kind that loads
cleanly and sounds wrong.

**Wave sequences are relocated, not renumbered.** Steps live in a pool the
bank shares, chained by link fields, so moving a sequence means copying its
chain into free slots in the destination pool and rewriting every link. The
build verifies this by re-walking each relocated chain and comparing it to
the source.

## What it does not do

**It never writes to the instrument.** It produces bytes; getting them into
the machine is the machine's own business, through its front panel. That is
the project's rule, not a limitation of this module.

**It cannot move ROM waves.** `wave >= 32` is PCM ROM. A patch whose
oscillator needs a wave from a PCM card that is not fitted will be copied
faithfully and still sound wrong -- the reference is correct, the sample is
absent. The Hcard's own banks 16-18 need WSC-01/02/03 for exactly this
reason.

**It does not check that a source bank's parts point where you think.** A
part records an absolute bank number, so a performance copied out of a card
bank refers to that card. This is the same trap the Hcard manual warns about
when it tells you to MOVE rather than COPY, and it is why the builder
rewrites references rather than copying bytes.

## Using it

```python
from kws import deps, bankbuild

a = deps.load_syx(["fav-a-perf.syx", "fav-a-patch.syx", "fav-a-seq.syx"], "A")
b = deps.load_syx(["fav-b-perf.syx", "fav-b-patch.syx", "fav-b-seq.syx"], "B")
sources = {"A": a, "B": b}

made = bankbuild.plan(sources, [("A", 3), ("A", 7), ("B", 12)])
print(len(made.patch_map), "patches;", len(made.performances), "performances")
for why in made.rejected:
    print("left out:", why)

built = bankbuild.build(
    sources,
    made,
    template_patches=empty.patches_bytes,
    template_performances=empty.performances_bytes,
    template_waveseq=empty.waveseq_bytes,
    destination_bank=11,
)
```

The template comes from dumping a freshly formatted bank off the instrument
-- on an Hcard, create an empty bank and dump it.
