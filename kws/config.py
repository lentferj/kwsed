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

from vinsynlib.config import (
    bind,
    config_dir,
    config_help,
    default_config_path,
    migrate_legacy_config,
)

DEFAULT_PATH = default_config_path("kwsed")

__all__ = [
    "DEFAULT_PATH",
    "bind",
    "config_dir",
    "config_help",
    "load_channel",
    "load_device_id",
    "load_last_port",
    "load_last_recv_port",
    "load_port_pair",
    "migrate_legacy_config",
    "save_channel",
    "save_device_id",
    "save_last_port",
    "save_last_recv_port",
    "save_ports",
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
