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
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.

"""A per-scope cache of whole banks: performances, patches, wave sequences.

This is a convenience cache, not the source of truth. A missing or corrupt
file is ignored with a short warning on ``sys.stderr`` rather than raising,
because the bytes can always be read off the unit again. Nothing here touches
MIDI, the instrument, or any hardware: it only reads and writes JSON files
on disk.

Each scope (``RAM1``, ``CARD/07``, ``CARD/c1`` ...) owns one file. Kinds
are merged across saves, so saving only patches never erases performances
or wave sequences already stored for that scope.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Dict, List, Optional

from kws import banks, deps, favorites
from kws import records as rec

__all__ = [
    "default_dir",
    "save",
    "load",
    "load_all",
    "has_complete_bank",
]


def default_dir() -> str:
    """Where bank cache files live, beside the favourites database."""
    return os.path.join(favorites.data_dir(), "banks")


def _filename(scope_id: str) -> str:
    """Map a scope id to a safe filename.

    ``/`` becomes ``__`` (``CARD/07`` -> ``CARD__07.json``); the result has
    no path separators and cannot escape its directory.
    """
    name = str(scope_id).replace("/", "__")
    name = name.replace(os.sep, "__")
    if os.altsep:
        name = name.replace(os.altsep, "__")
    name = os.path.basename(name)
    if not name or name in (".", ".."):
        raise ValueError(f"invalid scope id {scope_id!r}")
    if not name.endswith(".json"):
        name = name + ".json"
    return name


def _path(scope_id: str, directory: Optional[str]) -> str:
    d = directory if directory is not None else default_dir()
    return os.path.join(d, _filename(scope_id))


def _warn(path: str, reason: object) -> None:
    print(
        f"kwsed: ignoring {path}: {reason}. It is a cache of banks; "
        "delete it and read them again.",
        file=sys.stderr,
    )


_EXPECTED = {
    "performances": banks.PERFORMANCES_PER_BANK * rec.PERFORMANCE_SIZE,
    "patches": banks.PATCHES_PER_BANK * rec.PATCH_SIZE,
    "waveseq": rec.WAVESEQ_BANK_SIZE,
}


def _validate(kind: str, data: bytes) -> None:
    expected = _EXPECTED[kind]
    if len(data) != expected:
        raise ValueError(f"{kind} must be {expected} bytes; got {len(data)}")


def _decode_envelope(path: str) -> Optional[Dict[str, object]]:
    """Read and return the JSON envelope, or None (with a warning) if corrupt."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _warn(path, exc)
        return None
    if not isinstance(raw, dict):
        _warn(path, "not a JSON object")
        return None
    return raw


def _decode_kind(path: str, raw: Dict[str, object], kind: str) -> Optional[bytes]:
    """One stored kind, or None when absent or corrupt (with a warning)."""
    if kind not in raw:
        return None
    value = raw[kind]
    if not isinstance(value, str):
        _warn(path, f"{kind} is not a string")
        return None
    try:
        blob = base64.b64decode(value, validate=True)
    except Exception:
        _warn(path, f"invalid base64 in {kind}")
        return None
    expected = _EXPECTED[kind]
    if len(blob) != expected:
        _warn(path, f"{kind} must be {expected} bytes; got {len(blob)}")
        return None
    return blob


def _decode_kinds(path: str, raw: Dict[str, object]) -> Optional[Dict[str, bytes]]:
    """Every stored kind, or None if any stored one is corrupt.

    An absent kind is not corrupt: a bank may have been captured in pieces.
    A kind that is present but undecodable poisons the whole envelope,
    because a partial answer would silently drop the records a caller
    believes it just loaded.
    """
    out: Dict[str, bytes] = {}
    for kind in ("performances", "patches", "waveseq"):
        if kind not in raw:
            continue
        blob = _decode_kind(path, raw, kind)
        if blob is None:
            return None
        out[kind] = blob
    return out


def _split(blob: Optional[bytes], count: int, size: int) -> List[bytes]:
    """Split a flat blob into whole records; absent gives an empty list."""
    if blob is None:
        return []
    return [blob[index * size : (index + 1) * size] for index in range(count)]


def _build_source(
    path: str, fallback_scope: str, raw: Dict[str, object]
) -> Optional[deps.Source]:
    scope = raw.get("scope")
    if not isinstance(scope, str) or not scope:
        scope = fallback_scope
    decoded = _decode_kinds(path, raw)
    if decoded is None:
        return None
    return deps.Source(
        bank_id=scope,
        performances=_split(
            decoded.get("performances"),
            banks.PERFORMANCES_PER_BANK,
            rec.PERFORMANCE_SIZE,
        ),
        patches=_split(decoded.get("patches"), banks.PATCHES_PER_BANK, rec.PATCH_SIZE),
        waveseq=decoded.get("waveseq"),
    )


def load(scope_id: str, directory: Optional[str] = None) -> Optional[deps.Source]:
    where = _path(scope_id, directory)
    if not os.path.exists(where):
        return None
    raw = _decode_envelope(where)
    if raw is None:
        return None
    fallback = _filename(scope_id)[:-5].replace("__", "/")
    return _build_source(where, fallback, raw)


def _provided_kinds(
    performances: Optional[bytes],
    patches: Optional[bytes],
    waveseq: Optional[bytes],
) -> Dict[str, bytes]:
    """Validate and collect the kinds being written."""
    out: Dict[str, bytes] = {}
    for kind, data in (
        ("performances", performances),
        ("patches", patches),
        ("waveseq", waveseq),
    ):
        if data is not None:
            _validate(kind, data)
            out[kind] = data
    return out


def _read_existing(where: str) -> Dict[str, object]:
    """The envelope already on disk, or {} when absent or corrupt."""
    try:
        with open(where, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_atomic(where: str, envelope: Dict[str, object]) -> None:
    """Write the envelope through a temporary file and rename it into place."""
    temp = where + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle)
            handle.write("\n")
        os.replace(temp, where)
    except Exception:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        raise


def save(
    scope_id: str,
    *,
    performances: Optional[bytes] = None,
    patches: Optional[bytes] = None,
    waveseq: Optional[bytes] = None,
    directory: Optional[str] = None,
) -> None:
    provided = _provided_kinds(performances, patches, waveseq)
    where = _path(scope_id, directory)
    os.makedirs(os.path.dirname(os.path.abspath(where)), exist_ok=True)

    existing = _read_existing(where)
    envelope: Dict[str, object] = {"version": 1, "scope": scope_id}
    # Carry over kinds not being overwritten, so a partial capture merges
    # rather than erasing the rest of the bank.
    for kind in ("performances", "patches", "waveseq"):
        if kind in provided:
            envelope[kind] = base64.b64encode(provided[kind]).decode("ascii")
        elif kind in existing:
            envelope[kind] = existing[kind]
    _write_atomic(where, envelope)


def load_all(directory: Optional[str] = None) -> Dict[str, deps.Source]:
    d = directory if directory is not None else default_dir()
    out: Dict[str, deps.Source] = {}
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        raw = _decode_envelope(path)
        if raw is None:
            continue
        fallback = name[:-5].replace("__", "/")
        src = _build_source(path, fallback, raw)
        if src is None:
            continue
        out[src.bank_id] = src
    return out


def has_complete_bank(scope_id: str, directory: Optional[str] = None) -> bool:
    where = _path(scope_id, directory)
    try:
        with open(where, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    if "performances" not in raw or "patches" not in raw:
        return False
    try:
        perf = base64.b64decode(raw["performances"], validate=True)
        pat = base64.b64decode(raw["patches"], validate=True)
    except Exception:
        return False
    if len(perf) != _EXPECTED["performances"]:
        return False
    return len(pat) == _EXPECTED["patches"]
