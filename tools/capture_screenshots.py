#!/usr/bin/env python3
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

"""Capture ``docs/screenshots`` from the browser in demo mode.

Drives the Textual app headlessly through ``App.run_test``, exports each
screen with ``App.export_screenshot`` and converts the SVG to PNG when the
optional ``cairosvg`` converter is installed. No MIDI port is opened and
no live-names file is written: the device is the demo and ``live_path``
is None.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from cairosvg import svg2png
except ImportError:  # pragma: no cover - environment check
    svg2png = None

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from kws import banks  # noqa: E402
from kws import catalog as cat  # noqa: E402
from kws import scan as _scan  # noqa: E402
from kws.multi import Multiset, Part  # noqa: E402
from kwsed.app import (  # noqa: E402
    KwsedApp,
    SendScreen,
)
from kwsed.demo import DemoDevice  # noqa: E402
from vinsynlib.ui.modals import RatingFilter, RatingFilterScreen  # noqa: E402
from vinsynlib.ui.report import ReportScreen  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "docs" / "screenshots"
SCREEN_W = 820
SCREEN_NAMES = [
    "browser",
    "send",
    "scan-offer",
    "scan",
    "rating-filter",
    "reports",
]


def screen_names() -> list[str]:
    """The screens this tool captures, in order."""
    return list(SCREEN_NAMES)


def cairosvg_available() -> bool:
    """Whether the optional PNG converter is importable."""
    return svg2png is not None


def svg_filename(name: str) -> str:
    """Filename for a screen's SVG export."""
    return f"{name}.svg"


def png_filename(name: str) -> str:
    """Filename for a screen's PNG export."""
    return f"{name}.png"


def screenshot_path(directory: Any, name: str) -> Path:
    """Path to a screen's SVG export inside ``directory``."""
    return Path(directory) / svg_filename(name)


@dataclass(frozen=True)
class Output:
    """Where a shot goes and how wide its PNG is."""

    directory: Path
    width: int
    svg_only: bool


def fix_svg(svg: str) -> str:
    """Terminal font, no remote fonts, no window chrome."""
    svg = svg.replace(
        "font-family: Fira Code, monospace",
        'font-family: "DejaVu Sans Mono", monospace',
    )
    svg = re.sub(r"@font-face\s*\{[^}]+\}", "", svg)
    svg = re.sub(r"<circle[^>]*/>", "", svg)
    svg = re.sub(r"<circle[^>]*>.*?</circle>", "", svg)
    svg = re.sub(r'<text[^>]*class="[^"]*-title"[^>]*>.*?</text>', "", svg)
    return svg


def screen_text(svg: str) -> str:
    """The visible text of a shot."""
    text = re.sub(r"<style.*?</style>", "", svg, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def invented_catalog() -> cat.Catalog:
    """A small catalog of invented names."""
    return cat.Catalog(
        [
            cat.Entry("RAM1", 0, "Book Name", banks.Kind.PERFORMANCE),
            cat.Entry("RAM1", 1, "Rusted Pad", banks.Kind.PERFORMANCE),
            cat.Entry("RAM1", 2, "Amber Keys", banks.Kind.PERFORMANCE),
        ],
        source="demo (invented names)",
    )


def make_app(catalog: cat.Catalog) -> KwsedApp:
    """One fresh browser over the demo, with no MIDI port and no cache."""
    return KwsedApp(DemoDevice(), catalog=catalog, live_path=None)


async def wait_for_text(
    app: KwsedApp, pilot: Any, marker: str, tries: int = 200
) -> None:
    """Poll the visible text until it contains ``marker``."""
    for _ in range(tries):
        if marker.lower() in screen_text(app.export_screenshot()).lower():
            return
        await pilot.pause()
    raise AssertionError(f"never saw {marker!r} on screen")


async def wait_for_screen(
    app: KwsedApp, pilot: Any, cls: type, tries: int = 200
) -> None:
    """Poll until the top screen is an instance of ``cls``."""
    for _ in range(tries):
        if isinstance(app.screen, cls):
            return
        await pilot.pause()
    raise AssertionError(
        f"top screen is {type(app.screen).__name__}, not {cls.__name__}"
    )


async def capture(app: KwsedApp, pilot: Any, name: str, output: Output) -> str:
    """Export, fix, write the SVG and (if possible) the PNG."""
    await pilot.pause()
    svg = fix_svg(app.export_screenshot())
    out_svg = output.directory / svg_filename(name)
    out_svg.write_text(svg, encoding="utf-8")
    if output.svg_only or svg2png is None:
        print(f"  {svg_filename(name)} ({out_svg.stat().st_size // 1024} KB)")
    else:
        out_png = output.directory / png_filename(name)
        svg2png(
            bytestring=svg.encode(),
            write_to=str(out_png),
            output_width=output.width,
        )
        print(f"  {png_filename(name)} ({out_png.stat().st_size // 1024} KB)")
    return svg


def check(name: str, svg: str, markers: list[str]) -> None:
    """Every marker must be present in the shot."""
    text = screen_text(svg).lower()
    missing = [marker for marker in markers if marker.lower() not in text]
    if missing:
        raise AssertionError(
            f"{name}: marker(s) {missing} not in shot text:\n{text[:900]}"
        )
    print(f"  markers ok: {markers}")


async def shot_browser(app: KwsedApp, pilot: Any) -> None:
    """The two-pane browser."""
    await pilot.pause()
    await wait_for_text(app, pilot, "Book Name")


def _make_multisets() -> list[Multiset]:
    sets: list[Multiset] = []
    for i in range(2):  # keep small for screenshot
        parts: list[Part] = []
        for ch in range(16):
            parts.append(
                Part(
                    channel=ch + 1,
                    bank=0,
                    performance=ch % 50,
                    level=100,
                    flag=1 if ch < 15 else 0,
                )
            )
        sets.append(Multiset(number=i, name=f"Multi {i:02d}", parts=tuple(parts)))
    if not sets:
        sets.append(Multiset(number=0, name="Multi 00", parts=()))
    return sets


async def shot_send(app: KwsedApp, pilot: Any) -> None:
    """The send/write screen."""
    app.push_screen(
        SendScreen(
            path="demo.syx",
            kind=banks.Kind.PATCH,
            destination=banks.bank(0),
            current={0: "Book Name"},
            on_send=None,
        )
    )
    await wait_for_screen(app, pilot, SendScreen)


async def shot_scan_offer(app: KwsedApp, pilot: Any) -> None:
    """The scan offer screen."""
    app.push_screen(_scan.ScanOfferScreen(_scan.SCAN_OFFER))
    await wait_for_screen(app, pilot, _scan.ScanOfferScreen)


async def shot_scan(app: KwsedApp, pilot: Any) -> None:
    """The scan progress screen."""
    cancel = threading.Event()
    app.push_screen(_scan.ScanScreen(cancel))
    await wait_for_screen(app, pilot, _scan.ScanScreen)
    screen = app.screen
    if isinstance(screen, _scan.ScanScreen):
        screen.set_progress("scanned RAM 1 performance")


async def shot_rating_filter(app: KwsedApp, pilot: Any) -> None:
    """The rating filter screen."""
    app.push_screen(RatingFilterScreen(RatingFilter(stars=3, at_least=True)))
    await wait_for_screen(app, pilot, RatingFilterScreen)


async def shot_reports(app: KwsedApp, pilot: Any) -> None:
    """The reports/help screen."""
    app.action_help()
    await wait_for_screen(app, pilot, ReportScreen)


MARKERS: dict[str, list[str]] = {
    "browser": ["Book Name"],
    "send": ["Write a bank"],
    "scan-offer": ["no name catalog"],
    "scan": ["Scanning"],
    "rating-filter": ["Rating filter"],
    "reports": ["kwsed"],
}

SHOT_FUNCTIONS: dict[str, Callable[[KwsedApp, Any], Awaitable[None]]] = {
    "browser": shot_browser,
    "send": shot_send,
    "scan-offer": shot_scan_offer,
    "scan": shot_scan,
    "rating-filter": shot_rating_filter,
    "reports": shot_reports,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="capture screenshots")
    parser.add_argument(
        "--out",
        "--output-dir",
        dest="out",
        default=str(DEFAULT_OUT),
        metavar="DIR",
        help=f"where the shots go (default: {DEFAULT_OUT})",
    )
    png_group = parser.add_mutually_exclusive_group()
    png_group.add_argument(
        "--png",
        dest="png",
        action="store_true",
        default=None,
        help="write PNGs (default when cairosvg is available)",
    )
    png_group.add_argument(
        "--no-png",
        dest="png",
        action="store_false",
        default=None,
        help="skip PNG conversion",
    )
    parser.add_argument(
        "--list",
        dest="list_screens",
        action="store_true",
        help="print the screen names and exit",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=SCREEN_W,
        metavar="PX",
        help=f"PNG width in pixels (default: {SCREEN_W})",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.list_screens:
        for name in screen_names():
            print(name)
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    png = args.png
    if png is None:
        png = cairosvg_available()
    output = Output(out_dir, args.width, not (png and cairosvg_available()))

    for name in screen_names():
        app = make_app(invented_catalog())
        print(f"--- {name} ---")
        async with app.run_test(size=(120, 40)) as pilot:
            await SHOT_FUNCTIONS[name](app, pilot)
            svg = await capture(app, pilot, name, output)
        check(name, svg, MARKERS[name])
        print(screen_text(svg)[:900])
        print()

    print(f"Done. Screenshots in {out_dir}")
    if svg2png is None:
        print("PNG needs the optional cairosvg converter; SVGs written.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.width < 1:
        raise SystemExit("error: --width must be at least 1")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
