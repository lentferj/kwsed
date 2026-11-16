# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY OR FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""Setting performance slots to Init without disturbing the rest of a bank.

The source bank is built in the test: distinct, non-Init records with legible
names. Nothing here opens a file or a MIDI port.
"""

import pytest

from kws import banks, dumps, initbank, remediation

_RECORD = len(initbank.INIT_PERFORMANCE)
_BANK_SIZE = _RECORD * banks.PERFORMANCES_PER_BANK


def _source() -> bytes:
    """A whole bank of distinct, non-Init records named ``Slot00`` and so on."""
    records = []
    for index in range(banks.PERFORMANCES_PER_BANK):
        name = f"Slot{index:02d}".encode("ascii")
        records.append(name + bytes(_RECORD - len(name)))
    return b"".join(records)


def _records(bank: bytes) -> tuple[bytes, ...]:
    """Split ``bank`` into its fixed-size performance records."""
    return tuple(bank[at : at + _RECORD] for at in range(0, len(bank), _RECORD))


def test_untouched_slots_are_byte_identical() -> None:
    source = _source()
    result = remediation.init_performances_in(source, {3, 17})
    before = _records(source)
    after = _records(result)
    assert len(after) == banks.PERFORMANCES_PER_BANK
    for index in range(banks.PERFORMANCES_PER_BANK):
        if index in (3, 17):
            continue
        assert after[index] == before[index]
        assert after[index] != initbank.INIT_PERFORMANCE


def test_only_the_listed_slots_change_and_become_init() -> None:
    source = _source()
    after = _records(remediation.init_performances_in(source, {3, 17}))
    assert after[3] == initbank.INIT_PERFORMANCE
    assert after[17] == initbank.INIT_PERFORMANCE
    assert after[2] == _records(source)[2]
    assert after[18] == _records(source)[18]


def test_a_slot_that_was_not_init_becomes_init() -> None:
    source = _source()
    result = remediation.init_performances_in(source, {7})
    assert dumps.record_name(_records(source)[7]) != "Init"
    assert dumps.record_name(_records(result)[7]) == "Init"
    assert _records(result)[7] == initbank.INIT_PERFORMANCE


def test_a_slot_already_init_stays_init() -> None:
    source = initbank.init_performances()
    result = remediation.init_performances_in(source, {0, 49})
    assert result == source


def test_slots_are_deduplicated_and_order_insensitive() -> None:
    source = _source()
    once = remediation.init_performances_in(source, {3, 17})
    twice = remediation.init_performances_in(source, [17, 3, 3, 17])
    assert once == twice


def test_slots_may_be_any_iterable() -> None:
    source = _source()
    result = remediation.init_performances_in(source, (slot for slot in (5, 9)))
    assert _records(result)[5] == initbank.INIT_PERFORMANCE
    assert _records(result)[9] == initbank.INIT_PERFORMANCE


def test_empty_slots_returns_the_input_unchanged() -> None:
    source = _source()
    assert remediation.init_performances_in(source, ()) == source
    assert remediation.init_performances_in(source, set()) == source


def test_all_slots_equal_init_performances() -> None:
    source = _source()
    slots = range(banks.PERFORMANCES_PER_BANK)
    assert remediation.init_performances_in(source, slots) == (
        initbank.init_performances()
    )


def test_wrong_length_is_refused_and_names_the_expected_size() -> None:
    for bad in (b"", bytes(_BANK_SIZE - 1), bytes(_BANK_SIZE + 1)):
        with pytest.raises(ValueError, match=str(_BANK_SIZE)):
            remediation.init_performances_in(bad, {0})


def test_out_of_range_slots_are_refused() -> None:
    source = _source()
    for bad in (-1, banks.PERFORMANCES_PER_BANK, 99):
        with pytest.raises(ValueError, match="outside"):
            remediation.init_performances_in(source, {bad})


def test_cleared_bank_delegates_to_initbank() -> None:
    performances, patches, waveseq = remediation.cleared_bank()
    assert (performances, patches, waveseq) == initbank.cleared_bank()
    assert (len(performances), len(patches), len(waveseq)) == (9050, 14910, 8784)
