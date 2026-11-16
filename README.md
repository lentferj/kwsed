<!--
SPDX-License-Identifier: GPL-2.0-or-later
SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
-->

# kwsed

A terminal browser for the **Korg Wavestation SR**.

> **Prototype. Its read path works against a real WS SR; parts are still
> unverified.** Discovery, the identity reply and bank dumps — including
> from a fitted program card — were exercised on hardware on 2026-09-12.
> `select` now sends Bank Select (CC0 = 0, CC32 0–5) and a Program Change
> per Player's Guide §4.8 and Reference Appendix 3 §1.3, but kwsed cannot
> read whether the SR's Bank Select MIDI-receive parameter is ON; nothing
> inside a record past its name is decoded.
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

---

## ⚠️ Use at your own risk — back up first

kwsed is provided **as is, with absolutely no warranty and no liability**
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
- **The HCard's forty banks, archived.** A Hypersynth HCard-101 multiplexes
  forty banks behind the one Card slot; kwsed lists all forty, greys the ones
  not yet captured, and reads the active one with `C` — performance names,
  patch names and a content digest — filing it in `cards.json`. `X` walks
  them with a prompt to select each bank on the card. Everything is
  **read-only** on the unit; `kwscli cards` shows the archive and can write
  one bank out as a names-only `.syx`. Banks 16-18 say when they need a WSC
  PCM card.
- **Reads names off the unit, silently.** `r` asks for a whole bank in one
  System Exclusive message. Nothing is selected and nothing sounds.
- **Shows transfer progress in the app, not on the unit.** Every System
  Exclusive exchange — bank reads, imports, staging, undo — runs off the UI
  thread with a bar under the status line and the operation's name, so a
  ten-second bank dump does not freeze the app and does not need anyone
  watching the instrument's display.
- **Names from two sources, kept distinct**: a catalog built from your own
  `.syx` dumps, and what the unit says. A name read from hardware shows in
  bold; one that *disagrees* with the catalog gets a `*`.
- **Finds the unit and its global channel by itself.** A Wavestation answers
  a Universal Identity Request, and the reply carries the channel it is set
  to — the one number you cannot guess and without which every request is
  met with silence.
- **Copies performances atomically.** `y` in the browser, or
  `kwscli copy-perf`, puts the performance in the first free slot, copies its
  patches privately, packs them with no gap, and rewrites every part to point
  at the new slots. The patch, sequence and step budgets are checked before
  anything is written.
- **Copies whole banks, safely.** `B` in the browser, or
  `kwscli copy-bank --yes SRC DST`, copies wave sequences, patches and
  performances together from RAM to Card, Card to RAM or RAM to RAM. It
  rewrites the performances' references to the source bank so the copy is
  self-contained, backs up the destination first and verifies every kind.
- **Remembers what a Multiset pointed at.** A snapshot per Multiset warns on
  load when the Card bank or a RAM performance it was built against has
  changed; `r` in the Multi editor records existing setups without sending
  anything, and the same record rides in the backup envelope so a rebuild
  after a reset knows which card to insert.
- **Shows what a sound depends on.** `d`, the `D` pane and `kwscli deps` walk
  Performance → Patch → WaveSeq, with structural deduplication.
- **Builds favourites banks offline.** `tools/build_bank.py` writes `.syx`
  files and never touches the unit; patch-first and performance-first orders
  are both supported.
- **Imports whole banks and single performances** from SysEx, Standard MIDI,
  SoundDiver `.LIB`, SoundQuest/MidiQuest `.sqs`, FAT12 floppy images, `.zip`
  archives and `.bnk` bundles, in the browser with `I` or via `kwscli import`.
- **Caches whole bank records locally**, not just names. The browser,
  `deps` and `copy-perf` all use the same per-bank cache.
- **Writes behind a fence, with backups and undo.** Every write reads the
  destination first, saves an envelope, writes, and verifies the read-back.
  The browser has a write gate and `z` undo; the CLI requires `--yes`.
- **Favourites, tags, notes and star ratings**, stored per slot and kept for
  HCard banks and physical cards separately.
- **Multi-card Multisets.** The Multi editor can accept scoped card banks and
  stage their performances into RAM, so one Multiset can span several cards;
  the staged banks and the Multiset are written and undone as one bundle.
- **Remembers UI state.** Pane visibility and the scan offer live in
  `ui-state.toml`, separate from the connection settings.
- **A CLI** (`kwscli`), most of which needs no unit at all.

Feature map:

| feature | documented in |
|---|---|
| Cards, physical cards, HCard | [Cards](#cards-card_mode), [docs/CARDS.md](docs/CARDS.md), [docs/HCARD.md](docs/HCARD.md) |
| Multisets and multi-card staging | [Multisets and multi-card staging](#multisets-and-multi-card-staging) |
| Atomic performance copy and dependencies | [Dependencies and atomic copying](#dependencies-and-atomic-copying) |
| Bank builder | [Building a favourites bank](#building-a-favourites-bank), [docs/BANK_BUILDER.md](docs/BANK_BUILDER.md) |
| Imports, disk images, `.LIB`, `.sqs` | [Importing banks and performances](#importing-banks-and-performances), [docs/IMPORT_PLAN.md](docs/IMPORT_PLAN.md) |
| Bank cache | [Local bank cache](#local-bank-cache) |
| Writes, backups and undo | [Writes, backups and undo](#writes-backups-and-undo) |
| Favourites, tags, notes, ratings | [Favourites, tags, notes and ratings](#favourites-tags-notes-and-ratings) |
| Remembered UI state | [Remembered UI state](#remembered-ui-state) |
| Every key and CLI command | [Keys](#keys), [Command line](#command-line) |

### Cards: `card_mode`

The one card slot can be empty, hold one ordinary Program Card, a series of
them (swapped by hand), or an HCard-101 with forty banks. Choose the model in
`~/.config/kwsed/config.toml`:

```toml
card_mode = "cards"          # "none" | "cards" | "hcard"
show_card_defaults = false   # hcard: show the manual's bank names before capture
```

- `none` — no Card rows; the card actions are off.
- `cards` (default) — ordinary Program Cards. `C` reads the inserted card,
  identifies it by a content digest (the card name is unreadable over MIDI),
  and keeps its names and favourites per card in `physical-cards.json`. New
  content opens a picker: an archived card (e.g. after editing it on the unit)
  or "New card". `L` labels a card; `⏎` auditions only the card in the slot.
- `hcard` — the HCard-101's forty banks, switched on the card itself. `C`
  captures the active bank, `X` sweeps 00-39, `L` labels. Favourites are kept
  per bank, and `show_card_defaults` shows the manual's bank names for banks
  not captured yet.

See [docs/CARDS.md](docs/CARDS.md) and [docs/HCARD.md](docs/HCARD.md).

### Multi mode, and the word's two meanings

"Multi" names two different objects here, and mixing them up is easy:

* a **performance** combines up to eight parts, each a patch with its own
  MIDI channel and key range — the multi-timbral *sound*;
* a **Multiset** is MIDI multi mode proper: 32 of them, each saying which
  performance answers on each of the 16 channels, and at what level.

`m` in the browser, or `kwscli multi`, shows the second. One request, three
replies, nothing selected and no sound. The legend calls that key
`m multi`, matching `kwscli multi` and `e edit multi`: the key reads the 32
Multisets and lands on them, and the right pane is the selected one's 16
channels. `multi` is the instrument's own word for the object; the hint
names what the key reads rather than the view it opens.

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

### Selecting sends Bank Select and a Program Change

A Program Change means a Performance only in Performance (OMNI/POLY/MONO)
mode; in MULTI mode the same message selects a Multiset. So `select` first
sets the MIDI mode to POLY and selecting a Multiset sets it to MULTI,
using a Parameter Change for global parameter 4 (Reference Appendix 4
§2.1.14/§5.12). Neither is a memory write.

`select` then addresses the SR's twelve banks as six MIDI banks: Bank Select
MSB (CC0) = 0, Bank Select LSB (CC32) = 0–5, then Program Change
`program_base + slot`. Each MIDI bank holds two internal banks of 50, so
Program Change 0–49 selects the first and 50–99 the second (Player's Guide
§4.8, Reference Appendix 3 §1.3). The SR's Bank Select MIDI-receive parameter
must be ON; kwsed cannot read it.

The SR has no patch mode: a Program Change selects a performance (or a
Multiset in Multi mode), never a patch. `kwscli select` and the browser both
refuse patch slots; a patch is heard through a performance that uses it. See
[TODO.md](TODO.md).

---

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

---

## Run

```sh
kwsed --demo      # built-in demo device; opens no MIDI ports
kwsed             # find a Wavestation and adopt its global channel
kwsed --port "U6MIDI Pro:U6MIDI Pro MIDI 1 44:0" --device-channel 5
```

### Keys

| key | action |
|---|---|
| arrows, `tab` | move; `tab` switches pane |
| `enter` | select this sound — **it will sound** |
| `r` | read this bank's names from the unit — **silent** |
| `S` | scan every real bank into the local cache — **silent**, cancellable |
| `/` | search names |
| `f` / `F` | favourite this slot / cycle all slots → this bank's favourites → every favourite (silent) |
| `t` / `n` | edit tags / note on a favourite (silent; `f` first) |
| `V` | filter by star rating (silent) |
| `c`, `[`, `]` | pick / step the MIDI channel used for select and audition |
| `m` / `e` | read the 32 Multisets / edit the selected one — **silent** |
| `d` / `D` | show the selection's dependencies / show-hide the dependency pane — silent, remembered |
| `p` | show/hide the patch rows in the bank list — remembered (they can be inspected, not selected) |
| `y` | open the unified copy session for this performance, prefilled and ready for the target/slot map |
| `B` | copy the whole bank under the cursor into another writable bank — all three kinds, backed up and verified |
| `I` | import/copy: whole bank from a file or unit bank, or select performances from files, RAM, ROM or Card into one target bank |
| `z` / `w` | undo the last write / toggle the write gate |
| `C` / `L` / `X` | capture the inserted card / label it / sweep HCard banks — read-only on the unit |
| `i` / `?` / `q` | device identity / help / quit (with staged audition changes still on the unit, quitting asks first: restore them, leave them, or cancel) |

**Moving the cursor never sends anything**, enforced by a test.

`S` is the bulk version of `r`: it reads every real bank and kind, caches the
records, and can be stopped with `S` again between banks. The demo refuses it
rather than writing invented names into the live cache. `C`, `L` and `X` do
nothing when `card_mode = "none"`; `X` is HCard-only. `c`, `[` and `]` set the
MIDI channel used for selection and audition; the unit's own SysEx base
channel is the separate `--device-channel`.

On the unified **copy** session (`y`), choose the target bank, adjust the
prefilled slot map, use `s` to add more sources, `w` to arm and `ctrl+s` to
write; `?` opens help and `esc`/`q` goes back. In the **Multi editor**
(`m`, then `e`), `b` chooses a bank (including `CARD/07` and `CARD/c1`), `s`
stages card performances into RAM, `space` toggles a channel's used flag,
`+`/`-` nudges its level, `n` renames it, and `a`/`w` arm and write the staged
bundle.

### Writes, backups and undo

Every write is fenced equally, whether it comes from the browser or the CLI:
the destination is read first, a backup envelope is saved, the write is sent,
and the read-back is verified. If the first read fails, nothing is written at
all. CLI writes require `--yes`; `--backup-dir DIR` chooses where the
envelope goes.

The browser starts with the **write gate locked**. `w` arms or locks it;
`--allow-write` starts it armed. While the gate is armed, `z` restores the
most recent write. A copy restores the destination bank; a multi-card staging
write restores every staged bank *and* the Multiset in one step; a failed
write leaves the gate and the backups intact so it can be retried or undone.

`kwscli send` writes one bank `.syx`; `kwscli copy-perf --yes` writes a copied
performance bundle; `kwscli backup restore --yes` restores an envelope.
`kwscli backup list` and `backup save` use the same envelope format;
`backup save` reads banks off the unit with `--yes` and never writes. Envelopes
default to `~/.local/share/kwsed/backups` on Linux.

### Favourites, tags, notes and ratings

`f` toggles a favourite without sending MIDI; `F` cycles three views: all
slots, this bank's favourites, every favourite. `t` sets comma-separated tags
and `n` a free-text note; both require an existing favourite and say so
otherwise. `V` filters by 0–5 stars. These live in the favourites database
(`--favorites PATH`, default `~/.local/share/kwsed/favorites.db`), not in the
bank cache. HCard banks and physical cards keep their favourites separately,
so the same slot number on two cards cannot collide.

### Dependencies and atomic copying

`d` shows what the selected performance uses; `D` keeps a live dependency pane
open and remembers that choice. `kwscli deps BANK [KIND NUMBER]` prints the
same graph, using `--source ID=GLOB` or the local cache and needing no unit
when the bank is cached. The graph is Performance → Patch → WaveSeq; identical
patches are deduplicated structurally, not by name. When a part points at a
writable bank's patch that other performances also use, the line names them
(`also RAM 2:04`): editing that patch changes every performance listed. ROM
patches are read-only, so no `also` list is shown for them.

`y`, or `kwscli copy-perf SCOPE:NUMBER...`, copies a performance atomically:

- it goes into the first free performance slot (or into a clean bank with
  `--mode clean`);
- every part's patch is copied privately, so editing one copy cannot alter
  another performance;
- the patch bank is packed contiguously, with no gap;
- every part is rewritten to the new patch slot, and the patch/sequence/step
  budgets are checked before anything is sent.

The copy screen names the **destination bank** and **copy mode**. `b` opens a
bank list (RAM 1, RAM 2, RAM 3, Card), `t` opens the target slot list (all 50
slots, with `free`, `Init`, or `used` state), and `m` chooses:

- **append** — keep the destination's performances and add this one to the
  first free performance slot; the destination must still have room for the
  performance and its private patches. Factory `Init` slots count as free, so
  a not-yet-used bank does not look full.
- **clean** — replace the whole destination bank with this performance.
- **overwrite slot NN** — `t` selects one occupied slot and replaces that
  performance; the preview says `copy mode: overwrite slot NN`.
- **free/Init slot** — selecting the first free slot uses append; selecting a
  later free slot is refused with the first free slot named, because append
  has no destination-specific placement there. `Init` is recognised as free,
  not as a used performance.

The preview says `cannot copy` when the plan does not fit, and `a`/`w` refuse
to write it; switch the mode with `m`, choose another target with `t`, or
choose another bank with `b`. `?` on the copy screen opens its own help. `--out PREFIX` writes the three `.syx`
files and touches no unit; `--yes` writes the destination through the fence.
`--source ID=GLOB` and `--from-file FILE` supply the source; the latter also
accepts every import format below except the undecoded KryoFlux flux. Use
`--backup-dir` to choose the destination backup directory.

### Local bank cache

`kwscli import` files a bank in the per-user **bank cache**, one JSON envelope
per scope, under `~/.local/share/kwsed/banks` on Linux
(`--bankstore DIR` overrides). A card scope such as `CARD/07` becomes
`CARD__07.json`. The cache stores performance, patch and wave-sequence
records, because `copy-perf`, `deps` and the browser need the records, not
just names. Multisets are read for listing but are not stored there; the live
Multiset view reads them from the unit.

The cache is not the name catalog. Names read from the unit are a separate
live layer (`--live-names PATH`), and the generated catalog remains the
read-only fallback. `import --as` writes the cache; the other commands read it
unless `--source` or `--from-file` supplies the bank directly.

### Remembered UI state

Connection and card settings live in `~/.config/kwsed/config.toml`
(`--config PATH`): `card_mode` and `show_card_defaults`. Pane visibility and
the scan offer live in `~/.config/kwsed/ui-state.toml` beside it, so a
hand-edited dotted key cannot freeze settings saves:

```toml
[browser]
show_patches = true
deps_pane = true
[scan]
offer_dismissed = false
[import]
folder = "/path/to/start"
```

Older builds wrote these into `config.toml`; kwsed migrates them
automatically. Both files are safe to delete, and so is the remembered import
folder. `--favorites`, `--live-names` and `--cards` point at the other state
files.

| path | contents |
|---|---|
| `~/.config/kwsed/config.toml` | port, channel, card mode, HCard defaults |
| `~/.config/kwsed/ui-state.toml` | pane visibility, scan-offer state and the remembered import folder |
| `~/.local/share/kwsed/favorites.db` | favourites, tags, notes, ratings |
| `~/.local/share/kwsed/live-names.json` | names read from the unit |
| `~/.local/share/kwsed/cards.json` | HCard bank archive |
| `~/.local/share/kwsed/physical-cards.json` | physical card archive and labels |
| `~/.local/share/kwsed/banks/` | imported bank records |
| `~/.local/share/kwsed/backups/` | write-fence backup envelopes |

### Pre-audit staging area

Two hand-edited keys in `~/.config/kwsed/config.toml` reserve a staging area
the import screen's audition uses:

```toml
preaudit_target_perf = "RAM3:49"
preaudit_target_patches = "RAM3:27-34"
# preaudit_backup = false   # off: faster staging, no `z` for the patches
```

`preaudit_target_perf` names a single performance slot and
`preaudit_target_patches` a patch slot range (inclusive; a single slot is
valid). Both must point at a writable bank (RAM 1-3 or the Card). It is the
user's job to keep those slots free; the app still backs up whatever it
overwrites there. A malformed value or a ROM bank silently turns the feature
off. Before the dump the unit is switched to the configured pre-audit
performance slot (Performance mode, Bank Select, Program Change), so it
is on the slot the audition is meant for rather than whatever was current.
`preaudit_backup` defaults to true: what the staging slots hold is read
once per screen session and `z` puts it back. Setting it to false skips
those reads (and the per-patch backup envelopes) -- the audition is
quicker, but `z` no longer restores the staged patches. The staging bank
itself is read when the import screen opens, so the whole-bank fallback
does not wait for it during `t`; a unit that refuses the single-patch
write is remembered for the session and later auditions skip straight to
the bank dump.
A staged audition writes each needed patch on its own (one 426-byte
dump and an `EXECUTE_WRITE`, read back to prove it landed), so it costs a
fraction of a second per patch instead of a whole bank. Only the target's
sequence memory, when the plan needs wave sequences, is written as a whole
bank -- and when a unit does not take the single-patch write, the staging
falls back to one whole patch bank. Those whole-bank writes need **memory
protect off**; with protect on the unit refuses the dump. The ordinary
`ctrl+s` import needs no audition and is unaffected. A whole-bank import
into `RAM 3` overwrites the same patch slots named by
`preaudit_target_patches`; the import screen warns about them before it
writes.

### Multisets and multi-card staging

`m` reads the 32 Multisets; `e` edits the selected one's 16 channels. Silent
throughout until the final write. A channel's `b` accepts an ordinary bank or
a scoped card bank (`CARD/07`, `CARD/c1`). For a card performance, `s` stages
it into RAM — RAM 3 first, then the other writable banks — copying its
dependencies and remapping the channel, so one Multiset can hold sounds from
several cards at once. `a` then `w` writes the staged bank(s) *and* the Multi
Mode Setup as one fenced bundle; `z` restores all of it. The budgets and the
full staging rules are in [docs/BANK_BUILDER.md](docs/BANK_BUILDER.md).
`kwscli multi --all` includes empty Multisets.

---

## Command line

`kwscli` covers the same ground without the TUI. "No" in the unit column
means the command works from the local archives/cache.

| command | what it does | unit? | unit writes? |
|---|---|---|---|
| `banks` | the twelve-bank table | no | no |
| `list BANK KIND` | one bank's slots | no | no |
| `find TEXT` | search names | no | no |
| `ports` | MIDI ports on this host | no | no |
| `cards` | HCard archive; `--label` sets a local label, `--syx` writes one bank's names | no | no |
| `status` | identity and the unit's global channel | yes | no |
| `dump BANK [KIND]` | read one bank's names — silent | yes | no |
| `select BANK KIND N` | select a performance/Multiset (patches refused) | yes | no (sets mode) |
| `multi [--all]` | the 32 Multisets and their 16 channels — silent | yes | no |
| `deps BANK [KIND N]` | Performance → Patch → WaveSeq graph | no if cached | no |
| `import FILE` | file/disk → local bank cache | no | no |
| `copy-perf ...` | copy performances with dependencies | yes unless `--out` | with `--yes` |
| `copy-bank SRC DST` | copy a whole bank, all three kinds | yes | with `--yes` |
| `send FILE --to BANK` | write one bank dump | yes | with `--yes` |
| `backup list` | list backup envelopes | no | no |
| `backup show FILE` | print an envelope's metadata, blocks and annotations | no | no |
| `backup save` | read banks into backup envelopes with `--yes` | yes | no |
| `backup restore FILE` | write an envelope back | yes | with `--yes` |

```sh
kwscli banks
kwscli list RAM1 performance
kwscli find "glass"
kwscli status
kwscli dump CARD
kwscli multi --all
kwscli select RAM1 performance 12
kwscli deps RAM1 --source RAM1=dumps/ram1-*.syx
kwscli copy-perf CARD/07:3 CARD/13:11 --to RAM3 --out staged
kwscli copy-bank RAM1 CARD --yes
kwscli send staged-perf.syx --to RAM3 --yes
kwscli backup list
kwscli backup save --bank RAM1 --yes
kwscli cards --kind patch --syx hcard07.syx
```

`import` writes only the local cache; `copy-perf --out` writes only files.
The only unit writes are `send`, `copy-perf --yes`, `copy-bank --yes`,
`backup restore --yes` and the browser's armed write gate. `backup save`
defaults to all four writable banks and both kinds; `--bank` and `--kind`
narrow it. `cards --kind` chooses the names-only `.syx` and `--label` sets
its local archive label.

Common options: `--port`, `--recv-port`, `--scan` (re-probe ports, not a
bank scan), `--device-channel`, `--demo`, `--catalog`, `--live-names`.
`kwscli` additionally has `--channel` and `--cards`; the app's channel key is
`c`. The app adds `--config`, `--favorites` and `--allow-write`. Run
`kwscli --help` or `kwsed --help` for the rest.

---

## Importing banks and performances

`kwscli import` reads a whole bank into the local bank cache; the browser and
`copy-perf` then use it. `kwscli copy-perf --from-file` reads a file, disk or
archive as the source of one performance and its dependencies. Both use the
same readers; `--list` before choosing is cheap and needs no unit.

### From the browser

`I` opens a `ChoiceScreen` — **Whole bank** or **Single / multiple
performances**. Whole bank asks for a file or unit source; single/multiple
starts with a `FilePickerScreen`: one directory at a time, `..` to go up,
import files sorted first, `enter` opens a directory or file, `r` refreshes,
`esc`/`q` cancels. The start folder is remembered in `ui-state.toml` under
`[import] folder`.

**Whole bank** first opens a source chooser: a file, or any unit bank
(`RAM 1`–`RAM 3`, `ROM 4`–`ROM 11`, or the active `Card`). A file goes through
the file picker; a unit source is read once into a session snapshot and then
opens `WholeBankImportScreen`:

- it previews the source bank's active performances, patches and sequence
  bank;
- `s` switches source bank when the file holds several (ZIP, IMG, all-banks
  `.syx`);
- `b` chooses the destination (`RAM 1`, `RAM 2`, `RAM 3`, `Card` — the four
  physical writable banks; archived card banks are never targets);
- `m` cycles the import mode: as-is, self-contained 1:1, self-contained
  packed;
- `o` overflows what the target cannot hold, in three kinds: patches only
  (every performance stays on the target and the patches it names in other
  writable banks go to a RAM bank, repointed there), or a self-contained
  split (the performances that point outside the target move to the RAM bank
  with all their patches, so both banks are self-contained; when the split
  needs more banks than the one offered, the groups that fit are written and
  the rest wait for the next card), or cards only (no RAM bank at all: every
  performance is packed into self-contained groups that each fit one card,
  one group per pass, so the import visits card after card);
- `n` offers the next pass: for a split, switch the card on the unit (select
  it on the HCard and press C) and press n to write the next group onto that
  card; otherwise it offers the contained performances no earlier pass has
  written, into a fresh target;
- `x` offers the file's companion banks, each writing into the RAM bank the
  source's parts name;
- `w` arms the write gate; `c` saves to the local cache only; `ctrl+s` writes
  the whole bank to the unit in one fenced bundle;
- a write needs two approvals, not one: the write gate (`w`, or `--allow-write`)
  only says the session may write at all, while a per-plan approval names the
  exact physical banks and kinds that will be written and in what order. Token
  granularity follows the plan, not the wire: an overflow or split sequence is
  approved as one ordered plan — one token covers, for example, the overflow
  bank followed by its target, not one token per physical write — while a
  companion chain is answered one choice at a time and mints one token per
  answer, and each continuation pass (`n`) is its own approved request.
  `--allow-write` opens the broad gate, never a plan. Every bank in a plan is
  still separately read, backed up and verified;
- interior `PartRef`/`WaveRef` bank numbers are relocated to the destination
  bank automatically; ROM references stay in ROM;
- the reference rule is the same in all three modes: self-references move to
  the target bank; references to other writable banks stay where they point;
  ROM references stay in ROM, and no ROM patch or wave sequence is ever
  copied into a writable bank;
- every unit write reads the destination from the **unit** first: the cache
  is never treated as proof of what the bank holds. The bridge backs those
  frames up, then skips kinds that already match, so an unchanged bank is
  reported as "already matches — nothing was sent" rather than as a write.

**Affected performances.** A whole-bank write replaces every patch slot in each
bank it writes, so a performance that survives the write and points into a
written bank will play different sounds. Before anything is written, the app
asks one question: green field (clear the banks the import writes first —
performances, patches and wave sequences, each backed up before clearing; the
Card is never touched), decide with counts (read the banks the import does not
replace and count, per bank, the performances whose parts name a replaced patch,
then Init those slots or leave them), or leave as is (with a warning). Every
whole-bank write is asked, including a companion-only write (`x` or the
post-write popup's write), a Card target and a cards-only pass; green field is
offered only where a RAM bank would be cleared, and Init only where an
affected slot is known. When the companion preflight applies, it is asked
first and its answer is resolved before this question, so a cancel at either
has written nothing at all; on the answer the writes run remediation first,
then the confirmed companions, then the target. Green field clears only the
banks this import writes; RAM 3 and the Card can still hold performances
pointing at replaced patches, and their sound can still change. The Init
records are bundled with the app and nothing is read from a path at runtime.
Declining green field is the only time the unit is read for this question;
green field itself needs no read.

**Single / multiple performances** first asks for a source: a file, or any
unit bank (`RAM 1`–`RAM 3`, `ROM 4`–`ROM 11`, or the active `Card`). It then
asks for the target bank. `SingleImportScreen` shows the source performances
on the left and all 50 destination slots on the right, with a live budget:

```text
Space        assign the focused source to the first free/Init target slot
0-9          type the target slot directly into the table's target column
             two digits assign immediately; Enter commits one digit
Enter        commit a one-digit target (no dialog opens)
u / Backspace / Delete   unassign
A / U        assign all unassigned / clear all
p            toggle shared/fresh patches (fresh = the import gets its
             own patch copies, independent of the target bank)
t            audition the focused row (see below)
a            toggle auto audition: moving on a source row plays it
s            choose a source bank already in the session, or add another file
w            toggle the write gate
z            undo the last import or staging write (needs the armed gate)
ctrl+s       write the whole assignment in one batch
t            audition the focused row in the unit's edit buffer before
             anything is written; an unassigned row borrows the first free
             slot without committing an assignment. When the plan needs
             fresh patch copies they are staged into the configured
             pre-audit area (preaudit_target_perf / preaudit_target_patches);
             wave sequences are staged into the target bank's sequence
             memory as one fenced write, backed up and one `z` to undo.
             Without a configured staging area -> refused, use ctrl+s;
             staging writes need the armed write gate and are backed up;
             the staging slots are the user's to keep free. `a` turns on
             auto audition: the row the cursor lands on is played after a
             short pause, so arrow-key scrolling does not start a transfer
             per row. An import that would overwrite the configured
             pre-audit slots warns (red status line) before it writes.
```

The lower table has a `patch slots` column: it lists the destination patch
slots an existing performance uses, or the slots the planner will give an
assigned performance. Its `source` column shows the file or unit bank an
assigned performance came from. File paths are shortened to the file name and
its two parent folders (for example `.../Monotanz/Sounds/LIBRA18.SYX`); unit
banks show `RAM 2`, `ROM 7`, `Card 07`, and so on. Text is right-aligned and
left-trimmed, so the right-hand file name is never cut off when the terminal
is narrow. `Init` and free slots are available; occupied slots are overwritten
only when chosen. Each assignment is checked against the 50
performance, 35 patch, 32 sequence and 501 step budgets, so the screen refuses
an assignment before it can make a bank that does not fit. Pressing `p` toggles
whether byte-identical patches already in the target bank are reused (default)
or the import gets its own fresh patch copies; fresh copies keep the imported
performance independent when you later edit patches.

`s` opens the session's source-bank chooser. It lists every bank already added
and has **Add another source file…** and **Add unit bank (RAM/ROM/Card)…**.
The unit chooser lists RAM 1–3, ROM 4–11 and the active Card, and each bank is
read at most once per session. Assignments already made are kept when
switching or adding. Distinct banks with the same scope (two RAM 1 files, or
RAM 1 into RAM 1) are held under session aliases carrying their real bank
number, so one target bank can be assembled from several source files and unit
banks. The target is still single: many sources to one destination bank.

**Copying Card performances to RAM 3:** press `I`, choose **Single / multiple
performances**, choose **Card** (or the active `Card 07`), choose **RAM 3** as
the target, select the performances, press `w` to arm and `ctrl+s` to write.
For one performance, `y` does the same with that performance already assigned;
add more sources with `s` if needed.

**Separate SoundArtMedia-style files:** a package may split one bank into
`*_Performances.syx`, `*_Patches.syx` and `*_Waveseq.syx`. Add the performance
file first, then use `s` → **Add another source file…** for the patches and
waveseq files. Components for the same bank are merged into the source entry,
so the performance brings the matching patches and sequence data with it when
assigned.

When a performance-only file is selected, the app audits its used parts:
missing local patch slots and referenced wave sequences are reported in the
import screen. It also looks in the same directory for similarly named
`.syx`/`.MID`/`.bnk` companions. If exactly one candidate covers the missing
references (byte-identical `.MID`/`.syx` copies count as one), it is merged
automatically and the screen says so. Ambiguous or incomplete candidates are
listed as warnings instead of being guessed. References to RAM2/RAM3/ROM/Card
are treated as external and are not reported as missing from the file.

The app reads the destination bank once, the slot-preserving planner keeps
every untouched destination slot and every surviving shared/ROM reference, and
`ctrl+s` writes one fenced bundle with one undo step instead of one slow
single-performance operation per row. After the write, the target bank's names
and rows are refreshed from the bytes just written, without re-reading every
bank; `S` remains the full rescan and `r` re-reads only the current bank/kind.

The Import menu and the target-bank chooser bind both `q` and `Esc` and show
the legend `↑/↓ choose · Enter select · q / Esc back · ? help`.

`Esc`/`q` in the import flow goes back one step: single import → target-bank
chooser → source-bank chooser → file picker → Import menu; whole-bank import
goes back to the file picker. The Import menu's own `Esc` returns to the
browser.

`y` is now a shortcut into the same unified screen: it loads the selected
performance's source bank, asks for a target bank, and preassigns that
performance to the first free/Init slot. You can add more file/unit sources
with `s`, adjust the map, arm the gate and write one batch. The target write
is the same fenced bundle and one undo step. The CLI remains the scriptable
path; the browser `I`/`y` use the same readers, planner and cache.

### What each source supplies

| source | extensions | performances | patches | wave sequences | multisets | notes |
|---|---|---|---|---|---|---|
| SysEx | `.syx`, `.sysex` | yes | yes | yes | `0x62` only (read, not cached) | one type per file, all three in one file, single performance/patch, multi-bank, and All Data `0x50` (its RAM1/RAM2 banks) |
| Edisyn bank | `.syx` | yes | yes | yes | — | standard bank dumps; the same SysEx reader |
| Standard MIDI | `.mid`, `.midi` | yes | yes | yes | if present (read, not cached) | the SysEx the file carries, including the Korg header without its leading `F0` |
| SoundQuest / MidiQuest | `.sqs` | yes | yes | yes | if embedded (read, not cached) | embedded Korg SysEx; MidiQuest-native bodies are refused. MidiQuest `.mid` exports use the row above |
| SoundDiver | `.LIB` | yes | yes | yes | — | 1-, 2- or 4-oscillator patches; `RAM1-NN` slots; duplicate slots keep the last entry; multisets are absent from the examples |
| FAT12 floppy | `.img` | if contained | if contained | if contained | if contained (read, not cached) | reads the disk's `.syx`, `.mid` and `.bnk` files; `.arr`, docs and images are skipped |
| ZIP archive | `.zip` | if contained | if contained | if contained | if contained (read, not cached) | disk images plus loose `.syx`/`.mid`/`.bnk`; KryoFlux `.7z` members are skipped |
| `.bnk` bundle | `.bnk` | yes | yes | yes | if present (read, not cached) | an `FR`-prefixed or bare SysEx bundle |
| KryoFlux flux | `.7z` | — | — | — | — | **not decoded**; use the `.img` files in the same download |
| Korg Legacy VST | `.wsram` | — | — | — | — | **not decoded**; VST-native `WSra` bank, not hardware SysEx. Use the matching `.syx` performance/patch/waveseq files |
| Korg Legacy VST | `.fxb` | — | — | — | — | **not decoded**; VST preset/all-RAM state, not hardware SysEx |

`.wsram` is the Korg Legacy Collection **Wavestation VST** native RAM-bank
format, not a hardware SysEx dump. It begins with the ASCII tag `WSra`, then
contains VST-specific bank data; its performance/patch/waveseq areas are not
byte-identical to the hardware `.syx` files. Use the `*_Performances.syx`,
`*_Patches.syx` and `*_Waveseq.syx` files in the same SoundArtMedia package
instead. `.fxb` is the VST preset/all-RAM state and likewise unsupported.

All Data `0x50` is the one exception to "all three": the documented structure
it carries has RAM1 and RAM2 only. The `.LIB` multisets are genuinely absent
from the example libraries; an unknown entry type is reported, never guessed
at.

### Choosing a bank

- `--list` prints the labels. A `.syx` lists its performances; a `.LIB` lists
  its entries; an `.img`/`.zip` prints one compact line per bank with counts.
- `--scope RAM1` chooses the first bank with that scope. `--label "..."` chooses
  exactly one label from `--list` and is the reliable choice for a disk or a
  zip, where several banks legitimately share `RAM1`.
- `--as DEST` names the cache scope. It defaults to the source's own internal
  bank, and a card source may be filed under any `CARD/NN` slot. An internal
  source keeps its own scope because its parts and waves point at that bank;
  use `copy-perf` to move a performance to a different bank.
- `--bankstore DIR` overrides the cache directory. The default is
  `~/.local/share/kwsed/banks` on Linux.

### Import and copy options

| option | command | meaning |
|---|---|---|
| `FILE` | `import` | the source file; required |
| `--list` | `import` | print the banks, entries or disk/zip labels and exit |
| `--scope SCOPE` | `import` | first bank with this scope |
| `--label LABEL` | `import` | exact label from `--list`; combine with `--scope` if wanted |
| `--as DEST` | `import` | cache scope (default: the source's own internal scope) |
| `--bankstore DIR` | `import`, `deps`, `copy-perf` | cache directory |
| `SCOPE:NUMBER...` | `copy-perf` | performances to copy, in priority order |
| `--from-file FILE` | `copy-perf` | read the source from any format above; repeatable |
| `--source ID=GLOB` | `copy-perf`, `deps` | source bank dumps; repeatable |
| `--to BANK` | `copy-perf`, `send` | destination: `RAM1`, `RAM2`, `RAM3` or `CARD` |
| `--mode append|clean` | `copy-perf` | append to active performances, or replace the bank |
| `--out PREFIX` | `copy-perf` | write `<PREFIX>-perf/-patch/-seq.syx`; no unit is touched |
| `--yes` | `copy-perf`, `send`, `backup save`, `backup restore` | take the action; without it the fence/read stops first |
| `--backup-dir DIR` | `copy-perf`, `send`, `backup list/save/restore` | where the backup envelopes live |

`copy-perf` is the single-performance import. It puts the performance in the
first free slot, copies its patches privately, packs them contiguously with no
gap, and rewrites every part to the new patch slots. If the source is a
container with several banks of the same scope, import the chosen one first
with `--label` and then copy from the cache; `--from-file` keeps one source per
scope.

A SoundDiver `.LIB` brings its own RAM1 wave-sequence bank, so its
sequence-based performances copy too. A patch that points at a different
sequence bank still needs that bank supplied. Multisets are read for listing
but are not stored in the bank cache.

---

## Building a favourites bank

The instrument's programs form a graph: a performance names patches, a patch
names wave sequences, and a wave sequence is a chain of steps drawn from a
pool the whole bank shares. Copying a favourite between banks therefore
means rewriting references, not copying bytes -- which is why the
Wavestation's own whole-bank function is a move, not a copy.

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

**Performance first** is the other order: you name the performances and the
builder works out what they drag along.

```sh
python3 tools/build_bank.py --source A=dumps/cardA-*.syx --perf A:3 --perf A:7 --out favourites
```

Here every named Performance drags along its own private copies of the
patches it uses -- two performances that name the same source patch each
get their own, so editing one copy can never alter the other -- while the
wave sequences beneath them are still shared and packed once. See
[docs/BANK_BUILDER.md](docs/BANK_BUILDER.md).

`--bank` chooses the destination used for the interior bank references
(default `CARD`); `--channel` frames the output for the unit's global channel.
Omit `--out` for a dry run that reports the plan without writing files.

It writes `.syx` files and **never touches the instrument** -- loading them
is the machine's own business, through its front panel. See
[docs/BANK_BUILDER.md](docs/BANK_BUILDER.md).

---

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

---

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

---

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

---

## Edisyn

[Edisyn](https://github.com/eclab/edisyn) has Wavestation support and is
**Apache-2.0**, which is not compatible with this project's GPL-2.0. It was
read **only as a reference for interface facts** — the bank list, the nibble
encoding, the dump sizes — each of which is independently evidenced by dumps
this instrument produced. **No Edisyn code is used or derived here.** See
[LICENSE](LICENSE) and `docs/RESOLUTION_NOTES.md` §2.

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

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE) for the copyright notice and
third-party attributions, and [COPYING](COPYING) for the licence text.

Not affiliated with KORG INC.