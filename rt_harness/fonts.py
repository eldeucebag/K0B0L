"""Each theme's typeface: drawn from the bundled bitmap, not installed.

A terminal application cannot set the terminal's own font — that lives in the
emulator, not in us. What it can do is draw its headings from the bundled 5x7
bitmap in :mod:`rt_harness.glyphs`, which is how a theme gets a typeface of
its own: commodore-64 and edit-com draw the thick strokes of the machines'
8x8 charsets, amber and matrix a thin terminal face, beos a lighter face with
wide spacing, hotdog-3x the chunky system font of its era.

Headings only. Chat text stays in the terminal's own font at its own size:
styling a transcript we need to *read* at length would trade legibility for
decoration, and legibility wins.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import glyphs

#: A pair of stacked pixels -> the half-block that draws that pair.
_CELL = {(1, 1): "\u2588", (1, 0): "\u2580", (0, 1): "\u2584", (0, 0): " "}


@dataclass(frozen=True)
class Font:
    """How a theme draws the bitmap: weight, rhythm, and case."""

    name: str      # the machine's face this evokes
    weight: str    # "plain" | "bold"
    spacing: int   # blank columns between glyphs
    caps: bool     # the machine had no lowercase


def _thicken(bits: tuple[str, ...]) -> tuple[str, ...]:
    """Bold: every lit pixel also lights the pixel to its right."""
    out = []
    for row in bits:
        out.append("".join(
            "1" if row[i] == "1" or (i and row[i - 1] == "1") else "0"
            for i in range(len(row))
        ))
    return tuple(out)


def render(text: str, font: Font) -> list[str]:
    """Draw ``text`` in ``font``: the bitmap as rows of half-blocks.

    Seven pixel rows become four text rows at two pixels per cell, which keeps
    the letterforms roughly square in a terminal cell's 1:2 aspect.
    """
    if font.caps:
        text = text.upper()
    pad = ("0" * font.spacing,) * glyphs.GLYPH_H if font.spacing else None
    seq: list[tuple[str, ...]] = []
    for ch in text:
        if seq and pad is not None:
            seq.append(pad)
        bits = glyphs.glyph(ch)
        seq.append(_thicken(bits) if font.weight == "bold" else bits)
    lines: list[str] = []
    for top in range(0, glyphs.GLYPH_H, 2):
        cells: list[str] = []
        for bits in seq:
            upper = bits[top]
            lower = bits[top + 1] if top + 1 < glyphs.GLYPH_H else "0" * glyphs.GLYPH_W
            cells.append("".join(
                _CELL[(int(upper[i]), int(lower[i]))] for i in range(len(upper))
            ))
        lines.append("".join(cells).rstrip())
    while lines and not lines[-1].strip():
        lines.pop()
    return lines or [""]


def width(text: str, font: Font) -> int:
    """How many terminal cells :func:`render` uses to draw ``text``."""
    if not text:
        return 0
    return len(text) * glyphs.GLYPH_W + (len(text) - 1) * font.spacing


#: A face per theme: switching theme changes the lettering, not only the paint.
FONT_FOR_THEME: dict[str, Font] = {
    "hotdog-3x": Font("system-bold", "bold", 2, True),
    "beos": Font("swiss", "plain", 2, False),
    "commodore-64": Font("petscii", "bold", 1, True),
    "edit-com": Font("cp437", "bold", 1, True),
    "amber": Font("vt220", "plain", 1, False),
    "matrix": Font("vt220", "plain", 1, False),
}

#: Stock Textual themes, and anything unrecognised, get the plain face.
DEFAULT_FONT = Font("swiss", "plain", 1, False)


def font_for(theme: str | None) -> Font:
    """The face for a theme slug; unknown themes get :data:`DEFAULT_FONT`."""
    if not theme:
        return DEFAULT_FONT
    return FONT_FOR_THEME.get(str(theme).strip().lower(), DEFAULT_FONT)
