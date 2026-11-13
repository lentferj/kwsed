# The Multi catalog: what a Multiset pointed at

Status: implemented 2026-11-13.

## Why it exists

A Multiset part stores only a **bank byte and a performance number**. For the
Card that names the *slot*, not which card or HCard bank is in it. The unit
cannot say which bank it is playing from, so:

* a Multiset written against HCard bank 10 plays *whatever bank is in the
  slot* after a swap — often not the sound it was built for;
* a RAM slot is just a position: overwrite it and the Multiset still points
  there, under a different name.

The device has nowhere to store the missing information. kwsed records it
instead, and compares it on every load.

## What is stored

`multisets.json`, beside the favourites database:

```json
{
  "version": 1,
  "multisets": {
    "00": {
      "name": "Set 00",
      "recorded_at": "2026-11-13T12:00:00+00:00",
      "digest": "<sha256 of the 86-byte record>",
      "channels": {
        "00": {
          "bank": 3,
          "performance": 5,
          "name": "Warm Pad",
          "card_scope": "CARD/39",
          "card_digest": "9bc13d32…"
        },
        "03": {
          "bank": 0,
          "performance": 12,
          "name": "Bass",
          "card_scope": "",
          "card_digest": ""
        }
      }
    }
  }
}
```

Only used channels are recorded. `card_scope` is `CARD/39` for an HCard bank,
`CARD/c1` for a physical card, empty for a non-card channel or when the bank
was unknown. `card_digest` is the card archive's content digest, which
identifies the bank even after a rename or a factory reset.

## When it is recorded

* **Automatically** when kwsed writes a Multiset. The snapshot is the state
  on screen at the moment of the write, with the card scope from the
  channel's chosen bank (or the active/viewed card when none was chosen).
* **`r` in the Multi editor** records the current bindings **without sending
  anything**. This is how Multisets that already exist on the unit enter the
  catalog.
* The same record travels in the **backup envelope** (`write_multi` stores it
  as the `annotations` block beside the SysEx dump), so a restore after a
  factory reset or a dead battery still says which card to insert.

## What a load says

After `m` reads the 32 Multisets, each recorded one is compared with the
device:

* a card channel whose recorded scope is not the active card, and whose
  digest does not match it, produces
  `expects CARD/39 (Warm Pad); the active card is CARD/20` (or `unknown`);
* a RAM channel whose recorded name differs from the name now at that slot
  produces `expects RAM 1 12 'Bass'; it now holds 'Other'`.

The Multi's row gets a `!`, a status line summarises how many channels no
longer match, and the editor's status line names the first mismatches. An
uncached RAM bank or an unknown card is **not** evidence of a change and is
silent; read the bank (`r`/`S`) if you want the RAM check.

The check also re-runs by itself after `C` captures a different Card bank,
without a fresh Multi read: the RAM setup has not moved, but the card the
Multiset expects is no longer the one in the slot. The affected rows turn
red immediately and the status after the capture says how many channels
changed.

## One slot, several cards

A Multiset can record channels that mean different card banks (for example
channel 4 from Card 24 and channels 5-6 from Card 25). This is archive
metadata: the Wavestation has one CARD slot and stores only performance
numbers, so at play time every card channel uses whichever bank is active.
The editor says `MIXED CARDS`, the row is marked `mixed`, and `w` asks for
confirmation before writing such a Multiset. It does **not** collapse the
recorded scopes onto the active card: an untouched channel keeps the card
it was recorded against.

To make a mixed Multiset play as intended, press `s` first. Staging copies
each channel's performance from its recorded card into RAM, rewrites the
Multiset to point at those RAM slots, and then `w` is safe. Writing without
staging keeps the intent in the catalog but the unit will play the active
card; the channels that no longer match stay red.

## Using the backup to rebuild

```sh
kwscli backup show backups/multi_Multi_Mode_Setup_….json
```

prints the envelope's blocks and the annotations: which channel wanted which
card bank and performance, and the card digest. After a reset, restore the
Multi Mode Setup, then insert the card whose digest matches; the app confirms
it on the next capture or load.

## Limits, stated plainly

* Metadata only. It never goes to the unit; the device always stores just the
  slot.
* It records what the app believed at the time. An unknown bank is recorded as
  unknown rather than guessed.
* A card with the same content under another bank is not a mismatch: the
  digest is the identity, and the dependency data is already available.
* The live check is only as good as the read data: RAM names come from the
  cache/catalog, and the active card from the last capture/selection.

## Where it lives

* `kws/multisets.py` — the record, archive, digest and comparison rules.
* `kwsed/app.py` — recording on write, `r` in the Multi editor, the check
  after `m`, and the row/status annotations.
* `kwsed/cli.py` — `backup show`.
