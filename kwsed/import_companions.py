# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: Copyright (C) 2026  kwsed contributors
#
# This file is part of kwsed.
#
# kwsed is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) any later
# version.

"""Find companion files that complete a partial source bank."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from kws import deps, importers
from kws import import_audit as ia
from kwsed import import_source as isrc

__all__ = [
    "Companion",
    "normalized_stem",
    "candidate_paths",
    "find_companions",
    "unique_equivalent_companions",
    "best_companion",
]

_COMPONENTS: tuple[tuple[str, ...], ...] = (
    ("performances",),
    ("performance",),
    ("patches",),
    ("patch",),
    ("waveseq",),
    ("waveseqs",),
    ("wavesequences",),
    ("wave", "seq"),
    ("wave", "sequence"),
)

_CANDIDATE_EXTS = {".syx", ".sysex", ".mid", ".midi", ".bnk"}


@dataclass(frozen=True)
class Companion:
    path: str
    label: str
    source: deps.Source
    merged: deps.Source
    audit: ia.DependencyAudit


def normalized_stem(path: str) -> str:
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    text = stem.casefold()
    tokens = [t for t in re.split(r"[^0-9a-z]+", text) if t]
    changed = True
    while changed and tokens:
        changed = False
        for comp in _COMPONENTS:
            n = len(comp)
            if len(tokens) >= n and tuple(tokens[-n:]) == comp:
                del tokens[-n:]
                changed = True
                break
    return " ".join(tokens)


def candidate_paths(path: str) -> List[str]:
    directory = os.path.dirname(os.path.abspath(path))
    target = os.path.abspath(path)
    result: List[str] = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return result
    for name in entries:
        full = os.path.join(directory, name)
        if os.path.abspath(full) == target:
            continue
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in _CANDIDATE_EXTS:
            continue
        result.append(full)
    result.sort()
    return result


def find_companions(path: str, source: deps.Source) -> List[Companion]:
    current_audit = ia.audit_source(source)
    if current_audit.complete:
        return []
    current_missing = (
        len(current_audit.missing_patches),
        len(current_audit.missing_waveseqs),
    )
    stem = normalized_stem(path)
    out: List[Companion] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidate_paths(path):
        if normalized_stem(candidate) != stem:
            continue
        try:
            imported = importers.read_file(candidate)
        except (importers.UnsupportedFormat, ValueError, OSError):
            continue
        for bank in imported:
            if not isrc.same_source_bank(source, bank.source):
                continue
            merged = isrc.merge_source_components(source, bank.source)
            if merged is None:
                continue
            audit = ia.audit_source(merged)
            if not (
                len(audit.missing_patches) < current_missing[0]
                or len(audit.missing_waveseqs) < current_missing[1]
            ):
                continue
            key = (candidate, bank.source.scope_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Companion(
                    path=candidate,
                    label=bank.label,
                    source=bank.source,
                    merged=merged,
                    audit=audit,
                )
            )
    return out


def _merged_key(source: deps.Source) -> tuple[object, ...]:
    return (
        tuple(source.performances),
        tuple(source.patches),
        source.waveseq,
    )


def unique_equivalent_companions(
    companions: List[Companion], path: str
) -> List[Companion]:
    """Collapse byte-identical candidates such as a .MID/.syx pair.

    SoundArtMedia packages often carry both a MIDI and a SysEx copy of the
    same patch bank. They are not ambiguity; they are duplicates. Keep one,
    preferring the same extension as the source performance file.
    """
    if len(companions) <= 1:
        return companions
    keys = {_merged_key(companion.merged) for companion in companions}
    if len(keys) != 1:
        return companions
    source_ext = os.path.splitext(path)[1].lower()
    chosen = sorted(
        companions,
        key=lambda companion: (
            os.path.splitext(companion.path)[1].lower() != source_ext,
            companion.path,
        ),
    )[0]
    return [chosen]


def best_companion(path: str, source: deps.Source) -> Optional[Companion]:
    companions = unique_equivalent_companions(find_companions(path, source), path)
    if len(companions) != 1:
        return None
    return companions[0]
