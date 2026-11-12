# HCard support for kwsed

Status: design, accepted 2026-11-07 · hardware policy for this work: **read-only**

> Card modes (`none` / `cards` / `hcard`), the physical-card archive and
> per-card favourites are covered in [CARDS.md](CARDS.md).

## 1. Goal and differentiator

The Wavestation SR has one card slot. The Hypersynth **HCard-101** multiplexes
**40 banks** (00–39 in WS mode) behind that slot, selected physically on the
card itself. Sibling tools model *directly addressable* memory (p2ked's
`roms.py` handles not-installed ROM cards that are each selectable over Bank
Select). kwsed's differentiator is the harder case: a **multiplexer the synth
cannot address at all**, where "which bank is active" must be recovered from
the bank's contents rather than a wire byte.

The goal: make those 40 banks — the preloaded Korg ROM cards, factory
preloads and the user banks — **available in the preset browser**, browsable
by name, even though only one is physically present to the synth at a time.

## 2. Background facts (HCard manual, WS mode)

- WS bank range **00–39**; 00 = WS Internal ROM, 01–04 factory preloads,
  05–24 Korg ROM cards (WPC-11…20ex, WPC-J01, WPC-1S/2S/3S needing
  WSC-01/02/03, MCR03-WS*, MCR03 "Vector World"), 25–39 user banks.
- **Only one bank is visible at a time**; switching is a physical action on
  the HCard (touch pads / ▲▼). No remote bank-switch is documented.
- **Dynamic Update**: selecting a bank on the HCard rewrites the Wavestation's
  *card name* to that bank's name. Renaming on the WS (Global › UTIL › NAME)
  echoes the other way. The name is visible on the panel but **not readable
  over MIDI**: the `0x51` dump carries no name (measured 2026-11-12, §7), so
  identity comes from content, not from this name.
- Banks 16–18 require Korg **WSC-01/02/03** PCM cards; a patch referencing a
  missing PCM wave plays the wrong sound silently.
- The HCard has **per-bank write protection**, invisible to the synth.

## 3. Core model — three layers

1. **Static table** `kws/cards.py` — the 40 banks, transcribed from the manual
   (number, default name, category, PCM dependencies). Mirrors `p2k/roms.py`.
2. **Per-bank archive** `cards.json` beside the favourites DB — for each bank:
   local label, performance names, patch names, captured-at. Mirrors the
   livenames sidecar.
3. **Identity resolution** at read time — map the live card (content digest,
   first performance/patch names) to a bank number.

The physical `CARD` slot stays `CARD` for writes and backups. The 40 banks
are a **browse/archive overlay** over it, not a new wire address space.
Favourites and live names are scoped per HCard bank under `CARD/NN`
(`cards` mode uses `CARD/cN` for physical cards); see
[CARDS.md](CARDS.md). Decision 8-B1's shared `CARD` identity for
favourites/livenames is **superseded**.

## 4. Decisions

- **3-A — read the card name from the System Setup dump (`0x51`).
  Superseded 2026-11-12: there is no name in that dump.** The probe found
  the documented 34-field `system` structure, so the read was removed
  (§7). Identity is the content digest first, then the fingerprint
  (`performance[0].name` + `patch[0].name`), and a manual "I'm on bank N"
  override as a last resort.
- **4-A1 — expand the Card entry into a sub-list.** The left pane's `Card`
  row becomes a group of 40 sub-banks, each 50 performances + 35 patches.
  Unread banks show the table's default name greyed as "not captured"; read
  banks show live names with the existing disagreement `*` marker.
- **8-B1 — superseded: keep `CARD` as the identity key.** The original
  decision kept favourites/livenames/catalog keys kind-qualified as
  `CARD:performance:NNN`, with archive entries addressed by
  `(bank_number, kind, slot)`. Replaced: writes and backups still target the
  physical `CARD` slot, but favourites and live names are scoped per HCard
  bank under `CARD/NN` (`kws.cards.scope_id`; `kwsed.app._scoped_bank_id`),
  and `cards` mode scopes physical cards under `CARD/cN`
  (`kws.physical.scope_id`). Archive entries remain `(bank_number, kind, slot)`
  in `cards.json`; the HCard and physical-card archives are separate. See
  [CARDS.md](CARDS.md).

## 5. Data model (`cards.json`)

```json
{
  "version": 1,
  "banks": {
    "07": {
      "name": "WPC-13 \"Performance 3\"",
      "captured_at": "2026-11-07T…",
      "performances": {"0": "…", "…": "…"},
      "patches": {"0": "…", "…": "…"},
      "digest": "<sha256 hex>",
      "verified": true
    }
  }
}
```

Atomic write, corrupt-file tolerance and a merge (not replace) save, exactly
as `vinsynlib.livenames` does. Captured names are keyed by slot number.
`label` is the user's local name; `name` would hold a name the card
reported, and is empty because the card cannot report one. `digest` is the
SHA-256 hex hash of the bank's decoded performance and patch records
(`cards.content_digest`); `verified` says the capture was **confirmed by
content evidence** — a digest that matched — rather than only asserted by the
bank row it was captured under.

## 6. Identity resolution

Neither the card name (item 23 in `TODO.md`) nor which bank is active can be
read over MIDI — the SR's `CURRENT_BANK`/`CURRENT_PROG` are write-only in the
documented SysEx, and the HCard itself has no MIDI. Identity is therefore
resolved from content, strongest evidence first.
`resolve(card_name, perf0, patch0, digest=…) -> bank | None` in
`kws/cards.py`, in the order actually implemented:

1. **Content digest match** — the SHA-256 of the read's decoded performance
   and patch records against every captured bank's stored digest
   (`resolve_by_digest`). One match identifies the bank outright; the digest
   never needed the name we cannot read.
2. **Exact card-name match** against the static table or a captured bank's
   stored name — kept for a caller that supplies a name; the app passes
   `None` because the card cannot report one.
3. **First-slot-name fingerprint** — `performances[0]`/`patches[0]` against
   previously captured banks; also narrows a name that matched more than one
   bank.
4. **Ambiguous or unknown → `None`**; the caller offers "new / user bank"
   filing rather than guessing.

### The active-bank flow

Captures resolve against the card row the user has highlighted (decision
4-A1): `C` on a card sub-bank row reads the live card, digests it, and files
the capture against the evidence:

- The digest matches the highlighted row's stored digest → the capture is
  **confirmed** and marked `verified`.
- The digest matches a *different* captured bank → the card is on that bank
  (or the two are identical, which the digest cannot separate); the capture
  files under the evidence bank and the status says to switch the HCard back
  to the bank the user meant.
- No evidence at all — a first capture of that bank — files under the
  asserted row number **unverified**, on the user's say-so, with a prompt to
  select the bank on the HCard and capture again.

The last confirmed bank is remembered as the **active** bank and shown in the
detail line ("active; last confirmed capture …"). Selecting a slot from a
card row that is **not** the known-active bank is refused with a "select Card
NN on the HCard first, then press C" prompt — the card cannot be switched
remotely, and a capture is the only way to confirm the new bank. The
sweep (`X`) walks the same path: it confirms each expected bank in turn and
stops between banks when what answered is not the bank it was asked for, so
the user can switch the HCard to the next bank.

Limits, stated in the UI rather than worked around: two banks captured with
identical content are indistinguishable — `resolve_by_digest` refuses to
guess when more than one digest matches — a bank never captured cannot be
identified, and a bank edited on the unit no longer matches its stored
digest.

## 7. Hardware confirmation (read-only)

The card name was long assumed to sit at some offset in the `0x51` dump; only
the offset was thought unknown. A read-only probe was run on a bench SR
(2026-11-12, global channel 5): the dump is the documented 34-field `system`
structure — small parameter values, not ASCII — and sending a Parameter
Change to set the card name did not change it. **There is no name in the
`0x51` dump.** `read_card_name()` and its offset constants were removed;
identity is the content digest. Read verification is permitted this session;
**no writes to any slot, including the card** — write-path verification is
deferred to a separate session.

## 8. Write path (unchanged fence, deferred verification)

Writing *to* an HCard bank is the existing `write_bank(CARD, …)` fence
(ROM refusal, pre-write backup, read-back verify). Two HCard notes to surface,
not re-implement: per-bank write protection (a protected bank makes the
read-back verify fail — hint the user), and the copy-vs-MOVE reference trap
already handled by `bankbuild`.

## 9. PCM awareness

Banks 16–18 carry a `needs_pcm = "WSC-0n"` flag, rendered in the detail line
like the ROM `*` marker ("needs WSC-01"), mirroring p2ked's not-installed
sentinel concept.

## 10. Phasing

- **P0** — table + archive + read-one-bank capture (the useful core).
- **P1** — content-digest resolution + card sweep (identity mechanism).
- **P2** — PCM awareness, `kwscli cards` verb, send-to-card hint.
