# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors

from __future__ import annotations

from typing import Any, Dict

import pytest

from kws import banks, deps
from kwsed import import_source as isrc


def _source(bank_id: str = "RAM1", bank_number: int | None = None) -> deps.Source:
    return deps.Source(
        bank_id=bank_id,
        performances=[b"\x00"],
        patches=[b"\x01"],
        waveseq=b"\x02",
        bank_number=bank_number,
    )


def test_make_source_entry_same_scope_alias() -> None:
    src = _source(bank_number=None)
    existing: Dict[str, Any] = {"RAM1": object()}
    entry = isrc.make_source_entry(
        label="RAM 1", scope="RAM1", source=src, existing=existing
    )
    assert entry.scope == "RAM1#2"
    assert entry.source.bank_id == "RAM1#2"
    assert entry.source.bank_number == 0
    assert entry.label == "RAM 1 (alias)"


def test_make_source_entry_no_collision_keeps_scope() -> None:
    src = _source()
    entry = isrc.make_source_entry(
        label="RAM 1", scope="RAM1", source=src, origin="/home/me/x.syx"
    )
    assert entry.scope == "RAM1"
    assert entry.source.bank_id == "RAM1"
    assert entry.label == "RAM 1"
    assert entry.origin == "/home/me/x.syx"
    assert entry.source_path == "/home/me/x.syx"
    assert entry.source_origin == "/home/me/x.syx"


def test_make_source_entry_alias_chain() -> None:
    src = _source()
    existing: Dict[str, Any] = {"RAM1": object(), "RAM1#2": object()}
    entry = isrc.make_source_entry(
        label="RAM 1", scope="RAM1", source=src, existing=existing
    )
    assert entry.scope == "RAM1#3"


def test_unit_bank_choices() -> None:
    choices = isrc.unit_bank_choices()
    assert len(choices) == 12
    # RAM 1-3 writable
    for c in choices[:3]:
        assert c.read_only is False
    # ROM 4-11 read only
    for c in choices[3:11]:
        assert c.read_only is True
    card = choices[11]
    assert card.label == "Card"
    assert card.origin == "Card"
    assert card.scope == "CARD"


def test_origin_for_unit_card_slash() -> None:
    assert isrc.origin_for_unit("CARD/07") == "Card 07"


def test_compact_origin_examples() -> None:
    assert isrc.compact_origin("/a/b/c/d.syx") == ".../b/c/d.syx"
    # windows backslashes
    assert isrc.compact_origin("C:\\a\\b\\c\\d.syx") == ".../b/c/d.syx"
    # short unit labels unchanged
    assert isrc.compact_origin("RAM 2") == "RAM 2"
    assert isrc.compact_origin("Card 07") == "Card 07"


def test_input_source_not_mutated() -> None:
    src = _source(bank_number=5)
    before = (
        src.bank_id,
        list(src.performances),
        list(src.patches),
        src.waveseq,
        src.bank_number,
    )
    isrc.make_source_entry(
        label="x", scope="RAM1", source=src, existing={"RAM1": object()}
    )
    after = (
        src.bank_id,
        list(src.performances),
        list(src.patches),
        src.waveseq,
        src.bank_number,
    )
    assert before == after


def test_source_bank_number() -> None:
    assert isrc.source_bank_number("CARD") == banks.bank("CARD").number
    assert isrc.source_bank_number("CARD/07") == banks.bank("CARD").number
    assert isrc.source_bank_number("RAM1") == banks.bank("RAM1").number
    assert isrc.source_bank_number("NOPE") is None


def test_source_entry_existing_mapping_source_or_entry() -> None:
    src = _source()
    # keys matter only
    existing: Dict[str, Any] = {"RAM1": src}
    entry = isrc.make_source_entry(
        label="RAM 1", scope="RAM1", source=src, existing=existing
    )
    assert entry.scope == "RAM1#2"
