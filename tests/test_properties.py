# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kws.messages import NAME_LEN, decode_name, encode_name, from_nibbles, to_nibbles
from kws.records import (
    PART_BASE,
    PART_SIZE,
    PARTS_PER_PERFORMANCE,
    PERFORMANCE_SIZE,
    performance_parts,
    remap_performance_banks,
    set_performance_part,
)

# --- messages.py -------------------------------------------------------------


@given(st.binary())
def test_nibble_roundtrip(data):
    """from_nibbles(to_nibbles(data)) == data"""
    assert from_nibbles(to_nibbles(data)) == data


@given(st.text(max_size=NAME_LEN))
def test_name_encoding_roundtrip(s):
    """decode_name(encode_name(s)) == s (modulo padding/truncation/filtering)"""
    encoded = encode_name(s)
    decoded = decode_name(encoded)

    # encode_name filters to 32-126, others become 32 (space)
    def filter_char(c):
        o = ord(c)
        return c if 32 <= o <= 126 else " "

    expected = "".join(filter_char(c) for c in s).rstrip()
    assert decoded == expected


@given(st.text(min_size=NAME_LEN + 1))
def test_name_encoding_truncation(s):
    """encode_name(s) truncates to NAME_LEN"""
    encoded = encode_name(s)
    assert len(encoded) == NAME_LEN

    def filter_char(c):
        o = ord(c)
        return c if 32 <= o <= 126 else " "

    expected = "".join(filter_char(c) for c in s[:NAME_LEN]).rstrip()
    assert decode_name(encoded) == expected


# --- records.py --------------------------------------------------------------


@st.composite
def performance_record(draw):
    """Generates a binary performance record that won't crash the decoder."""
    data = bytearray(
        draw(st.binary(min_size=PERFORMANCE_SIZE, max_size=PERFORMANCE_SIZE))
    )
    # Ensure the bank bits are valid (0-11 in exp_bank)
    for i in range(PARTS_PER_PERFORMANCE):
        at = PART_BASE + i * PART_SIZE
        # exp_bank = (data[at] & 3) + ((data[at+4] >> 6) & 3) * 4
        # We need this sum to be < 12.
        # Simplest: keep the high bits 0-2 (so max 2*4 + 3 = 11)
        data[at + 4] = (data[at + 4] & 0x3F) | (draw(st.integers(0, 2)) << 6)
    return bytes(data)


@st.composite
def valid_performance_bank(draw, num_records=1):
    records = [draw(performance_record()) for _ in range(num_records)]
    return b"".join(records)


@given(
    valid_performance_bank(num_records=2),
    st.integers(min_value=0, max_value=11),
    st.integers(min_value=0, max_value=11),
)
def test_remap_performance_banks_invariants(bank_data, source, dest):
    if source == dest:
        with pytest.raises(
            ValueError, match="source and destination are the same bank"
        ):
            remap_performance_banks(bank_data, source, dest)
        return

    remapped = remap_performance_banks(bank_data, source, dest)

    # Invariant 1: No part in the remapped bank points to 'source'
    for base in range(0, len(remapped), PERFORMANCE_SIZE):
        record = remapped[base : base + PERFORMANCE_SIZE]
        for part in performance_parts(record):
            assert part.bank != source

    # Invariant 2: Remapping the same bank again to a different destination does nothing
    dest2 = (dest + 1) % 12
    if dest2 == source:
        dest2 = (dest2 + 1) % 12

    re_remapped = remap_performance_banks(remapped, source, dest2)
    assert re_remapped == remapped


@given(
    valid_performance_bank(num_records=1),
    st.integers(min_value=0, max_value=11),
    st.integers(min_value=0, max_value=11),
)
def test_remap_nonexistent_bank(bank_data, source, dest):
    """Remapping a bank that isn't referenced should change nothing."""
    if source == dest:
        return

    # Ensure 'source' is not in the bank
    record = bytearray(bank_data)
    for part in performance_parts(record):
        if part.bank == source:
            # Change it to something else
            other_bank = (source + 1) % 12
            if other_bank == dest:
                other_bank = (dest + 1) % 12
            set_performance_part(record, part.index, bank=other_bank, patch=part.patch)

    clean_bank = bytes(record)
    remapped = remap_performance_banks(clean_bank, source, dest)
    assert remapped == clean_bank
