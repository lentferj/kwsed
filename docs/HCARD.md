# HCard support for kwsed

Status: design, accepted 2026-11-07 · hardware policy for this work: **read-only**

## 1. Goal and differentiator

The Wavestation SR has one card slot. The Hypersynth **HCard-101** multiplexes
**40 banks** (00–39 in WS mode) behind that slot, selected physically on the
card itself. Sibling tools model *directly addressable* memory (p2ked's
`roms.py` handles not-installed ROM cards that are each selectable over Bank
Select). kwsed's differentiator is the harder case: a **multiplexer the synth
cannot address at all**, where "which bank is active" must be recovered from
a name field rather than a wire byte.

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
  echoes the other way. The card name is therefore the only identity signal
  the synth exposes for "which bank is active".
- Banks 16–18 require Korg **WSC-01/02/03** PCM cards; a patch referencing a
  missing PCM wave plays the wrong sound silently.
- The HCard has **per-bank write protection**, invisible to the synth.

## 3. Core model — three layers

1. **Static table** `kws/cards.py` — the 40 banks, transcribed from the manual
   (number, default name, category, PCM dependencies). Mirrors `p2k/roms.py`.
2. **Per-bank archive** `cards.json` beside the favourites DB — for each bank:
   card name, performance names, patch names, captured-at. Mirrors the
   livenames sidecar.
3. **Identity resolution** at read time — map the live card (name, first
   performance/patch names) to a bank number.

The physical `CARD` slot stays `CARD` for writes, favourites and backups. The
40 banks are a **browse/archive overlay** over it (Decision 8-B1), not a new
address space.

## 4. Decisions

- **3-A — read the card name from the System Setup dump (`0x51`).** Send
  `SYSTEM_SETUP_REQUEST (0x0E)`, parse the card-name field. The exact
  offset/length is the one open wire fact; see §7. Fallbacks, in order:
  fingerprint (`performance[0].name` + `patch[0].name`) when the name is
  absent/ambiguous, and a manual "I'm on bank N" override as a last resort.
- **4-A1 — expand the Card entry into a sub-list.** The left pane's `Card`
  row becomes a group of 40 sub-banks, each 50 performances + 35 patches.
  Unread banks show the table's default name greyed as "not captured"; read
  banks show live names with the existing disagreement `*` marker.
- **8-B1 — keep `CARD` as the identity key.** Favourites/livenames/catalog
  keys stay kind-qualified `CARD:performance:NNN`; archive entries are
  addressed by `(bank_number, kind, slot)`. No key migration.

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
as `vinsynlib.livenames` does. Captured names are keyed by slot number; the
card-name field is separate from the slot names. `digest` is the SHA-256 hex
hash of the bank's decoded performance and patch records
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
   stored name — kept for when a name is ever supplied (Dynamic Update
   echoing a bank's name into the WS, or a user label).
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
NN on the HCard first" prompt — the card cannot be switched remotely. The
sweep (`X`) walks the same path: it confirms each expected bank in turn and
stops between banks when what answered is not the bank it was asked for, so
the user can switch the HCard to the next bank.

Limits, stated in the UI rather than worked around: two banks captured with
identical content are indistinguishable — `resolve_by_digest` refuses to
guess when more than one digest matches — a bank never captured cannot be
identified, and a bank edited on the unit no longer matches its stored
digest.

## 7. Hardware confirmation (read-only)

The card-name field in the `0x51` dump is the only unknown. A read-only probe
(`tools/probe_card_name.py`) dumps the System Setup bytes so the offset can be
confirmed against the real unit. Until confirmed, `read_card_name()` returns
`None` gracefully and the constants are marked `# CONFIRM AGAINST HARDWARE`.
Read verification is permitted this session; **no writes to any slot,
including the card** — write-path verification is deferred to a separate
session.

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
- **P1** — card-name read + resolution + card sweep (identity mechanism).
- **P2** — PCM awareness, `kwscli cards` verb, send-to-card hint.
