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

"""The local settings cache: which port answered, and which channel.

Now a binding of :class:`vinsynlib.config.Settings` rather than a fifth copy
of it. The behaviour, including the traps it is written around, is the
family's and lives in one place:

* A TOML boolean is not a channel. ``isinstance(True, int)`` is true, so a
  hand-edited ``channel = true`` used to come back as ``True``, pass every
  range check, and then ``0xC0 | True`` is MIDI channel 2 -- a wrong channel
  being not an error anywhere.
* Everything written is escaped. A port name is whatever ALSA reports, and a
  quote in one produces a file that is not TOML, which the writer then
  refuses to overwrite: a cache that never heals.
* Only :class:`OSError` is swallowed -- a read-only directory or a full disk,
  where forgetting a preference beats refusing to run.

What is added here is the name. One string decides what the one warning
message says, and it has to be right: several of these programs can be open
in one session, and a user who is told a file could not be parsed has to be
able to tell which one is unhappy.
"""

from __future__ import annotations

import os
import re
from typing import Any

from kws import banks
from vinsynlib.config import (
    bind,
    config_dir,
    config_help,
    default_config_path,
    migrate_legacy_config,
)

DEFAULT_PATH = default_config_path("kwsed")

CARD_MODES = ("none", "cards", "hcard")
DEFAULT_CARD_MODE = "cards"

#: UI state lives in its own file beside the settings, not inside them. A
#: hand-edited dotted key in the settings file is a TOML table, and the
#: writer refuses any file holding one -- so one such line used to freeze
#: every later save, the channel and port included.
UI_STATE_FILENAME = "ui-state.toml"

#: The namespaces whose keys are UI state rather than connection settings.
UI_NAMESPACES = ("browser", "import", "scan")

__all__ = [
    "CARD_MODES",
    "DEFAULT_CARD_MODE",
    "DEFAULT_PATH",
    "bind",
    "config_dir",
    "config_help",
    "load_card_mode",
    "load_channel",
    "load_device_id",
    "load_last_port",
    "load_last_recv_port",
    "UI_STATE_FILENAME",
    "ui_state_path",
    "migrate_ui_state",
    "load_port_pair",
    "load_scan_offer_dismissed",
    "load_show_card_defaults",
    "load_show_patches",
    "load_deps_pane",
    "load_import_folder",
    "load_preaudit_backup",
    "load_preaudit_patch_area",
    "load_preaudit_target",
    "load_single_object_timeout",
    "migrate_legacy_config",
    "parse_preaudit_backup",
    "parse_preaudit_patch_range",
    "parse_preaudit_perf",
    "save_card_mode",
    "save_channel",
    "save_device_id",
    "save_last_port",
    "save_last_recv_port",
    "save_ports",
    "save_scan_offer_dismissed",
    "save_show_card_defaults",
    "save_show_patches",
    "save_deps_pane",
    "save_import_folder",
    "save_single_object_timeout",
]

_settings = bind("kwsed", DEFAULT_PATH)

#: Module-level aliases, so every existing call site reads the same as it did
#: before: ``config.load_channel(path)``. The library takes the path as an
#: optional first argument exactly as these did, so nothing here wraps.
load_channel = _settings.load_channel
save_channel = _settings.save_channel
load_last_port = _settings.load_port
save_last_port = _settings.save_port
load_last_recv_port = _settings.load_recv_port
save_last_recv_port = _settings.save_recv_port
load_device_id = _settings.load_device_id
save_device_id = _settings.save_device_id
#: Both ports at once, in one write. What the connection policy saves after a
#: sweep: a half-written pair is a pair where the second half is lost.
save_ports = _settings.save_ports


def load_port_pair(path: str | None = None) -> tuple[str, str] | None:
    """The remembered port pair, or None.

    :meth:`vinsynlib.config.Settings.load_ports` answers only when *both*
    keys are present, because for most of the family a half-remembered pair
    is not a pair. Here it would be wrong: a Wavestation's input port is the
    same USB port it outputs to unless the user has said otherwise, so a
    remembered output port on its own is a complete pair. Reading it as
    half a pair would mean sweeping on every launch, which is the thing this
    exists to stop.

    ``recv_port`` did not exist in this project's config until the
    remembered-port path did; a config written before that has no such key
    and reads as the single port it was.
    """
    send = _settings.load_port(path)
    if not send:
        return None
    return (send, _settings.load_recv_port(path) or send)


def ui_state_path(path: str | None = None) -> str | None:
    """``ui-state.toml`` beside the settings file, or the library's default.

    ``None`` keeps the library's default (a ``ui-state.toml`` next to the
    default settings); a settings path places it beside that path instead.
    """
    if path is None:
        return None
    return os.path.join(os.path.dirname(os.path.abspath(path)), UI_STATE_FILENAME)


def load_scan_offer_dismissed(path: str | None = None) -> bool:
    """Whether the user chose "don't ask again" for the scan offer."""
    return bool(
        _settings.load_ui_bool(
            "scan.offer_dismissed", ui_state_path(path), default=False
        )
    )


def save_scan_offer_dismissed(value: bool, path: str | None = None) -> None:
    """Remember the scan offer's "don't ask again" answer."""
    _settings.save_ui_state("scan.offer_dismissed", value, ui_state_path(path))


def load_card_mode(path: str | None = None) -> str:
    """The configured card mode, or the default when missing/invalid."""
    data = _settings.read(path or DEFAULT_PATH)[0]
    value = data.get("card_mode")
    if isinstance(value, str) and value in CARD_MODES:
        return value
    return DEFAULT_CARD_MODE


def save_card_mode(mode: str, path: str | None = None) -> None:
    """Validate and store the card mode."""
    if mode not in CARD_MODES:
        raise ValueError(f"invalid card mode {mode!r}; expected one of {CARD_MODES!r}")
    _settings.update(path or DEFAULT_PATH, card_mode=mode)


def load_show_card_defaults(path: str | None = None) -> bool:
    """Whether hcard mode shows the manual's pre-set bank names."""
    data = _settings.read(path or DEFAULT_PATH)[0]
    value = data.get("show_card_defaults")
    return value is True


def save_show_card_defaults(value: object, path: str | None = None) -> None:
    """Coerce and store the show_card_defaults flag."""
    _settings.update(path or DEFAULT_PATH, show_card_defaults=bool(value))


def load_show_patches(path: str | None = None) -> bool:
    """Whether the browser lists patch rows, remembered between runs."""
    return bool(
        _settings.load_ui_bool(
            "browser.show_patches", ui_state_path(path), default=True
        )
    )


def save_show_patches(value: bool, path: str | None = None) -> None:
    """Remember whether the browser lists patch rows."""
    _settings.save_ui_state("browser.show_patches", bool(value), ui_state_path(path))


def load_deps_pane(path: str | None = None) -> bool:
    """Whether the browser's dependency pane is visible, remembered."""
    return bool(
        _settings.load_ui_bool("browser.deps_pane", ui_state_path(path), default=True)
    )


def save_deps_pane(value: bool, path: str | None = None) -> None:
    """Remember whether the browser's dependency pane is visible."""
    _settings.save_ui_state("browser.deps_pane", bool(value), ui_state_path(path))


def load_import_folder(path: str | None = None) -> str:
    """The last folder used by the import file picker, or ''."""
    value = _settings.load_ui_str("import.folder", ui_state_path(path), default="")
    return value if isinstance(value, str) else ""


def save_import_folder(value: str, path: str | None = None) -> None:
    """Remember the last folder used by the import file picker."""
    _settings.save_ui_state("import.folder", value, ui_state_path(path))


#: Range bounds, mirroring ``banks.PERFORMANCES_PER_BANK`` (50) and
#: ``banks.PATCHES_PER_BANK`` (35); duplicated here so ``kws.config`` does
#: not import ``kws.banks`` at module load in a way that could cycle. (It
#: turns out ``banks`` does not import ``config``, so the import above is
#: safe; the bounds are still stated locally for clarity.)
_PREAUDIT_PERF_COUNT = 50
_PREAUDIT_PATCH_COUNT = 35


def _parse_bank_token(token: str) -> Any:
    """Resolve a bank id token, rejecting space-bearing labels."""
    if not token or any(ch.isspace() for ch in token):
        raise ValueError(
            f"unknown bank {token!r}; expected one of RAM1, RAM2, "
            f"RAM3, ROM4..ROM11, CARD"
        )
    try:
        entry = banks.bank(token)
    except (LookupError, ValueError, TypeError):
        raise ValueError(
            f"unknown bank {token!r}; expected one of RAM1, RAM2, "
            f"RAM3, ROM4..ROM11, CARD"
        ) from None
    if not entry.writable:
        raise ValueError(
            f"bank {entry.id} is not writable (ROM); the pre-audit "
            f"staging area must live in a writable bank"
        )
    return entry


def parse_preaudit_perf(value: Any) -> tuple[Any, int] | None:
    """Parse "RAM3:49" into (Bank, slot) for the pre-audit staging slot.

    Returns None for an unset/empty value. Raises ValueError with a
    helpful message for anything malformed, a bank the unit cannot
    write (ROM), or a slot outside 0-49.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("preaudit target must be a string like 'RAM3:49'")
    text = value.strip()
    if text == "":
        return None
    if ":" not in text:
        raise ValueError(f"malformed preaudit target {value!r}; expected 'BANK:SLOT'")
    bank_token, _, slot_token = text.partition(":")
    bank_token = bank_token.strip()
    slot_token = slot_token.strip()
    if bank_token == "" or slot_token == "" or ":" in slot_token:
        raise ValueError(f"malformed preaudit target {value!r}; expected 'BANK:SLOT'")
    entry = _parse_bank_token(bank_token)
    try:
        slot = int(slot_token)
    except (ValueError, TypeError):
        raise ValueError(f"invalid slot {slot_token!r} in {value!r}") from None
    if slot < 0 or slot >= _PREAUDIT_PERF_COUNT:
        raise ValueError(
            f"slot {slot} out of range for {entry.id}; expected 0-"
            f"{_PREAUDIT_PERF_COUNT - 1}"
        )
    return entry, slot


def _parse_patch_slots(rest: str, value: Any) -> tuple[int, ...]:
    if "-" in rest:
        parts = rest.split("-")
        if len(parts) != 2:
            raise ValueError(f"malformed preaudit patch area {value!r}")
        try:
            a = int(parts[0].strip())
            b = int(parts[1].strip())
        except (ValueError, TypeError):
            raise ValueError(f"invalid slot range {rest!r} in {value!r}") from None
        if a > b:
            raise ValueError(f"slot range {a}-{b} is not ascending in {value!r}")
        return tuple(range(a, b + 1))
    try:
        return (int(rest),)
    except (ValueError, TypeError):
        raise ValueError(f"invalid slot {rest!r} in {value!r}") from None


def parse_preaudit_patch_range(value: Any) -> tuple[Any, tuple[int, ...]] | None:
    """Parse "RAM3:27-34" into (Bank, slots) for the pre-audit patch area.

    "RAM3:30" (one slot) is also valid. Returns None when unset.
    Raises ValueError for malformed input, a ROM bank, or slots
    outside 0-34.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("preaudit patch area must be a string like 'RAM3:27-34'")
    text = value.strip()
    if text == "":
        return None
    if ":" not in text:
        raise ValueError(
            f"malformed preaudit patch area {value!r}; expected "
            f"'BANK:SLOT' or 'BANK:A-B'"
        )
    bank_token, _, rest = text.partition(":")
    bank_token = bank_token.strip()
    rest = rest.strip()
    if bank_token == "" or rest == "" or ":" in rest:
        raise ValueError(f"malformed preaudit patch area {value!r}")
    entry = _parse_bank_token(bank_token)
    slots = _parse_patch_slots(rest, value)
    if any(s < 0 or s >= _PREAUDIT_PATCH_COUNT for s in slots):
        bad = next(s for s in slots if s < 0 or s >= _PREAUDIT_PATCH_COUNT)
        raise ValueError(
            f"slot {bad} out of range for {entry.id}; expected 0-"
            f"{_PREAUDIT_PATCH_COUNT - 1}"
        )
    return entry, slots


def parse_preaudit_backup(value: Any) -> bool:
    """Whether a staged audition backs up what it overwrites.

    Default is True: the staging slots are the user's, and an audition
    that replaces them should be undoable. Turning it off saves the reads
    that capture those slots -- one per slot per session -- at the cost of
    `z` no longer restoring them.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "true", "yes", "on", "1"):
            return True
        if text in ("false", "no", "off", "0"):
            return False
    raise ValueError(f"preaudit_backup must be true or false, not {value!r}")


def load_preaudit_backup(path: str | None = None) -> bool:
    """The pre-audit backup switch; True when unset or malformed."""
    data = _settings.read(path or DEFAULT_PATH)[0]
    try:
        return parse_preaudit_backup(data.get("preaudit_backup"))
    except ValueError:
        return True


def load_preaudit_target(path: str | None = None) -> tuple[Any, int] | None:
    """The pre-audit staging performance slot, or None when unset/broken."""
    data = _settings.read(path or DEFAULT_PATH)[0]
    value = data.get("preaudit_target_perf")
    if not isinstance(value, str):
        return None
    try:
        return parse_preaudit_perf(value)
    except ValueError:
        return None


DEFAULT_SINGLE_OBJECT_TIMEOUT = 15.0

#: Default configuration values for keys that are not yet in the file.
DEFAULT_CONFIG = {
    "single_object_timeout": DEFAULT_SINGLE_OBJECT_TIMEOUT,
}


def load_single_object_timeout(path: str | None = None) -> float:
    """The timeout for single object (patch/performance) reads, in seconds."""
    data = _settings.read(path or DEFAULT_PATH)[0]
    value = data.get("single_object_timeout")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return DEFAULT_SINGLE_OBJECT_TIMEOUT


def save_single_object_timeout(value: float, path: str | None = None) -> None:
    """Store the single object timeout."""
    _settings.update(path or DEFAULT_PATH, single_object_timeout=float(value))


def load_preaudit_patch_area(
    path: str | None = None,
) -> tuple[Any, tuple[int, ...]] | None:
    """The pre-audit staging patch slots, or None when unset/broken."""
    data = _settings.read(path or DEFAULT_PATH)[0]
    value = data.get("preaudit_target_patches")
    if not isinstance(value, str):
        return None
    try:
        return parse_preaudit_patch_range(value)
    except ValueError:
        return None


def _toml_scalar(value: object) -> str | None:
    """One flat setting as TOML, or ``None`` when it is not a scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return None


def _write_settings(where: str, values: dict[str, object]) -> None:
    """Rewrite a settings file with flat scalar keys only, atomically."""
    lines = ["# kwsed local config - gitignored, safe to delete."]
    for key, value in values.items():
        rendered = _toml_scalar(value)
        if rendered is None:
            continue
        written_key = key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else f'"{key}"'
        lines.append(f"{written_key} = {rendered}")
    temp = where + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temp, where)
    except OSError:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            pass
        raise


def _split_ui_state(
    data: dict[str, object],
) -> "tuple[dict[str, str | int | float | bool], dict[str, object], bool] | None":
    """Separate UI keys from flat settings, or ``None`` if the file is not ours.

    A table or list outside the known UI namespaces means the settings file
    holds something this program did not write; the caller must leave it
    alone rather than rewrite it.
    """
    moved: dict[str, str | int | float | bool] = {}
    flat: dict[str, object] = {}
    stale = False
    for key, value in data.items():
        if isinstance(value, dict):
            if key not in UI_NAMESPACES:
                return None
            stale = True
            for sub, subvalue in value.items():
                if not isinstance(subvalue, (str, int, float, bool)):
                    return None
                moved[f"{key}.{sub}"] = subvalue
        elif isinstance(value, list):
            return None
        elif "." in key and isinstance(value, (str, int, float, bool)):
            stale = True
            moved[key] = value
        elif key in UI_NAMESPACES:
            return None
        else:
            flat[key] = value
    return moved, flat, stale


def migrate_ui_state(path: str | None = None) -> None:
    """Move UI state out of the settings file and heal a dotted-key table.

    Older builds wrote UI state into ``config.toml``. An unquoted dotted key
    (``browser.deps_pane = false``) is a TOML table, and both writers refuse
    a file that holds one -- so that single line froze every later save,
    including the channel and port. The known UI namespaces are copied to
    ``ui-state.toml`` and the settings file is rewritten with its flat keys
    only. A table or list this does not recognise is left untouched: the
    file is then someone else's, and the refusal is right.
    """
    where = path or DEFAULT_PATH
    data, status = _settings.read(where)
    if status != "ok" or not data:
        return
    split = _split_ui_state(data)
    if split is None:
        return
    moved, flat, stale = split
    if not stale:
        return
    target = ui_state_path(where)
    try:
        for key, value in moved.items():
            _settings.save_ui_state(key, value, target)
        _write_settings(where, flat)
    except OSError:
        return
