# Cards: modes, identity and per-card state

Status: design accepted 2026-11-09 · replaces the single-mode assumption

## Config (`~/.config/kwsed/config.toml`)

```toml
# "none" | "cards" | "hcard"   (default: cards)
card_mode = "cards"

# hcard mode only: show the manual's pre-set bank names before capture
show_card_defaults = false
```

- `none` — no Card rows; the card actions refuse.
- `cards` — one or several ordinary Program Cards, identified by content.
- `hcard` — the HCard's forty banks.

## Identity

The card name is unreadable over MIDI (`0x51` carries no name and there is
no parameter-read request). A physical card is therefore identified by the
**SHA-256 of its decoded performance and patch dumps** — the same content
digest the HCard banks use.

- A capture reads the inserted card, hashes it, and looks it up.
- A match makes that archived card active.
- No match opens a **picker**: the archived cards plus "New card". Picking
  an existing one adopts the new content (digest updated, id/label/
  favourites kept); "New card" asks for a label and creates an entry.
- Two cards with identical content are indistinguishable; the picker is
  the answer there.

## Archive (`physical-cards.json`, beside the favourites database)

```json
{
  "version": 1,
  "active": "c2",
  "cards": {
    "c1": {
      "label": "Orchestra card",
      "digest": "<sha256 hex>",
      "captured_at": "2026-11-09T…",
      "verified": true,
      "performances": {"0": "…"},
      "patches": {"0": "…"}
    }
  }
}
```

`id` (`c1`, `c2`, …) is stable and never changes; `label` is the display
name; `verified` says the last capture matched the stored digest rather
than being adopted through the picker. The HCard archive (`cards.json`)
is separate and unchanged.

## Per-card state

- **Names** are stored in the per-card record; the browser shows the
  archive, not the shared catalog `CARD` layer.
- **Favourites** are scoped by a per-card bank id:
  - physical card `c1` → `CARD/c1`, favourite key `CARD/c1:<kind>`
  - HCard bank `07` → `CARD/07`, favourite key `CARD/07:<kind>`

  The old shared `CARD` favourites are deliberately **discarded** (left in
  the database but never read); per-card keys are a new space.
- The **active** card is the last one captured/identified. `⏎` auditions
  only from the active card, because the synth can only play what is in the
  slot; the archive rows of other cards are read-only until inserted.

## Whole-bank copy into a Card

`B` in the browser copies the bank under the cursor — wave sequences,
patches and performances — into another writable bank. The performances'
references to the source bank are rewritten to the destination, so the
copy plays its own patches rather than the source's. The destination is
backed up first and every kind is read back and verified. The copy
window's `m` can use the unit's own MOVE as the transfer instead, with
kwsed's backup and read-back verify around it (the destination is still
auto-adopted the same way).

A verified write into the Card is **adopted automatically**: the active
card's archive record keeps its identity (number, name, label, capture
time) while its names, digest and `verified` flag are recomputed from what
was just written. `hcard` mode updates the active bank in `cards.json`;
`cards` mode updates the active card in `physical-cards.json`. A RAM
destination touches no archive.

`z` restores the destination's previous content — all three kinds — from
the single backup envelope the copy wrote. In `hcard` mode the active bank
must be a user bank (25-39): HCard ROM banks (0-24) are refused as either
source or destination. `card_mode = none` refuses when the Card is either
end.

## Browser

- `cards`: the archived cards appear as rows (one per card per kind), with
  the active one marked `•`; `C` captures the inserted card, `L` labels the
  selected one. Empty archive: one placeholder row saying to insert a card
  and press `C`.
- `hcard`: the forty banks as today; `show_card_defaults` renders the
  manual's expected name (dimmed) for uncaptured banks instead of
  `not captured`.
- `none`: no Card rows.

## Migration

The HCard `cards.json` is untouched. Favourites filed under the old shared
`CARD` key are no longer read; the per-bank/per-card keys start empty.
