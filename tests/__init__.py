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

"""The test suite.

This file exists so that `tests` is a **package**, which is not a matter of
taste. Without it mypy names each module after its file -- `test_app`, not
`tests.test_app` -- and a `module = ["tests.*"]` override in pyproject.toml
is then silently *accepted* as matching and never applied, with no "unused
section" note to say so. A per-module list of every test file instead works
but has to be edited each time a test file is added, which is the kind of
bookkeeping that quietly stops happening.

With this file present mypy names the modules `tests.test_app` and the
wildcard does what it says.
"""
