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
from typing import Any, Optional, Tuple

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
    "StorageField",
    "EffectParam",
    "Effect",
    "FxBlock",
    "EMPTY_FX",
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
# Storage fields and logical parameters.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageField:
    """One storage field contributing to a logical parameter's value.

    ``byte`` (0-7) and ``bit`` locate the field in the processor's 8-byte
    block; ``bitlen`` is the field width (negative means sign-extended);
    ``parambit`` is the bit offset of this field inside the logical value.
    """

    byte: int  #: 0-7 inside the processor block
    bit: int
    bitlen: int  #: negative = sign-extended
    parambit: int


@dataclass(frozen=True)
class EffectParam:
    """A logical parameter assembled from one or more storage fields.

    A parameter is a single logical value; the PCL is the authority on how
    its storage fields are assembled.  ``fields`` holds every storage field
    that contributes to the value.
    """

    key: str  #: stable identifier, e.g. "dry_wet"
    label: str  #: what the screen prints
    low: int
    high: int
    names: Optional[Tuple[str, ...]]  #: enumerated values
    fields: Tuple[StorageField, ...]


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


def _logical_width(row: EffectParam) -> int:
    if not row.fields:
        return 0
    return max(f.parambit + abs(f.bitlen) for f in row.fields)


def _is_signed(row: EffectParam) -> bool:
    return any(f.bitlen < 0 for f in row.fields)


# ---------------------------------------------------------------------------
# Effect (one processor).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """One processor: a type and its 8 parameter bytes."""

    type: int
    raw: bytes  #: 8 parameter bytes
    stored_type_bits: Optional[int] = None  #: raw 6-bit field from byte 0/1

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
        """The parameter rows for this effect's group.

        One row per distinct ``paramnum`` in the group's PCL, in the order
        of first appearance.  The PCL is the authority on storage layout.
        """
        group = self.group
        if group == 0:
            return ()
        pcl_entries = FX_PCL_LIST[group]
        # Group by paramnum, preserving first appearance.
        ordered: list[int] = []
        seen_pn: set[int] = set()
        fields_by_param: dict[int, list[Any]] = {}
        for pcl in pcl_entries:
            paramnum = pcl.paramnum
            fields_by_param.setdefault(paramnum, []).append(pcl)
            if paramnum not in seen_pn:
                seen_pn.add(paramnum)
                ordered.append(paramnum)

        result: list[EffectParam] = []
        seen_keys: set[str] = set()
        for paramnum in ordered:
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
                fields = fields_by_param[paramnum]
                w = max(f.parambit + abs(f.bitlen) for f in fields) if fields else 0
                any_neg = any(f.bitlen < 0 for f in fields)
                if any_neg:
                    low = -(1 << (w - 1)) if w > 0 else 0
                    high = (1 << (w - 1)) - 1 if w > 0 else 0
                else:
                    low = 0
                    high = (1 << w) - 1 if w > 0 else 0
                names_tuple = None

            storage_fields = tuple(
                StorageField(
                    byte=e.bytenum,
                    bit=e.bit,
                    bitlen=e.bitlen,
                    parambit=e.parambit,
                )
                for e in fields_by_param[paramnum]
            )
            row = EffectParam(
                key="",  # filled below
                label=label,
                low=low,
                high=high,
                names=names_tuple,
                fields=storage_fields,
            )
            key = _slug(label)
            if key in seen_keys:
                suffix = 2
                while f"{key}_{suffix}" in seen_keys:
                    suffix += 1
                key = f"{key}_{suffix}"
            seen_keys.add(key)
            # Rebuild with key (dataclass frozen, so construct fresh).
            row = EffectParam(
                key=key,
                label=label,
                low=low,
                high=high,
                names=names_tuple,
                fields=storage_fields,
            )
            result.append(row)
        return tuple(result)

    def _assemble(self, row: EffectParam) -> int:
        w = _logical_width(row)
        unsigned = 0
        for f in row.fields:
            mag = abs(f.bitlen)
            if mag == 0:
                continue
            fm = (1 << mag) - 1
            field_val = (self.raw[f.byte] >> f.bit) & fm
            if f.bitlen < 0 and (field_val & (1 << (mag - 1))):
                field_val -= 1 << mag
            unsigned |= (field_val & fm) << f.parambit
        if _is_signed(row) and w > 0 and (unsigned & (1 << (w - 1))):
            return unsigned - (1 << w)
        return unsigned

    def display(self, row: EffectParam) -> int | str:
        """The value this row currently holds."""
        value = self._assemble(row)
        if row.names is not None:
            if not (0 <= value < len(row.names)):
                raise ValueError(f"{row.key} {value} is outside 0-{len(row.names) - 1}")
            return row.names[value]
        if not (row.low <= value <= row.high):
            raise ValueError(f"{row.key} {value} is outside {row.low}-{row.high}")
        return value

    def with_value(self, row: EffectParam, value: int | str) -> "Effect":  # noqa: C901
        """A copy of this effect with one row set to ``value``."""
        w = _logical_width(row)
        signed = _is_signed(row)
        if row.names is not None:
            names = row.names
            if isinstance(value, str):
                if value not in names:
                    raise ValueError(
                        f"{row.key} has no value {value!r}; it has {', '.join(names)}"
                    )
                raw_logical = names.index(value)
            else:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{row.key} takes a number, not {value!r}")
                if not (0 <= value < len(names)):
                    raise ValueError(f"{row.key} {value} is outside 0-{len(names) - 1}")
                raw_logical = value
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{row.key} takes a number, not {value!r}")
            if signed and w > 0:
                if not (-(1 << (w - 1)) <= value <= (1 << (w - 1)) - 1):
                    raise ValueError(
                        f"{row.key} {value} is outside "
                        f"{-(1 << (w - 1))}-{(1 << (w - 1)) - 1}"
                    )
            else:
                mask = (1 << w) - 1 if w > 0 else 0
                if not (0 <= value <= mask):
                    raise ValueError(f"{row.key} {value} is outside 0-{mask}")
            if not (row.low <= value <= row.high):
                raise ValueError(f"{row.key} {value} is outside {row.low}-{row.high}")
            raw_logical = value

        uvalue = raw_logical & ((1 << w) - 1) if w > 0 else 0

        new_raw = bytearray(self.raw)
        for f in row.fields:
            mag = abs(f.bitlen)
            if mag == 0:
                continue
            fm = (1 << mag) - 1
            field_raw = (uvalue >> f.parambit) & fm
            new_raw[f.byte] = (new_raw[f.byte] & ~(fm << f.bit)) | (
                (field_raw & fm) << f.bit
            )
            new_raw[f.byte] &= 0xFF
        return Effect(
            type=self.type, raw=bytes(new_raw), stored_type_bits=self.stored_type_bits
        )

    def with_type(self, new_type: int) -> "Effect":
        """A copy of this effect with a new logical type.

        ``stored_type_bits`` is cleared so :meth:`FxBlock.to_bytes` re-encodes
        the type from the logical value.
        """
        return Effect(type=new_type, raw=self.raw, stored_type_bits=None)


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
        series = bool(data[0] & 0x80)
        raw1 = data[5:13]
        raw2 = data[13:21]

        def resolve_type(bits: int, raw: bytes) -> int:
            # bits 2-49: normal type = bits - 2 (extended when bits==2
            # and the low nibble of the first parameter byte is nonzero).
            if 2 <= bits <= 49:
                if bits == 2:
                    ext = raw[0] & 0x0F
                    if ext:
                        return 47 + ext
                return bits - 2
            # Anything else: logical Off; raw bits preserved separately.
            return 0

        bits1 = data[0] & 0x3F
        bits2 = data[1] & 0x3F
        type1 = resolve_type(bits1, raw1)
        type2 = resolve_type(bits2, raw2)

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
            fx1=Effect(type=type1, raw=raw1, stored_type_bits=bits1),
            fx2=Effect(type=type2, raw=raw2, stored_type_bits=bits2),
            mix=mix,
            mod_source=mod_source,
            mod_amount=mod_amount,
            mod_sign=mod_sign,
            extended_enable=bool(data[1] & 0x80),
        )

    @classmethod
    def empty(cls) -> "FxBlock":
        """A valid "Off" block: both processors type 0, extended-enable set."""
        return cls(
            series=False,
            fx1=Effect(type=0, raw=bytes(8), stored_type_bits=2),
            fx2=Effect(type=0, raw=bytes(8), stored_type_bits=2),
            mix=(0, 0),
            mod_source=(0, 0),
            mod_amount=(0, 0),
            mod_sign=(False, False),
            extended_enable=True,
        )

    def to_bytes(self) -> bytes:
        out = bytearray(FX_SIZE)
        # FX 1 type byte.
        if self.fx1.stored_type_bits is not None:
            type_bits1 = self.fx1.stored_type_bits
            raw1 = bytearray(self.fx1.raw)
        elif self.fx1.type >= 48:
            type_bits1 = 2
            ext1 = self.fx1.type - 47
            raw1 = bytearray(self.fx1.raw)
            raw1[0] = raw1[0] | (ext1 & 0x0F)
        else:
            type_bits1 = self.fx1.type + 2
            raw1 = bytearray(self.fx1.raw)
        # FX 2.
        if self.fx2.stored_type_bits is not None:
            type_bits2 = self.fx2.stored_type_bits
            raw2 = bytearray(self.fx2.raw)
        elif self.fx2.type >= 48:
            type_bits2 = 2
            ext2 = self.fx2.type - 47
            raw2 = bytearray(self.fx2.raw)
            raw2[0] = raw2[0] | (ext2 & 0x0F)
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


#: A module-level "Off" block, equivalent to :meth:`FxBlock.empty`.
EMPTY_FX = FxBlock.empty()
