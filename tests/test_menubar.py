"""Menu bar tests: does the bar show what it offers, and does Esc take the keys?

No model and no server. Drives a real Textual app headlessly (``run_test``) with
the real ``MenuBar``, because both bugs this guards against are layout/focus
bugs: every header has to sit *on* the bar row at its own x (or all the
dropdowns open at the far left), and opening a menu has to move the keyboard
onto the bar (or the input keeps eating the arrow keys and the menu can only be
worked with the mouse).
"""
import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import Input, Static  # noqa: E402

from rt_harness.menubar import BAR_HINT, MenuBar, MenuItem  # noqa: E402

#: Text runs in the SVG screenshot Textual can export: (x, y, text).
SVG_RUN = re.compile(r'<text[^>]*x="([\d.]+)"[^>]*y="([\d.]+)"[^>]*>(.*?)</text>')

fails: list[str] = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)


PICKS: list[str] = []


def menus() -> list[tuple[str, list[MenuItem]]]:
    return [
        ("Session", [MenuItem("New", action=lambda: PICKS.append("new")),
                     MenuItem("Save", action=lambda: PICKS.append("save"))]),
        ("Run", [MenuItem("Start", action=lambda: PICKS.append("start"))]),
        ("View", [MenuItem("Theme", submenu=[MenuItem("amber", action=lambda: PICKS.append("amber"))])]),
        ("Help", [MenuItem("Keys", action=lambda: PICKS.append("keys"))]),
    ]


class BarApp(App):
    """The smallest app with the same Esc contract as ChatApp."""

    def compose(self) -> ComposeResult:
        yield MenuBar(menus(), id="menubar")
        yield Input(id="input")

    def _on_key(self, event) -> None:
        bar = self.query_one("#menubar", MenuBar)
        if event.key == "escape":
            if bar.menus_open:
                bar.close_all()
            else:
                bar.open()
            event.stop()

    def menu_closed(self) -> None:
        """Same contract ChatApp offers: where focus goes when a menu closes."""
        self.query_one("#input", Input).focus()

    def on_mount(self) -> None:
        self.query_one("#input", Input).focus()


async def main() -> None:
    app = BarApp()
    async with app.run_test(size=(96, 24)) as pilot:
        await pilot.pause()
        bar = app.query_one("#menubar", MenuBar)
        labels = list(bar.query(".bar-label"))
        headers = [bar.query_one(f"#bar{i}", Static) for i in range(len(menus()))]

        # -- what the bar offers is on the bar -----------------------------
        check("one header per menu", len(headers) == len(menus()), str(len(headers)))
        check("bar is one line tall", bar.region.height == 1, str(bar.region))
        check("every header has width",
              all(h.region.width >= len(label) for h, label in zip(headers, [m[0] for m in menus()])),
              str([h.region for h in headers]))
        check("every header shares the bar row",
              all(h.region.y == bar.region.y for h in headers),
              str([(h.region.x, h.region.y) for h in headers]))
        check("headers do not overlap",
              all(headers[i].region.right <= headers[i + 1].region.x for i in range(len(headers) - 1)),
              str([(h.region.x, h.region.right) for h in headers]))
        check("headers are inside the bar",
              all(bar.region.x <= h.region.x and h.region.right <= bar.region.right for h in headers),
              str((bar.region, [(h.region.x, h.region.right) for h in headers])))
        check("no header is clipped away",
              all(h.region.width > 0 and h.display for h in headers))
        hint = bar.query_one("#menu-hint", Static)
        check("bar says how to drive it", "Esc" in BAR_HINT and hint.region.width > 0,
              f"{BAR_HINT!r} {hint.region}")
        check("hint sits on the bar row", hint.region.y == bar.region.y, str(hint.region))
        check("hint is right of the headers", hint.region.x > headers[-1].region.x,
              f"{hint.region.x} vs {headers[-1].region.x}")

        # -- and the painted row really carries them -----------------------
        # The layout above is what the compositor uses; this is what a terminal
        # would show. Before the bar was laid out horizontally the labels
        # stacked, so no single painted row held all four names.
        rows: dict[float, str] = {}
        for x, y, chunk in SVG_RUN.findall(app.export_screenshot()):
            key = round(float(y), 1)
            rows[key] = rows.get(key, "") + chunk
        painted = [line for line in rows.values()
                   if all(name in line for name, _ in menus())]
        check("the painted bar row carries every menu name", len(painted) == 1,
              repr(sorted(rows.values())[:6]))

        # -- Esc takes the keyboard and opens the current menu -------------
        check("bar is not focused at rest", app.focused is app.query_one("#input"))
        await pilot.press("escape")
        await pilot.pause()
        check("Esc opens the menu", bar.menus_open, str(bar.open_stack))
        check("Esc puts the keyboard on the bar", app.focused is bar,
              repr(getattr(app.focused, "id", app.focused)))
        panel = bar.open_stack[0]
        check("panel opens under its own header",
              panel.region.x == headers[0].region.x,
              f"panel {panel.region} header {headers[0].region}")
        check("panel opens below the bar", panel.region.y == bar.region.height,
              str(panel.region))

        # -- arrows walk the bar, and the panel follows --------------------
        await pilot.press("right")
        await pilot.pause()
        check("right moves to the next menu", bar.bar_index == 1, str(bar.bar_index))
        check("panel follows the header",
              bar.open_stack[0].region.x == headers[1].region.x,
              f"{bar.open_stack[0].region} vs {headers[1].region}")
        await pilot.press("left")
        await pilot.pause()
        check("left moves back", bar.bar_index == 0, str(bar.bar_index))
        await pilot.press("right")
        await pilot.pause()

        # -- down + enter picks, and the prompt gets the keys back ---------
        await pilot.press("down")
        await pilot.pause()
        check("down moves the highlight", bar.open_stack[0].selected == 0,
              str(bar.open_stack[0].selected))
        await pilot.press("enter")
        await pilot.pause()
        check("enter runs the item", PICKS == ["start"], repr(PICKS))
        check("picking closes the menu", not bar.menus_open, str(bar.open_stack))
        check("picking returns the keyboard to the prompt",
              app.focused is app.query_one("#input"),
              repr(getattr(app.focused, "id", app.focused)))

        # -- Esc closes and hands the keys back ----------------------------
        await pilot.press("escape")
        await pilot.pause()
        check("Esc reopens", bar.menus_open)
        await pilot.press("escape")
        await pilot.pause()
        check("second Esc closes", not bar.menus_open, str(bar.open_stack))
        check("closing returns the keyboard to the prompt",
              app.focused is app.query_one("#input"),
              repr(getattr(app.focused, "id", app.focused)))

        # -- the mouse still works, and now picks items too ----------------
        await pilot.click("#bar3")
        await pilot.pause()
        check("click opens that menu", bar.bar_index == 3 and bar.menus_open,
              f"{bar.bar_index} {bar.menus_open}")
        check("clicked menu opens under its header",
              bar.open_stack[0].region.x == headers[3].region.x,
              f"{bar.open_stack[0].region} vs {headers[3].region}")
        check("click focuses the bar", app.focused is bar,
              repr(getattr(app.focused, "id", app.focused)))
        await pilot.click("#mi0")
        await pilot.pause()
        check("click picks the item", PICKS == ["start", "keys"], repr(PICKS))
        check("click closes and returns the keyboard",
              not bar.menus_open and app.focused is app.query_one("#input"),
              f"{bar.open_stack} {getattr(app.focused, 'id', app.focused)}")

        # -- a submenu opens to the right of its parent --------------------
        await pilot.click("#bar2")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        check("submenu opens beside its parent", len(bar.open_stack) == 2, str(bar.open_stack))
        if len(bar.open_stack) == 2:
            parent, child = bar.open_stack
            check("submenu is to the right",
                  child.region.x >= parent.region.right, f"{child.region} {parent.region}")
        await pilot.press("escape")
        await pilot.pause()
        check("Esc backs out of a submenu only", len(bar.open_stack) == 1,
              str(bar.open_stack))


asyncio.run(main())
print()
if fails:
    print(f"{len(fails)} FAILED: " + ", ".join(fails))
    raise SystemExit(1)
print("menu bar ok")
