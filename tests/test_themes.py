"""Theme tests: are the retro themes real themes, and does each one apply?

No model and no server: this exercises the Textual/pygments wiring only.
"""
import asyncio
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rich.console import Console  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from textual.app import App  # noqa: E402
from textual.color import Color  # noqa: E402

from rt_harness import themes  # noqa: E402
from rt_harness.textual_chat import (  # noqa: E402
    SYNTAX_THEME_BY_UI,
    THEMES,
    theme_name,
)

RETRO = ["hotdog-3x", "beos", "commodore-64", "edit-com", "amber", "matrix"]
SNIPPET = "def greet(name):\n    # say hi\n    return 'hi ' + name  # 3\n"

fails: list[str] = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)


# -- the names exist everywhere they need to ---------------------------------
for name in RETRO:
    check(f"registered as a retro theme: {name}", name in themes.RETRO_THEMES)
    check(f"offered by the chat: {name}", name in THEMES)
    check(f"has a syntax theme: {name}", name in SYNTAX_THEME_BY_UI)
    check(f"is a registered Theme object: {name}",
          themes.RETRO_THEMES.get(name) is not None
          and themes.RETRO_THEMES[name].name == name)

check("retro order is the documented one", themes.RETRO_THEME_NAMES == RETRO,
      repr(themes.RETRO_THEME_NAMES))
check("every chat theme has a syntax theme",
      set(THEMES) <= set(SYNTAX_THEME_BY_UI), repr(set(THEMES) - set(SYNTAX_THEME_BY_UI)))

# -- the colours are colours, and the frame's variables all resolve -----------
for name, theme in themes.RETRO_THEMES.items():
    bad = []
    for field_name in ("primary", "foreground", "background", "surface", "panel"):
        value = getattr(theme, field_name)
        try:
            Color.parse(value)
        except Exception:  # noqa: BLE001
            bad.append(f"{field_name}={value!r}")
    check(f"declared fields parse: {name}", not bad, ", ".join(bad))

    generated = theme.to_color_system().generate()
    unparsed = []
    for key, value in generated.items():
        # Text-style slots hold keywords ("none", "underline", "initial",
        # "bold not underline"), not colours.
        if "style" in key or value in {"none", "initial", "transparent"}:
            continue
        if "auto" in value:
            continue
        candidate = value.rsplit(" ", 1)[0]  # "#00FF41 30%" is a colour + alpha
        try:
            Color.parse(candidate)
        except Exception:  # noqa: BLE001
            unparsed.append(f"{key}={value!r}")
    check(f"generated variables parse: {name}", not unparsed, ", ".join(unparsed[:4]))
    check(f"frame variables exist: {name}",
          {"text", "text-muted", "surface-lighten-1", "surface-darken-2", "border"} <= set(generated))

# -- each syntax theme really highlights, in the theme's own background -------
EXPECTED_BG = {
    "hotdog-3x": "#A80000",
    "beos": "#FFFFFF",
    "commodore-64": "#352879",
    "edit-com": "#0000AA",
    "amber": "#000000",
    "matrix": "#000000",
}
for name in RETRO:
    syntax_theme = SYNTAX_THEME_BY_UI[name]
    background = syntax_theme.get_background_style().bgcolor
    check(f"syntax background for {name}",
          background is not None and background.get_truecolor().hex.lower() == EXPECTED_BG[name].lower(),
          repr(background))
    console = Console(record=True, width=60, file=io.StringIO())
    console.print(Syntax(SNIPPET, "python", theme=syntax_theme))
    rendered = console.export_text()
    check(f"syntax renders {name}", "greet" in rendered and "return" in rendered, rendered[:60])

# -- prefix resolution for /theme --------------------------------------------
check("prefix resolves", theme_name("mat") == "matrix", repr(theme_name("mat")))
check("full name resolves", theme_name("commodore-64") == "commodore-64")
check("case is ignored", theme_name("AMBER") == "amber", repr(theme_name("AMBER")))
check("ambiguous prefix resolves to nothing", theme_name("textual-") == "",
      repr(theme_name("textual-")))
check("unknown name resolves to nothing", theme_name("nope") == "")


# -- and the app actually accepts them ---------------------------------------
class Probe(App):
    """The smallest app that registers themes the way ChatApp does."""


async def apply_all() -> None:
    app = Probe()
    themes.register(app)
    async with app.run_test() as _pilot:
        for name in RETRO:
            app.theme = name
            check(f"app applies {name}", app.current_theme.name == name,
                  repr(app.current_theme.name))
            check(f"app serialises {name}", isinstance(app.export_screenshot(), str))


asyncio.run(apply_all())

print()
print(f"{len(fails)} FAIL" if fails else "all ok")
sys.exit(1 if fails else 0)
