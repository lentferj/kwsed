<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# kwsed

A terminal browser for the **Korg Wavestation SR**.

> **Prototype. Its read path works against a real WS SR; parts are still
> unverified.** Discovery, the identity reply and bank dumps — including
> from a fitted program card — were exercised on hardware on 2026-09-12.
> What is *not* established is which of the twelve banks a Bank Select
> actually reaches, and nothing inside a record past its name is decoded.
> **Read [DISCLAIMER.md](DISCLAIMER.md) first.**

Two panes: bank-and-kind on the left, that bank's slots on the right.

```
 bank    kind  n         #    name
 RAM 1   perf  50        00   Glass Meridian
 RAM 1   patc  35        01   Slow Tideline
▸Card    patc  35        02   Amber Drift
 ROM 4   perf  50  ro    03   Kelp Forest

 Card patch 02  Amber Drift
```

## Support this project

kwsed is free software and always will be. Nothing is behind a paywall, no
feature is withheld, and none of what follows changes that.

But if it has been useful — if it saved you an evening of mapping zones
by hand, or got a library onto a machine that had no way of reading it, or
**best of all, if it has your vintage instrument switched on and in use more often
than it was, and you are having more fun with it** — then please consider
supporting the work.

**Because here is what it has actually cost:**

- **Real machines on a real bench.** Much of what these tools know about these
  formats was measured on hardware rather than read anywhere, because for most
  of it there is nowhere to read it. That needs the machines — the instruments
  themselves — and it puts hours of wear on hardware that has been locked up and
  recovered more than once in the course of it. Some of these instruments were
  bought specifically to add and verify a format; the others were already here,
  because the person doing this is a vintage instrument enthusiast first and the
  projects exist because the instruments were in the room.
- **Dozens — realistically hundreds — of hours of human time.** Format
  reverse-engineering is slow: measure, be wrong, measure again. A single
  parameter law in this README can represent an evening at the bench.
- **AI assistance, which is a paid service**, used heavily throughout and not
  cheap at this volume.

**This is support, not a donation — and the distinction is a legal one, not a
turn of phrase.** The maintainer is based in Germany, where payments like these
are *not* `Spenden` in the tax sense: they count as **taxable income** for the
recipient and are **not tax-deductible** for the giver. So this section is
titled *Support*, deliberately, and no receipt for tax purposes can be issued.
(That is a statement of how it is handled here, not tax advice.)

If the project saved you the work, you can support it through
**[GitHub Sponsors](https://github.com/sponsors/lentferj)** — the *Sponsor*
button at the top of the repository. Payment is handled entirely by GitHub and
Stripe, so bank and tax details are never handed to the person paying.

**Support is not expected, and it is not the only currency.**

- **Bug reports** — ideally with the bank, preset or disk image that produced
  them. A tool's failures are usually specific to one file rather than
  general, and without that file they are very hard to reproduce.
- **Confirmations from hardware that is not on this bench**, which matters more
  here than for a single-machine tool. These tools write for whole *families*
  of instruments, and the bench holds only a subset. Whether a variant accepts
  what we write is genuinely unknown, and several notes say "on this unit" for
  that reason. A "loads fine here too", or a "no, mine refuses it", is worth a
  great deal.
- **Corrections to the reverse-engineering notes.** The wrong turns are
  recorded next to the findings in `docs/RESOLUTION_NOTES.md` — retractions
  included, because a finding that was withdrawn is as useful as one that
  stood. If any of it is wrong in a way that is still costing someone time,
  saying so improves the record.

---

## AI assistance & human authorship

kwsed was built by its human author together with AI assistance. The
**ideas, the project vision, and every feature** came from the human author;
AI assisted with **writing the code and analyzing the binary formats**.
Crucially, the **reverse engineering rests on hands-on human work** — all testing
and verification on real hardware, creating the reference images/banks on those
instruments (disk saves, SysEx probes), and aural A/B comparison of presets —
which is what makes the results correct.
Full account in [DISCLAIMER.md](DISCLAIMER.md).

---

---

## ⚠️ Use at your own risk — back up first

 is provided **as is, with absolutely no warranty and no liability**
for data loss or **hardware damage**. You assume all risk. Full terms:
[DISCLAIMER.md](DISCLAIMER.md).

Before you use this software, **make good, current backups of all your files** —
and of any existing banks on your instrument and storage media. These tools can
write to hardware and storage media; a mistake, a bug, or untested output could
overwrite or corrupt data, or be rejected by hardware. Always test on a spare
unit or emulator **before** connecting irreplaceable equipment.

---

## What it does

- **All twelve banks, both kinds.** RAM 1-3, ROM 4-11 and the **Card** slot,
  each holding 50 performances and 35 patches.
- **Reads names off the unit, silently.** `r` asks for a whole bank in one
  System Exclusive message. Nothing is selected and nothing sounds.
- **Names from two sources, kept distinct**: a catalog built from your own
  `.syx` dumps, and what the unit says. A name read from hardware shows in
  bold; one that *disagrees* with the catalog gets a `*`.
- **Finds the unit and its global channel by itself.** A Wavestation answers
  a Universal Identity Request, and the reply carries the channel it is set
  to — the one number you cannot guess and without which every request is
  met with silence.
- **A CLI** (`kwscli`), most of which needs no unit at all.

### Multi mode, and the word's two meanings

"Multi" names two different objects here, and mixing them up is easy:

* a **performance** combines up to eight parts, each a patch with its own
  MIDI channel and key range — the multi-timbral *sound*;
* a **Multiset** is MIDI multi mode proper: 32 of them, each saying which
  performance answers on each of the 16 channels, and at what level.

`m` in the browser, or `kwscli multi`, shows the second. One request, three
replies, nothing selected and no sound. The legend calls that key
`m channels`, because in every browser in this family `m` is channels and a
Multiset is precisely a channel assignment — Multiset 3 is what answers on
MIDI channel 3. The subcommand keeps `multi`, which is the instrument's own
word for the object.

### A performance is not a patch

They are different objects, and a bank holds both:

- a **patch** is a sound: four oscillators, each playing a wave or a wave
  sequence;
- a **performance** combines up to **eight parts**, each pointing at a patch,
  with its own MIDI channel, key range and level.

So the performance is the multi-timbral object, and usually what a sequencer
wants. kwsed keys them separately throughout — both words begin with `P`, and
an abbreviated key merges a bank's 50 performances with its 35 patches
without any error at all. That was a real bug here, caught by a test.

### The reply decides which kind you get

One request code asks for a bank, and the unit answers with performances
*or* patches — **its** choice, not the request's. kwsed therefore reads the
bank and the kind out of the **reply** and files the names under that,
rather than under what it asked for. A late answer filed under the wrong
bank is a silent, permanent error in a name catalog.

### Selecting is a best effort, and says so

`select` sends Bank Select and a Program Change, but **which of the twelve
banks a given Bank Select reaches has not been established** — the manuals
to hand are scans with no text layer. kwsed sends the bank's own System
Exclusive number and reports what it sent, rather than implying it knows
where that lands. See [TODO.md](TODO.md).

## Install

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
```

Needs Python 3.11+, `python-rtmidi` and `textual`, plus **vinsynlib** — the
shared base of this family of terminal instrument tools: the settings cache,
the favourites database, the keymap and legend, the command line and the port
listing. It comes from PyPI with the rest. To develop against a *working copy*
of it instead of the published one, install that alongside — it shadows the
published version in this environment only:
`.venv/bin/pip install --no-deps -e ../vinsynlib`.

`--system-site-packages` here is deliberate: ALSA's sequencer and the
system `python-rtmidi` are used rather than a fresh build.

## Run

```sh
kwsed --demo      # built-in demo device; opens no MIDI ports
kwsed             # find a Wavestation and adopt its global channel
kwsed --port "U6MIDI Pro:U6MIDI Pro MIDI 1 44:0" --device-channel 5
```

### Keys

| | |
|---|---|
| arrows, `tab` | move; `tab` switches pane |
| `enter` | select this sound — **it will sound** |
| `r` | read this bank's names from the unit — **silent** |
| `/` | search names |
| `m` | channels: the 32 Multisets and their 16 channels — **silent** |
| `i` | device identity and the unit's global channel |
| `?` / `q` | help / quit |

**Moving the cursor never sends anything**, enforced by a test.

### Command line

```sh
kwscli banks                      # the bank table (no unit needed)
kwscli list RAM1 performance      # one bank's slots (no unit needed)
kwscli find "glass"               # search names (no unit needed)
kwscli ports                      # MIDI ports on this host
kwscli status                     # who is there, and on which global channel
kwscli dump CARD                  # read a bank's names (silent)
kwscli multi                      # the 32 Multisets (silent)
kwscli select RAM1 patch 12       # select a sound ON THE UNIT
```

## Building a favourites bank

The instrument's programs form a graph: a performance names patches, a patch
names wave sequences, and a wave sequence is a chain of steps drawn from a
pool the whole bank shares. Copying a favourite between banks therefore
means rewriting references, not copying bytes -- which is why the Wavestation
itself distinguishes COPY from MOVE, and only for whole banks.

`tools/build_bank.py` builds one. **Patch first**: you name the patches, it
works out what they drag along, packs them against all four budgets (50
performances, 35 patches, 32 wave sequences, 501 shared steps), and reports
which performances came out complete and exactly why anything was left out.

```sh
python3 tools/build_bank.py \
    --source A=dumps/cardA-*.syx --source B=dumps/cardB-*.syx \
    --template dumps/empty-*.syx \
    --patch A:3 --patch A:7 --patch B:12 \
    --out favourites
```

It writes `.syx` files and **never touches the instrument** -- loading them
is the machine's own business, through its front panel. See
[docs/BANK_BUILDER.md](docs/BANK_BUILDER.md).

## Development checks

The toolchain is pinned in `requirements-dev.txt` and installed into the same
`.venv` the project already uses:

```sh
make bootstrap   # install the project and the pinned toolchain
make check       # lint, typecheck, test, audit
make help        # every target
```

`make check` runs **ruff** (linting and formatting), **mypy --strict**,
**pytest** with coverage, and an audit pass of **pip-audit**, **vulture**,
**deptry** and **detect-secrets**. It stops at the first failure.

Individual pieces: `make lint`, `make format-check`, `make typecheck`,
`make test`, `make audit`. `pre-commit install` runs ruff, mypy and
detect-secrets on every commit.

**mypy is strict and passes outright** — all 28 source files, with nothing on
a baseline. The one relaxation is `disallow_untyped_defs` and
`disallow_untyped_calls` for `tests/` and `tools/`: annotating a test is close
to no work, and requiring it there would bury anything real.

Ruff keeps 786 pre-existing findings suppressed rather than fixed — every one
is listed, with a reason, in [docs/QUALITY_CHECKS.md](docs/QUALITY_CHECKS.md).
None of them suppresses a *new* finding: the rules are all enabled, and the
per-file exemptions name specific codes, so a long line added to a file whose
complexity is ignored is still caught.

## Names are not included

kwsed ships **no** Korg content. The bank table and the numbering are its
own; the names come from your own dumps:

```sh
python3 tools/extract_catalog.py ~/path/to/*.syx
```

That writes `kws/data/catalog.json`, which is gitignored. kwsed works
without it, showing numbers only — and a connected unit can supply every
name.

Screenshots of the demo browser are captured with
`python3 tools/capture_screenshots.py`.

Where two files hold the same bank and kind, the **first wins** and the rest
are reported. Silently keeping whichever was read last would produce a
catalog matching no actual instrument.

## How the dump format was worked out

The manuals to hand are scans with no text layer, so the format was derived
from dumps the machine produced, then confirmed on hardware:

- The Wavestation encodes each data byte as **two MIDI bytes, low nibble
  first** — which is *not* what the X5 family does, and assuming Korg is
  consistent across its own range costs an afternoon.
- The sizes then come out exact: from offset 6, a performance bank yields
  `9050 = 50 × 181` bytes and a patch bank `14910 = 35 × 426`, neither with
  anything left over. Two unrelated dumps landing exactly is not what a
  wrong reading does.
- A single-performance dump (function `0x49`) independently yields 181 bytes
  **from offset 7** — one byte later, because that message carries a number
  as well as a bank — agreeing with the bulk dump's division.
- Every record then begins with a legible name: all 35 patches in a bank,
  all 50 performances.

`docs/RESOLUTION_NOTES.md` records it in full, including the wrong turns.

## Edisyn

[Edisyn](https://github.com/eclab/edisyn) has Wavestation support and is
**Apache-2.0**, which is not compatible with this project's GPL-2.0. It was
read **only as a reference for interface facts** — the bank list, the nibble
encoding, the dump sizes — each of which is independently evidenced by dumps
this instrument produced. **No Edisyn code is used or derived here.** See
[LICENSE](LICENSE) and `docs/RESOLUTION_NOTES.md` §2.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE) for the copyright notice and
third-party attributions, and [COPYING](COPYING) for the licence text.

Not affiliated with KORG INC.
