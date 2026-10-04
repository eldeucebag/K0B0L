"""A drop-down menu bar with submenus, in plain Textual.

The bar itself is a 1-line strip. Drop-down panels are *overlays*, not
siblings appended to the screen — putting them in the screen's vertical layout
pushes the transcript up and leaves a black wedge at the bottom. Instead each
panel lives on a (transparent) overlay layer, offset directly below its menu
header.

The bar is a row of labels and lays out horizontally; with the default vertical
layout only the first label fits inside the 1-line strip, every other header
reports the same (off-screen) x, and so *every* dropdown opened at the far left.
Focus is the other half of it: a menu only answers arrow keys when it has the
keyboard, so opening one takes focus and closing one hands it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


#: What the bar can do, said on the bar. Docked right, so it never pushes a
#: menu label out of the row.
BAR_HINT = "←/→ menus · ↑/↓ items · Enter pick · Esc close"


@dataclass
class MenuItem:
    label: str
    action: Callable[[], None] | None = None
    submenu: list["MenuItem"] | None = None


class MenuDropDown(Container):
    """One menu panel, overlaid on the screen's ``-menu`` layer."""

    DEFAULT_CSS = """
    MenuDropDown {
        position: absolute;
        overlay: screen;
        background: $surface-darken-1;
        border: solid $primary;
        width: 36;
        height: auto;
        padding: 0 1;
    }
    MenuDropDown .menu-item { width: 100%; padding: 0 1; }
    MenuDropDown .menu-item.-selected { background: $accent; color: $text; }
    MenuDropDown .menu-item.-disabled { color: $text-disabled; }
    """

    selected = reactive(0)

    def __init__(self, items: Sequence[MenuItem], *, on_pick=None, **kw):
        super().__init__(**kw)
        self.items = list(items)
        #: Called with the item under a click, so a menu entry can be picked
        #: with the mouse and not only with Enter.
        self.on_pick = on_pick

    def compose(self) -> ComposeResult:
        for index, item in enumerate(self.items):
            arrow = " ▶" if item.submenu else "  "
            yield Static(item.label + arrow, classes="menu-item", id=f"mi{index}")

    def on_click(self, event: events.Click) -> None:
        ident = getattr(event.widget, "id", None) or ""
        if not ident.startswith("mi") or self.on_pick is None:
            return
        try:
            index = int(ident[2:])
        except ValueError:
            return
        if 0 <= index < len(self.items):
            event.stop()
            self.on_pick(self.items[index])

    def on_mount(self) -> None:
        # A menu is a visual overlay; it never holds keyboard focus, which is
        # what made Enter appear to do nothing before.
        self.can_focus = False
        for child in self.query(".menu-item"):
            child.can_focus = False
        self._repaint()

    def _repaint(self) -> None:
        for index, widget in enumerate(self.query(".menu-item")):
            widget.set_class(index == self.selected, "-selected")

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.selected = (self.selected + delta) % len(self.items)

    def current(self) -> MenuItem | None:
        if not self.items:
            return None
        return self.items[self.selected]

    def watch_selected(self, old: int, new: int) -> None:
        self._repaint()


class MenuBar(Widget):
    """The top strip plus whatever dropdowns are open."""

    DEFAULT_CSS = """
    MenuBar {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
        /* Horizontal, or the labels stack inside a 1-line strip: only the
           first one lands on screen and every header reports the same x, so
           each dropdown opened at the far left with nothing on the bar to say
           which menus exist. */
        layout: horizontal;
    }
    MenuBar Static { width: auto; }
    MenuBar .bar-label { margin: 0 1; width: auto; }
    MenuBar .bar-label.-selected { background: $accent; color: $text; text-style: bold; }
    MenuBar #menu-hint { dock: right; margin: 0 1; color: $text-muted; }
    """

    bar_index = reactive(0)

    def __init__(self, menus: Sequence[tuple[str, list[MenuItem]]], **kw):
        super().__init__(**kw)
        self.menus = list(menus)
        self.open_stack: list[MenuDropDown] = []
        #: Who had the keyboard when a menu was opened, so closing hands it back.
        self._return_focus: Widget | None = None

    def compose(self) -> ComposeResult:
        for index, (label, _) in enumerate(self.menus):
            yield Static(label, classes="bar-label", id=f"bar{index}")
        yield Static(BAR_HINT, id="menu-hint")

    def on_mount(self) -> None:
        self.can_focus = True
        self._repaint_bar()

    def _repaint_bar(self) -> None:
        for index, widget in enumerate(self.query(".bar-label")):
            widget.set_class(index == self.bar_index, "-selected")

    def watch_bar_index(self, old: int, new: int) -> None:
        self._repaint_bar()
        if self.open_stack:
            self._open_panel(new)

    def on_click(self, event: events.Click) -> None:
        """Clicking a header opens that menu and puts the keyboard on the bar."""
        ident = getattr(event.widget, "id", None) or ""
        if not ident.startswith("bar"):
            return
        try:
            index = int(ident[3:])
        except ValueError:
            return
        if not 0 <= index < len(self.menus):
            return
        event.stop()
        self.bar_index = index
        self.open()

    # -- panels ------------------------------------------------------------
    def _open_panel(self, bar_index: int) -> None:
        for panel in self.open_stack:
            panel.remove()
        self.open_stack = [self._make_panel(self.menus[bar_index][1])]

    def _make_panel(self, items: Sequence[MenuItem],
                    *, anchor: tuple[int, int] | None = None,
                    side_of: MenuDropDown | None = None) -> MenuDropDown:
        panel = MenuDropDown(items, on_pick=self._activate)
        # position: absolute makes the offset screen-relative. Mounted on the
        # screen itself, so (header_x, 1) lands directly under the menubar
        # row and never moves with the transcript's scroll.
        self.app.screen.mount(panel)
        if anchor is not None:
            panel.styles.offset = anchor
        elif side_of is not None:
            panel.styles.offset = (side_of.region.x + side_of.region.width, side_of.region.y)
        else:
            header = self.query_one(f"#bar{self.bar_index}", Static)
            panel.styles.offset = (header.region.x, 1)
        self.open_stack.append(panel)
        return panel

    def _activate(self, item: MenuItem | None) -> None:
        if item is None:
            return
        if item.submenu:
            # Drop any submenu already open off the current panel, then open
            # this one to its right.
            if len(self.open_stack) > 1:
                self.open_stack.pop().remove()
            self._make_panel(item.submenu, side_of=self.open_stack[-1])
            return
        if item.action is not None:
            self.close_all()
            item.action()

    def close_all(self) -> None:
        for panel in self.open_stack:
            panel.remove()
        self.open_stack = []
        self._repaint_bar()
        self._give_back_focus()

    def _give_back_focus(self) -> None:
        """Hand the keyboard back to whoever held it when the menu opened."""
        target, self._return_focus = self._return_focus, None
        if target is not None and getattr(target, "is_attached", True):
            target.focus()
            return
        # Opened with the mouse (the click focused the bar itself): ask the app
        # to route focus back to the prompt.
        hook = getattr(self.app, "menu_closed", None)
        if callable(hook):
            hook()

    # -- keys -------------------------------------------------------------
    def open(self) -> None:
        """Esc path: show the current menu and take the keyboard.

        The bar has to *hold* focus: a menu that only highlights still sends
        arrows to the input, so it cannot be walked without clicking it first.
        """
        if not self.open_stack:
            focused = self.app.focused
            self._return_focus = None if focused is self else focused
            self._open_panel(self.bar_index)
        self.focus()

    def close_on_resume(self) -> None:
        """Called by the app once input focus should come back."""
        self.close_all()

    def _on_key(self, event: events.Key) -> None:
        if not self.menus_open:
            return
        key = event.key
        if key == "right":
            # Top panel: Right flips the menu. Submenu-level: Right descends.
            if len(self.open_stack) > 1:
                self._activate(self.open_stack[-1].current())
            else:
                self.bar_index = (self.bar_index + 1) % len(self.menus)
            event.stop()
        elif key == "left":
            if len(self.open_stack) > 1:
                self.open_stack.pop().remove()
            else:
                self.bar_index = (self.bar_index - 1) % len(self.menus)
            event.stop()
        elif key == "up":
            if self.open_stack:
                self.open_stack[-1].move(-1)
            event.stop()
        elif key == "down":
            if self.open_stack:
                self.open_stack[-1].move(1)
            event.stop()
        elif key == "enter":
            self._activate(self.open_stack[-1].current() if self.open_stack else None)
            event.stop()
        elif key == "escape":
            if len(self.open_stack) > 1:
                self.open_stack.pop().remove()
            else:
                self.close_all()
            event.stop()

    @property
    def menus_open(self) -> bool:
        return bool(self.open_stack)

