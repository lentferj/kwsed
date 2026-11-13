from __future__ import annotations

import random

from kws import multi
from kws.fx import FxBlock
from kws.multi import Part


def _make_parts():
    parts = []
    for ch in range(1, 17):
        parts.append(Part(channel=ch, bank=0, performance=50, level=0, flag=0))
    return tuple(parts)


def _synthetic_dump(seed: int = 1) -> bytes:
    rng = random.Random(seed)  # noqa: S311
    needed = multi.NAME_BASE + multi.MULTISETS * multi.NAME_SIZE
    data = bytearray(needed)
    for number in range(multi.MULTISETS):
        base = number * multi.MULTISET_SIZE
        data[base] = rng.randrange(16)
        if number % 3 == 0:
            fx = bytes(21)
        else:
            fx = bytes(rng.randrange(256) for _ in range(21))
        data[base + 1 : base + 22] = fx
        for channel in range(multi.CHANNELS):
            at = base + multi.PART_BASE + channel * multi.PART_SIZE
            data[at] = rng.randrange(4)
            data[at + 1] = rng.randrange(50)
            data[at + 2] = rng.randrange(128)
            data[at + 3] = rng.randrange(2)
        name = f"Set{number:02d}".encode()
        at = multi.NAME_BASE + number * multi.NAME_SIZE
        data[at : at + len(name)] = name
    return bytes(data)


def test_roundtrip_synthetic_dump():
    data = _synthetic_dump()
    sets = multi.parse(data)
    assert len(sets) == multi.MULTISETS
    out = multi.encode(sets)
    assert out == data


def test_all_zero_fx_block_roundtrips():
    data = bytearray(_synthetic_dump())
    data[1:22] = b"\x00" * 21
    data_bytes = bytes(data)
    sets = multi.parse(data_bytes)
    assert sets[0].fx.to_bytes() == b"\x00" * 21
    out = multi.encode(sets)
    assert out == data_bytes


def test_control_channel_and_fx_from_bytes():
    data = _synthetic_dump()
    sets = multi.parse(data)
    for number in range(multi.MULTISETS):
        base = number * multi.MULTISET_SIZE
        assert sets[number].control_channel == data[base]
        assert sets[number].fx.to_bytes() == data[base + 1 : base + 22]


def test_part_byte_change_does_not_alter_control_or_fx():
    data = bytearray(_synthetic_dump())
    original = bytes(data)
    base = 2 * multi.MULTISET_SIZE
    at = base + multi.PART_BASE + 0 * multi.PART_SIZE + 2
    data[at] = (data[at] + 1) % 128
    sets = multi.parse(bytes(data))
    orig_sets = multi.parse(original)
    assert sets[2].control_channel == orig_sets[2].control_channel
    assert sets[2].fx.to_bytes() == orig_sets[2].fx.to_bytes()


def test_default_construction():
    ms = multi.Multiset(number=0, name="X", parts=_make_parts())
    assert ms.control_channel == 0
    assert ms.fx == FxBlock.empty()


def test_a_real_control_byte_is_accepted_and_roundtrips():
    """Real dumps carry 0x19 (25), so the byte is not a plain 0-15 channel."""
    import pytest

    ms = multi.Multiset(number=0, name="X", parts=_make_parts(), control_channel=25)
    assert ms.control_channel == 25
    with pytest.raises(ValueError):
        multi.Multiset(number=0, name="X", parts=_make_parts(), control_channel=128)
