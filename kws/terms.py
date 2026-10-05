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

"""What the Wavestation calls things, declared once.

**The instrument's word is the instrument's.** The Wavestation prints
PATCH on its display for a single sound and names the area BANK A through
ROM D, so those are the words every string a user reads uses here.

**A Wavestation bank has two kinds of thing in it, and the tool says so.**
A performance is up to eight parts; a patch is one sound. Performance 7 is
not patch 7. So a favourite's key is ``RAM1:performance`` rather than
``RAM1:patch`` -- the kind is part of the identity. That is the one
documented exception in the family to keying a favourite on the slot alone
(docs/UX-SPEC.md section 1), and :func:`kwsed.app.favorite_bank_id` is where
it is implemented.

``performance`` is in the family's allowed sound words, so a conformance
check accepts it; the word the *display* uses for a single sound stays
``patch``, and the distinction is kwsed's own rather than the family's.
"""

from __future__ import annotations

from vinsynlib.terms import Terminology, register

__all__ = ["TERMS"]

TERMS = Terminology(
    app_name="kwsed",
    sound="patch",
    container="bank",
    device="Korg Wavestation SR",
    own={
        # The second kind of thing in a bank. The family has no opinion
        # about it; kwsed's bank holds both and the two must not be merged.
        "performance": "performance",
        "part": "part",
    },
)

#: Registered at import so the family's registry knows what this tool calls
#: its concepts. Registration rather than a monkeypatched module global: a
#: library cannot know the name of the program using it, and passing it in is
#: honest where patching is global.
register(TERMS)
