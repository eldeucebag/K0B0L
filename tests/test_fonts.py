#!/usr/bin/env python3
"""The bundled fonts: every theme draws its heading in its own face.

Plain script, no pytest: prints one line per check, exits non-zero on failure.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness import fonts, glyphs  # noqa: E402
from rt_harness.themes import RETRO_THEMES  # noqa: E402

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if ok:
        print(f"ok   {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f" -- {detail}" if detail else ""))


# -- the bitmap itself -----------------------------------------------------
print("-- the bitmap")
check("every glyph is 7 rows of 5 bits",
      all(len(b) == glyphs.GLYPH_H and all(len(r) == glyphs.GLYPH_W for r in b)
          for b in glyphs.GLYPHS.values()))

letters = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ") | set("0123456789")
check("the whole alphabet and the digits are drawn", letters <= set(glyphs.GLYPHS))

space = glyphs.GLYPHS[" "]
check("space draws nothing", not any("1" in r for r in space))
check("letters draw something", all(any("1" in r for r in glyphs.GLYPHS[c])
                                    for c in letters))

shapes = defaultdict(list)
for ch, bits in glyphs.GLYPHS.items():
    shapes[bits].append(ch)
same = {bits: chs for bits, chs in shapes.items() if len(chs) > 1}
check("no two characters share a shape", not same,
      f"collisions: {sorted(v for v in same.values())}")

check("a glyph is case-folded", glyphs.glyph("a") == glyphs.glyph("A"))
check("an undrawn character falls back to the box",
      glyphs.glyph("~") == glyphs.GLYPHS[glyphs.MISSING])

# -- drawing ---------------------------------------------------------------
print("-- drawing")
face = fonts.font_for("commodore-64")
lines = fonts.render("K0B0L", face)
check("a drawn word is four rows", len(lines) == 4, f"got {len(lines)}")
check("no drawn row is wider than width() says",
      all(len(r) <= fonts.width("K0B0L", face) for r in lines))
check("drawing uses only blocks and spaces",
      set("".join(lines)) <= {" ", "\u2588", "\u2580", "\u2584"},
      repr(set("".join(lines))))
check("an empty string draws nothing wide", fonts.width("", face) == 0)
check("width() counts a blank between glyphs, not a blank glyph",
      fonts.width("HH", face) == 2 * glyphs.GLYPH_W + face.spacing,
      f"{fonts.width('HH', face)} for spacing {face.spacing}")
check("two glyphs really are that far apart",
      len(fonts.render("HH", face)[0]) == fonts.width("HH", face))

wide = fonts.font_for("hotdog-3x")
check("a wider face widens the same word",
      fonts.width("OK", wide) > fonts.width("OK", fonts.font_for("beos")))

plain_bits = sum(r.count("1") for r in glyphs.glyph("A"))
bold_bits = sum(r.count("1") for r in fonts._thicken(glyphs.glyph("A")))
check("bold thickens the strokes", bold_bits > plain_bits)
check("bold draws at least as wide as plain",
      fonts.width("A", fonts.Font("x", "bold", 1, False))
      == fonts.width("A", fonts.Font("x", "plain", 1, False)))

print("-- case")
caps = fonts.render("edit-com", fonts.font_for("edit-com"))
upper = fonts.render("EDIT-COM", fonts.font_for("edit-com"))
check("a machine with no lowercase draws capitals", caps == upper)
mixed = fonts.Font("test", "plain", 1, False)
check("a face with lowercase keeps the case it is given",
      fonts.render("a", mixed) != fonts.render("A", mixed))

# -- one face per theme ----------------------------------------------------
print("-- faces")
check("every retro theme has its own face",
      set(RETRO_THEMES) <= set(fonts.FONT_FOR_THEME),
      f"missing: {sorted(set(RETRO_THEMES) - set(fonts.FONT_FOR_THEME))}")
check("no face is defined for a theme that does not exist",
      set(fonts.FONT_FOR_THEME) <= set(RETRO_THEMES),
      f"stale: {sorted(set(fonts.FONT_FOR_THEME) - set(RETRO_THEMES))}")
check("an unknown theme gets the default face",
      fonts.font_for("textual-dark") is fonts.DEFAULT_FONT
      and fonts.font_for(None) is fonts.DEFAULT_FONT)
check("theme slugs are matched case-insensitively",
      fonts.font_for("Commodore-64") is fonts.FONT_FOR_THEME["commodore-64"])

drawn = {slug: fonts.render("REDTRAM", fonts.font_for(slug)) for slug in RETRO_THEMES}
distinct = {tuple(v) for v in drawn.values()}
check("the themes do not all draw the same", len(distinct) > 1,
      f"{len(distinct)} distinct renderings")
for slug, art in drawn.items():
    check(f"{slug} draws the name in 4 rows", len(art) == 4)

# -- and the app uses them --------------------------------------------------
print("-- wiring")
source = (REPO / "rt_harness" / "textual_chat.py").read_text()
check("the chat view imports the fonts", "from . import fonts" in source
      or "import fonts" in source)
check("the splash is drawn in the active theme's face",
      "fonts.font_for(self.theme)" in source)
check("the splash is drawn before the greeting",
      source.index("self._draw_splash()") < source.index("self.ui.greet()"))
check("the splash goes to the transcript, not a new widget",
      'query_one("#transcript", Transcript)' in source.split("def _draw_splash")[1].split("def ")[0])

print()
if FAILED:
    print(f"{FAILED} check(s) failed")
    sys.exit(1)
print("all font checks passed")
