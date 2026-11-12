# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# The 21-byte effect block layout is transcribed from Edisyn's record
# builder (KorgWavestationPerformance.buildPerformance) as FACTS ONLY per
# docs/RESOLUTION_NOTES.md section 2: parameter numbers, names, ranges,
# storage offsets and bit layouts are facts; no code or comments were
# copied.  Hardware confirmation of the layout is step 0 of
# docs/FX_PLAN.md and has not yet been done.
#
# kwsed is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 2 of the License, or (at your
# option) any later version.
#
# kwsed is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""The 21-byte FX block: two processors, routing, mix and modulation.

Pure bytes and tables, no MIDI, no UI.  A performance carries this block at
bytes 16-36; a Multiset carries the same block at bytes 1-21.  Both share
this codec and these tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from kws.fx_tables import (
    FX_GROUP as _FX_GROUP_TABLE,
)
from kws.fx_tables import (
    FX_INDICES,
    FX_PARAM_LABELS,
    FX_PCL_LIST,
)

__all__ = [
    "FX_SIZE",
    "EffectType",
    "EffectParam",
    "Effect",
    "FxBlock",
]

FX_SIZE = 21

# ---------------------------------------------------------------------------
# Effect types (56 of them, types 0..55).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectType:
    """One of the 56 effect types."""

    number: int  #: 0-55
    name: str
    group: int  #: 0-30, which parameter layout applies


EFFECT_TYPES: Tuple[EffectType, ...] = tuple(
    EffectType(number=entry.number, name=entry.name, group=entry.group)
    for entry in _FX_GROUP_TABLE
)


# ---------------------------------------------------------------------------
# Parameters (one bit-field of a processor's 8-byte block).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectParam:
    """One bit-field owned by a processor's parameter block."""

    key: str  #: stable identifier, e.g. "dry_wet"
    label: str  #: what the screen prints
    low: int
    high: int
    names: Optional[Tuple[str, ...]] = None  #: enumerated values
    byte: int = 0  #: 0-7 inside the processor block
    bit: int = 0
    bitlen: int = 8  #: negative = sign-padded
    parambit: int = 0


def _slug(label: str) -> str:
    out = []
    for ch in label.lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s or "param"


def _param_index_for(group: int, paramnum: int) -> Optional[int]:
    indices = FX_INDICES[group]
    for idx, pn in enumerate(indices):
        if pn == paramnum:
            return idx
    return None


# ---------------------------------------------------------------------------
# Effect (one processor).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """One processor: a type and its 8 parameter bytes."""

    type: int
    raw: bytes  #: 8 parameter bytes

    def __post_init__(self) -> None:
        if len(self.raw) != 8:
            raise ValueError(
                f"an effect's parameter block is 8 bytes, not {len(self.raw)}"
            )
        object.__setattr__(self, "raw", bytes(self.raw))

    @property
    def name(self) -> str:
        if 0 <= self.type < len(EFFECT_TYPES):
            return EFFECT_TYPES[self.type].name
        return ""

    @property
    def group(self) -> int:
        if 0 <= self.type < len(EFFECT_TYPES):
            return EFFECT_TYPES[self.type].group
        return 0

    def rows(self) -> Tuple[EffectParam, ...]:
        """The parameter rows for this effect's group."""
        group = self.group
        if group == 0:
            return ()
        pcl_entries = FX_PCL_LIST[group]
        result: list[EffectParam] = []
        seen: set[str] = set()
        for pcl in pcl_entries:
            bytenum, bit, bitlen, paramnum, parambit = pcl
            idx = _param_index_for(group, paramnum)
            info = None
            if idx is not None:
                info = FX_PARAM_LABELS.get((group, idx))
            if info is not None:
                label, low, high, names = info
                names_tuple: Optional[Tuple[str, ...]] = (
                    tuple(names) if names is not None else None
                )
            else:
                label = f"Param {paramnum}"
                mag = abs(bitlen)
                if bitlen < 0:
                    low = -(1 << (mag - 1))
                    high = (1 << (mag - 1)) - 1
                else:
                    low = 0
                    high = (1 << bitlen) - 1 if bitlen > 0 else 0
                names_tuple = None
            key = _slug(label)
            if key in seen:
                suffix = 2
                while f"{key}_{suffix}" in seen:
                    suffix += 1
                key = f"{key}_{suffix}"
            seen.add(key)
            result.append(
                EffectParam(
                    key=key,
                    label=label,
                    low=low,
                    high=high,
                    names=names_tuple,
                    byte=bytenum,
                    bit=bit,
                    bitlen=bitlen,
                    parambit=parambit,
                )
            )
        return tuple(result)

    def display(self, row: EffectParam) -> int | str:
        """The value this row currently holds."""
        mag = abs(row.bitlen)
        mask = (1 << mag) - 1
        field = (self.raw[row.byte] >> row.bit) & mask
        if row.bitlen < 0 and (field & (1 << (mag - 1))):
            field -= 1 << mag
        if row.names is not None:
            if not (0 <= field < len(row.names)):
                raise ValueError(f"{row.key} {field} is outside 0-{len(row.names) - 1}")
            return row.names[field]
        if not (row.low <= field <= row.high):
            raise ValueError(f"{row.key} {field} is outside {row.low}-{row.high}")
        return field

    def with_value(self, row: EffectParam, value: int | str) -> "Effect":  # noqa: C901
        """A copy of this effect with one row set to ``value``."""
        mag = abs(row.bitlen)
        mask = (1 << mag) - 1
        if row.names is not None:
            names = row.names
            if isinstance(value, str):
                if value not in names:
                    raise ValueError(
                        f"{row.key} has no value {value!r}; it has {', '.join(names)}"
                    )
                raw_field = names.index(value)
            else:
                if not (0 <= value < len(names)):
                    raise ValueError(f"{row.key} {value} is outside 0-{len(names) - 1}")
                raw_field = value
        else:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{row.key} takes a number, not {value!r}")
            if row.bitlen < 0:
                if not (-(1 << (mag - 1)) <= value <= (1 << (mag - 1)) - 1):
                    raise ValueError(
                        f"{row.key} {value} is outside "
                        f"{-(1 << (mag - 1))}-{(1 << (mag - 1)) - 1}"
                    )
                raw_field = (1 << mag) + value if value < 0 else value
            else:
                if not (0 <= value <= mask):
                    raise ValueError(f"{row.key} {value} is outside 0-{mask}")
                raw_field = value
            # Honour the declared UI range as well.
            if not (row.low <= value <= row.high):
                raise ValueError(f"{row.key} {value} is outside {row.low}-{row.high}")
        if not (0 <= raw_field <= mask):
            raise ValueError(f"{row.key} {raw_field} is outside 0-{mask}")
        new_byte = (self.raw[row.byte] & ~(mask << row.bit)) | (
            (raw_field & mask) << row.bit
        )
        new_raw = bytearray(self.raw)
        new_raw[row.byte] = new_byte & 0xFF
        return Effect(type=self.type, raw=bytes(new_raw))


# ---------------------------------------------------------------------------
# FxBlock (one 21-byte block).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FxBlock:
    """One 21-byte effect block (performance bytes 16-36 or Multiset 1-21)."""

    series: bool
    fx1: Effect
    fx2: Effect
    mix: Tuple[int, int]  #: FX1/FX2 mix amounts
    mod_source: Tuple[int, int]
    mod_amount: Tuple[int, int]  #: signed -15..15
    mod_sign: Tuple[bool, bool] = (False, False)  #: sign bits when amount is zero
    extended_enable: bool = True  #: byte 1, bit 7

    @classmethod
    def from_bytes(
        cls, block: bytes | bytearray | tuple[int, ...] | list[int]
    ) -> "FxBlock":
        data = bytes(block)
        if len(data) != FX_SIZE:
            raise ValueError(f"an FX block is {FX_SIZE} bytes, not {len(data)}")
        # Validate type bytes.
        for i in (0, 1):
            bits = data[i] & 0x3F
            # Valid stored type bits: 2..49 (types 0..47) and 2 for
            # extended types.  Bits 50..57 never occur in a valid block.
            if bits < 2 or bits > 49:
                raise ValueError(f"type byte {i} out of range: {bits}")
        series = bool(data[0] & 0x80)
        raw1 = data[5:13]
        raw2 = data[13:21]

        def resolve_type(bits: int, raw: bytes) -> int:
            if bits == 2:
                ext = raw[0] & 0x0F
                if ext:
                    return 47 + ext
                return 0
            return bits - 2

        type1 = resolve_type(data[0] & 0x3F, raw1)
        type2 = resolve_type(data[1] & 0x3F, raw2)

        mix = (data[2] & 0x0F, (data[2] >> 4) & 0x0F)
        mod_source = (data[3] & 0x0F, (data[3] >> 4) & 0x0F)

        amt1 = data[4] & 0x0F
        sign1 = bool(data[0] & 0x40)
        if sign1 and amt1:
            amt1 = -amt1
        amt2 = (data[4] >> 4) & 0x0F
        sign2 = bool(data[1] & 0x40)
        if sign2 and amt2:
            amt2 = -amt2
        mod_amount = (amt1, amt2)
        mod_sign = (sign1 and amt1 == 0, sign2 and amt2 == 0)

        return cls(
            series=series,
            fx1=Effect(type=type1, raw=raw1),
            fx2=Effect(type=type2, raw=raw2),
            mix=mix,
            mod_source=mod_source,
            mod_amount=mod_amount,
            mod_sign=mod_sign,
            extended_enable=bool(data[1] & 0x80),
        )

    def to_bytes(self) -> bytes:
        out = bytearray(FX_SIZE)
        # FX 1 type byte.
        if self.fx1.type >= 48:
            type_bits1 = 2
            ext1 = self.fx1.type - 47
            raw1 = bytearray(self.fx1.raw)
            raw1[0] = (raw1[0] & 0xF0) | (ext1 & 0x0F)
        else:
            type_bits1 = self.fx1.type + 2
            raw1 = bytearray(self.fx1.raw)
        # FX 2.
        if self.fx2.type >= 48:
            type_bits2 = 2
            ext2 = self.fx2.type - 47
            raw2 = bytearray(self.fx2.raw)
            raw2[0] = (raw2[0] & 0xF0) | (ext2 & 0x0F)
        else:
            type_bits2 = self.fx2.type + 2
            raw2 = bytearray(self.fx2.raw)

        out[0] = (
            (0x80 if self.series else 0)
            | (0x40 if (self.mod_amount[0] < 0 or self.mod_sign[0]) else 0)
            | (type_bits1 & 0x3F)
        )
        out[1] = (
            (0x80 if self.extended_enable else 0)
            | (0x40 if (self.mod_amount[1] < 0 or self.mod_sign[1]) else 0)
            | (type_bits2 & 0x3F)
        )
        out[2] = (self.mix[1] & 0x0F) << 4 | (self.mix[0] & 0x0F)
        out[3] = (self.mod_source[1] & 0x0F) << 4 | (self.mod_source[0] & 0x0F)
        out[4] = (abs(self.mod_amount[1]) & 0x0F) << 4 | (
            abs(self.mod_amount[0]) & 0x0F
        )
        out[5:13] = raw1
        out[13:21] = raw2
        return bytes(out)
