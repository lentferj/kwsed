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

## Performance-First Atomic Copying & Multi Staging

Patch-first building (below) lets performances share patches. The
performance-first modules are the opposite, deliberately: a copied
performance owns its sound outright, so editing one favourite can never
alter another. Three modules carry it.

**`kws/relations.py`** reads the graph. Performance -> Patch -> WaveSeq is
walked in both directions -- `RelationGraph.performance_relation` from a
performance down to the unique patches and sequences it reaches, and
`performances_using_patch` / `multisets_using_performance` back up from a
patch or performance to everything that would change if it were edited.
Asking whether two patches *play* the same thing despite sitting in two
different banks is `patch_structural_digest`'s job: the walked step chains
are hashed by content, links reduced to positions within the chain and
quotes to places in the closure, so two copies of one patch in different
banks compare equal while two genuinely different ones do not.

**`kws/perfcopy.py`** writes it. `plan_atomic_bank` takes chosen
performances and allocates nothing but what they need; `build_atomic_bank`
turns a plan into bank bytes; `copy_into_bank` wraps both as one
append/replace/removal against a destination bank.

**`kws/multistage.py`** plans a whole Multiset. `plan_multi_staging`
resolves each of the sixteen channels, stages into RAM the performances
that need moving, and returns the built banks plus the remapped Multiset.

### Same-bank locality, in two different numbers

Every copied Performance's active `PartRef.bank` is rewritten to the
destination bank's **interior display number** (`Bank.number`: RAM 3 = 2,
CARD = 11), and every patch a part reaches *across a bank boundary* is
copied into the destination, so after a copy the sound needs only the bank
it now lives in. The dump **framing**, meanwhile, names the same bank with
`Bank.sysex` (RAM 3 = 4, CARD = 3), because that is the byte a dump request
and reply carry on the wire.

Two different numbers for the same bank, on purpose: `Bank.number` is the
position a person counts in (`RAM 1`-`RAM 3`, `ROM 4`-`ROM 11`, Card --
display 11 is the Card), while `Bank.sysex` is what the instrument
addresses (`ROM 11` = 2, RAM 3 = 4, the Card = 3). A part's interior record
holds the display number; the wire holds the sysex byte. Conflating the two
was a real bug here -- every "Card" kwsed read was really ROM 10, and
nothing errored -- so the copy rewrites one of them and frames with the
other, and never the reverse.

### Strict isolation between performances, dedup within one

Two different Performances **never share a target patch slot**. If two of
them name the same source patch, each receives a private copy, so editing
one cannot be heard through the other. Inside a *single* Performance the
rule relaxes: two parts of the same Performance naming the same source
patch share that one private slot, because a performance edits its own
parts together. `AtomicPerfAllocation.allocated_patches` records exactly
which source patch went to which target slot, per performance.

### Zero-fragmentation contiguous packing

Performance slots run `0..P-1` and patch slots `0..M-1` with no gaps in
between. An append fills the first free slot; a replace or a removal
compacts whatever follows it and rewrites every affected PartRef, so no
later edit has to know where a slot once was. The budgets are unchanged:

| | per bank |
|---|---|
| performances | 50 |
| patches | **35**, first-come private |
| wave sequences | 32, shared |
| wave sequence steps | **501, shared** |

### Wave sequences are shared, and their sharing is the point

While patches are duplicated for isolation, the wave sequences beneath them
are still shared: duplicated patches point at one relocated copy of each
sequence in the destination's 32-sequence / 501-step pool, and steps the
source banks already share are packed once. A step's `wave` below 32 names
another sequence and is renumbered through the same map; **PCM ROM waves
(`wave >= 32`) are not moved** -- they are firmware, and the reference
carries over unchanged.

### From the command line

```sh
kwscli deps CARD/07 performance 3
kwscli deps RAM1 patch 12
kwscli copy-perf CARD/07:3 CARD/13:11 --to RAM3 --mode append --out staged
```

`deps` prints the graph -- whole bank, one performance (parts, unique
patches, sequence step cost, which patches are shared), or one patch (its
waves, who uses it) -- and needs no unit when the banks are cached.
`copy-perf` rewrites one destination bank from the named performances; with
`--out` it stops at writing `{PREFIX}-perf.syx` / `-patch.syx` / `-seq.syx`
and sends nothing, while without it the write goes through the same
arm-then-fire fence as everything else, asking for `--yes` and backing up
the destination first. `tools/build_bank.py --perf ID:N` is the file-only
equivalent.

### In the interface

* `d` opens the **dependency inspector** for whatever is under the cursor:
  a Performance, a Patch, or a Multiset channel.
* `y` opens the **copy-performance** screen: it plans against the chosen
  destination, shows the target slots and the budget, arms with `a` and
  writes with `w`. Opening sends nothing; arming sends nothing; firing is a
  different key and works only while armed.
* Inside `MultiEditScreen`, `b` now accepts scoped card banks (`CARD/07`,
  `CARD/c1`), and `s` stages the card performances into RAM (RAM 3 first,
  spilling into RAM 1 and RAM 2 when one bank's 35-patch budget runs out)
  because the synth has one card slot and two channels cannot hear two
  different cards at once. `a` / `w` then writes the staged bank(s) *and*
  the remapped Multiset together.

### One logical operation is one undo

Whether it is one bank written, a performance copied, or a staged Multi
spanning several banks plus its Multiset, `z` undoes the whole thing in one
step: the backups the write fence made are what makes that reversal
possible, and no partial state survives between keystrokes.

## Importing existing banks and performances

Two CLI paths read other formats; both end in the same `deps.Source` model:

* `kwscli import FILE` reads a **whole bank** into the local bank cache. It
  understands SysEx (`.syx`/`.sysex`; one type per file, all three in one
  file, single performance/patch dumps, and multi-bank files), Standard MIDI
  files carrying SysEx, SoundQuest `.sqs` banks whose bodies are embedded
  Korg SysEx, and Edisyn's standard bank dumps. `--list` shows what a file
  holds; `--as` files a card bank under a `CARD/NN` slot. A bank keeps its
  own interior `PartRef`/`WaveRef` bank references, so an internal bank is
  kept under its own scope; relocating performances is `copy-perf`'s job.
* `kwscli copy-perf SCOPE:SLOT --to BANK --from-file FILE` imports a
  **single performance with its dependencies**. The performance goes to the
  first free slot in the target bank (append) or into a clean bank, its
  patches are copied privately and packed contiguously with no gaps, and
  every part is rewritten to the new patch slots. `--out PREFIX` writes the
  three `.syx` files offline; `--yes` writes the target bank through the
  fenced `write_bank_bundle` (backup, send, read-back).

A SoundDiver `.LIB` now decodes its **patch** entries. The 410-byte patch
payload is the common section split around a two-byte prefix, followed by
the four oscillator blocks in the adaptation's byte order (the order was
derived from 42 real library/source patch pairs). A patch's `RAM1-NN`
comment selects its slot; when several entries name one slot, the last
entry is kept and the note says how many were replaced. **Performance**
entries carry 158 bytes of SoundDiver's normalized part values, not the raw
181-byte performance record -- they do not even share a byte multiset --
so they are listed by `--list` but not converted; export those as `.syx`
from SoundDiver. A `0x50` All Data dump is decoded
into its two RAM banks: the six fixed bank blocks (`RAM1`/`RAM2`
performances, patches and wave sequences) sit at the end of the payload,
so the prefix before them is derived from the length and checked; a
payload with any other shape is refused rather than guessed at.

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
