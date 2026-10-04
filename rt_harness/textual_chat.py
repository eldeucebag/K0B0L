"""The chat front end, on Textual.

The engine and hooks are unchanged from the prompt_toolkit front end. The
chrome around it is real now: a MenuBar with submenus instead of a
filterable palette, modal screens for help/paste/picker/wizard, themes that
change the whole frame, and Rich-syntax highlighting for fenced code the
model prints.

Threading: the engine's turn runs on a daemon thread; everything the UI
touches goes through the App thread via _dispatch, which compares against the
thread Textual mounted us on.
"""

from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import (
    DirectoryTree,
    Footer,
    Input,
    Label,
    RichLog,
    Static,
    TextArea,
)
from textual.suggester import Suggester

from . import fonts, semantic, themes as retro_themes
from .chat import TurnStats
from .client import OllamaClient, OllamaError, OpenAIClient
from .menubar import MenuBar, MenuItem
from .tools import TOOL_VERBOSITY, ToolResult, summarize_call
from .tui import ChatUI

HELP_TEXT = """keys
  Esc          open/close the menu bar, or close a dialog
  F1           this panel
  F2           open the paste pad
  F3           open the base-prompt picker (stages /run start ofile=…)
  F4           open the run-start wizard
  Ctrl-Q       leave
  Up/Down      prompt history when the prompt is focused; menu items when the
               menu is open
  ←/→          menu headers when a menu is open
  PageUp/Down  scroll the transcript
  Tab          from a wizard prompt, pick the current field from a list

commands
  /help              this list
  /tools <level>     off | summary | full
  /think on|off      reasoning blocks: unfolded, or folded away (off)
  /fold [what]       fold reasoning|code|all blocks away (default all)
  /unfold [what]     open them again; a click on a block header does one
  /theme [name]      switch the UI theme (list with no arg)
  /semantic on|off   colour data in the transcript (paths, commands, verdicts…)
  /read <path>       print a file into the chat
  /run ...           start|status|tail|stop a cycle run
  /paste             open the paste pad
  /model [name]      show or switch the chat model
  /models            list the models the endpoint reports
  /target [name]     show or set the next run's target
  /root [path]       show or change the workspace root
  /clear             clear the conversation
  /history           messages and tool-call counts
  /exit              leave

run syntax
  /run start [target=MODEL] [modes=a,b] [attempts=N] [family=NAME] <objective>

themes
  textual-dark, textual-light, nord, gruvbox, catppuccin-mocha, monokai,
  solarized-dark, tokyo-night, hotdog-3x, beos, commodore-64, edit-com,
  amber, matrix

  /theme takes a prefix, so /theme matrix and /theme mat are the same theme.
"""

THEMES = [
    "textual-dark", "textual-light", "nord", "gruvbox", "catppuccin-mocha",
    "monokai", "solarized-dark", "tokyo-night",
] + retro_themes.RETRO_THEME_NAMES
SYNTAX_THEME_BY_UI = {
    "textual-dark": "monokai",
    "textual-light": "default",
    "nord": "nord",
    "gruvbox": "gruvbox-dark",
    "catppuccin-mocha": "monokai",
    "monokai": "monokai",
    "solarized-dark": "solarized-dark",
    "tokyo-night": "monokai",
}
SYNTAX_THEME_BY_UI.update(retro_themes.RETRO_SYNTAX)


def theme_name(prefix: str) -> str:
    """Resolve a theme prefix to a full name, or ``""``.

    ``/theme`` is typed by hand, and `commodore-64` is a lot of keys; a unique
    prefix is enough. An ambiguous prefix resolves to nothing so the caller can
    say so rather than pick one silently.
    """
    wanted = prefix.strip().lower()
    if wanted in THEMES:
        return wanted
    matches = [name for name in THEMES if name.startswith(wanted)]
    return matches[0] if len(matches) == 1 else ""


def split_fence(chunk: str) -> tuple[str, str]:
    """A fenced chunk -> ``(info string, code)``.

    Markdown says the first line inside a fence is the info string, so that is
    the rule here: a newline before any code makes the first line a tag, and a
    chunk with no newline in it is all code -- unless that lone word is a
    language pygments knows, in which case the fence was a tag with no body
    (`` ```python``` ``) and drawing it would put the word "python" in the
    transcript as if it were code. The first line is passed on whole:
    ``python {linenos=table}`` is a tag followed by options, and
    :func:`resolve_language` is what knows to read only the first word of it.
    """
    head, newline, rest = chunk.partition("\n")
    if not newline:
        word = chunk.strip()
        if word and " " not in word:
            try:
                get_lexer_by_name(word)
            except ClassNotFound:
                pass
            else:
                return word, ""
        return "", chunk
    return head.strip(), rest


def _sniff(code: str) -> str:
    """Name the language of an untagged block, or ``""`` if it is not obvious.

    pygments ships a classifier and it is deliberately not used here. On a
    twenty-line JavaScript block it answers "Python"; on a shell script,
    "scdoc". A wrong lexer is worse than no lexer -- it colours correct code
    incorrectly and names it wrongly in the header -- so only signals that are
    proof rather than opinion are allowed to name a block: a shebang, an XML
    or HTML opening tag, a diff header, or a body that parses as JSON.
    """
    head = code.lstrip()
    first = head.split("\n", 1)[0].strip()
    if first.startswith("#!"):
        if "python" in first:
            return "python"
        if any(shell in first for shell in ("/sh", "/bash", "/zsh", "/ksh", "env sh")):
            return "bash"
    if first.startswith("<?xml"):
        return "xml"
    if first.lower().startswith(("<!doctype html", "<html")):
        return "html"
    if first.startswith(("diff --git", "@@ -")):
        return "diff"
    if head.startswith(("{", "[")):
        try:
            json.loads(code)
        except ValueError:
            pass
        else:
            return "json"
    return ""


def resolve_language(tag: str, code: str) -> str:
    """The pygments lexer alias to highlight a fenced block with.

    Three cases, and the difference between them is who gets to decide. A tag
    pygments knows is the author's own choice and is honoured -- note that only
    the first word counts, since a fence commonly carries options after the
    language (``python {linenos=table}``, which is otherwise a tag pygments
    has never heard of and a block that quietly arrives uncoloured). A tag
    pygments does *not* know is still a choice, so the block stays plainly
    uncoloured rather than being re-interpreted. With no tag at all there is
    nothing to contradict, and :func:`_sniff` gets to guess at proof-grade
    signals only.
    """
    alias = tag.split()[0] if tag.strip() else ""
    if alias:
        try:
            get_lexer_by_name(alias)
        except ClassNotFound:
            return ""
        return alias
    return _sniff(code)


class HistoryInput(Input):
    """An Input with Up/Down walked through a persistent history file."""

    path = retro_themes.migrate_state(Path.home() / ".k0b0l-chat-history")

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._history: list[str] = []
        self._index = 0
        try:
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    self._history.append(line)
        except OSError:
            pass
        self._index = len(self._history)

    async def _on_key(self, event: Any) -> None:
        if event.key == "up":
            if self._index > 0:
                self._index -= 1
                self.value = self._history[self._index]
                self.cursor_position = len(self.value)
            event.stop()
        elif event.key == "down":
            if self._index < len(self._history) - 1:
                self._index += 1
                self.value = self._history[self._index]
            else:
                self._index = len(self._history)
                self.value = ""
            event.prevent_default()
            event.stop()

    def note_submitted(self, text: str) -> None:
        self._history.append(text)
        self._index = len(self._history)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError:
            pass


class SuggestorForChat(Suggester):
    """Context-aware suggestions for the chat prompt."""

    def __init__(self, config: Any, root: Path) -> None:
        self.config = config
        self.root = Path(root)
        self._models_at = 0.0
        self._models: list[str] = []
        super().__init__()

    def _models_now(self) -> list[str]:
        import time

        from . import catalog

        now = time.monotonic()
        if now - self._models_at > 30:
            self._models_at = now
            self._models = catalog.known_models(self.config)
        return self._models

    async def get_suggestion(self, value: str) -> Any:
        text = value
        words = text.split()
        if not words:
            return None
        if text.startswith("/") and len(words) == 1:
            # A word that is already a whole command needs no completion, and
            # suggesting a longer command that merely starts with it (/models
            # for /model) would hijack the keypress.
            if words[0] in _COMMANDS:
                return None
            for cmd in _COMMANDS:
                if cmd.startswith(words[0]) and cmd != words[0]:
                    return cmd[len(words[0]):]
            return None
        head = words[-1]
        if words[0] in ("/model", "/target"):
            for name in self._models_now():
                if name.startswith(head) and name != head:
                    return name[len(head):]
        if words[0] == "/theme":
            for name in THEMES:
                if name.startswith(head) and name != head:
                    return name[len(head):]
        if words[0] == "/run" and len(words) >= 2:
            if head.startswith("modes="):
                from . import catalog

                for name, _ in catalog.mode_choices():
                    if name.startswith(head[6:]) and name != head[6:]:
                        return name[len(head[6:]):]
            if head.startswith("family="):
                from . import catalog

                for family in catalog.deployment_choices():
                    if family.startswith(head[7:]) and family != head[7:]:
                        return family[len(head[7:]):]
            for token in ("start", "status", "tail", "stop"):
                if len(words) == 2 and token.startswith(head) and token != head:
                    return token[len(head):]
        if "/" in head:
            try:
                directory = (self.root / head).parent if "/" in head else self.root
                prefix = Path(head).name
                if directory.is_dir():
                    for entry in sorted(directory.iterdir()):
                        name = entry.name + ("/" if entry.is_dir() else "")
                        if name.startswith(prefix) and name != prefix:
                            return name[len(prefix):]
            except OSError:
                return None
        return None


_COMMANDS = ["/help", "/tools", "/think", "/read", "/run", "/paste",
             "/model", "/models", "/target", "/root", "/clear", "/history",
             "/exit", "/theme", "/semantic", "/skills", "/session", "/docs",
             "/soul"]

#: One-line descriptions for the Ctrl-P command dispatch. Filled by texting.
COMMAND_INFO = {
    "/help": "show this help",
    "/tools": "tool output level",
    "/think": "reasoning blocks: unfolded or folded away",
    "/fold": "fold reasoning|code|all blocks away",
    "/unfold": "open folded reasoning|code|all blocks",
    "/read": "print a file into the chat",
    "/run": "spawn/stop a cycle run",
    "/paste": "open the multiline paste pad",
    "/model": "switch the chat model",
    "/models": "list the models the server reports",
    "/target": "set the next run's target model",
    "/root": "change the workspace root",
    "/clear": "clear the conversation",
    "/history": "show message counts",
    "/exit": "leave the chat",
    "/theme": "switch the UI theme",
    "/semantic": "colour data in the transcript",
    "/skills": "manage prompt-injection skills",
    "/session": "save or load a chat session",
    "/docs": "list or read the product docs",
    "/soul": "show the soul file in force",
}


@dataclass
class Block:
    """One thing that arrived in the transcript.

    Blocks are kept as *source*, not just as the strips they produced, because
    folding one changes the height of everything below it: the widget has to be
    able to draw the whole transcript again from the top.

    ``theme`` is the UI theme in force when the block arrived. Resolving colour
    from it at draw time (rather than at arrival) is what lets a fold
    re-render the transcript without repainting history in a newer theme.
    """

    kind: str
    body: str
    language: str = ""
    theme: str = ""
    #: Only the kinds in ``themes.BLOCK_KINDS`` ever fold; prose never does.
    folded: bool = False


class Transcript(RichLog):
    """Scrollback that colours fenced code blocks via rich Syntax.

    Prose is not left plain either: it goes through ``rt_harness.semantic``, so
    a path, a command, a model tag or a verdict carries its own colour — the
    one the active theme gives that kind of datum. Colour is resolved when the
    line is written, so a line keeps the theme it arrived under.

    Reasoning and code are kept as *blocks*: each paints a background of its
    own (the theme decides the colour) and each can be folded to a one-line
    header, which is how a transcript stays readable when the model thinks at
    length. Reasoning arrives folded, because the thinking is the working and
    not the answer; code arrives unfolded, because it was asked for.

    The inference overlay is painted here, into this widget's own strips, and
    not by a widget stacked on top of it. Textual's compositor cuts an
    overlapping widget's whole region out of whatever sits below it, so a
    floating panel can only ever *hide* the chat behind it — a see-through
    overlay is not something the layer system can express. Overriding
    ``render_line`` puts the glyphs on the text's own cells instead.
    """

    def __init__(self, ui: "TextualChatUI", **kw: Any) -> None:
        super().__init__(wrap=True, highlight=False, markup=True, **kw)
        self.ui = ui
        #: The falling-glyph grid while the model generates; None when idle.
        self.rain: Any = None
        self._rain_timer: Any = None
        #: Every block in order. A fold re-renders the log from these.
        self.blocks: list[Block] = []
        #: Per block, the half-open row range it occupies in ``self.lines``.
        #: Kept in step with ``blocks`` by drawing, which is what lets a click
        #: on a row be traced back to the block that owns it.
        self._spans: list[tuple[int, int]] = []
        #: Pending resize re-render, so a drag does not redraw per frame.
        self._resize_timer: Any = None

    def start_rain(self, rain: Any) -> None:
        """Take over this widget's rows with ``rain`` until stopped."""
        if rain is None:
            # Themes without an overlay (everything but matrix) call through
            # here too, so a None is a no-op rather than an error.
            self.stop_rain()
            return
        self.rain = rain
        if self._rain_timer is None:
            self._rain_timer = self.set_interval(rain.interval, self._rain_tick)

    def stop_rain(self) -> None:
        if self._rain_timer is not None:
            self._rain_timer.stop()
            self._rain_timer = None
        self.rain = None
        self.refresh()

    def _rain_tick(self) -> None:
        if self.rain is None:
            return
        self.rain.tick(self.size.width, self.size.height)
        # Targeted refresh: the glyphs land on cells that already hold text, so
        # the widget has to be re-rendered every frame of the animation.
        self.refresh()

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        if self.rain is None:
            return strip
        return self.rain.overlay(strip, y)

    # -- geometry ----------------------------------------------------------

    def band_width(self) -> int:
        """The width a block band is painted to: the visible text area.

        A band is padded out to this width so a block reads as a slab rather
        than as a stripe that stops wherever its longest line happens to end.
        ``write`` is handed the same number, so the padding never wraps.
        """
        width = self.scrollable_content_region.width
        if width <= 0:
            # Before the first layout there is no region to measure, and
            # min_width is what RichLog would render at regardless.
            width = self.min_width
        return max(8, width)

    def on_resize(self, event: Any) -> None:
        """Re-pad the bands once a resize settles.

        The padding is baked into the text, so a narrower window needs every
        band drawn again or the old width wraps. Debounced, because a drag
        would otherwise redraw the transcript on every frame.
        """
        del event
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.25, self._after_resize)

    def _after_resize(self) -> None:
        self._resize_timer = None
        if any(block.kind in retro_themes.BLOCK_KINDS for block in self.blocks):
            self.rerender()

    # -- drawing -----------------------------------------------------------

    def _pad(self, text: Text, width: int, paint: str) -> Text:
        """Pad ``text`` out to ``width`` and paint ``paint`` across each line.

        The padding is what makes a band reach the right edge: rich paints a
        style over the cells it is given and the log fills the rest of the row
        with nothing. ``paint`` goes on *last*, so it wins on background while
        leaving the foreground each span already picked alone.
        """
        out = Text()
        for index, line in enumerate(text.split("\n")):
            if index:
                out.append("\n")
            start = len(out)
            out.append_text(line)
            fill = width - cell_len(line.plain)
            if fill > 0:
                out.append(" " * fill)
            if paint:
                out.stylize(paint, start, len(out))
        return out

    def _header(self, block: Block, width: int, paint: str) -> Text:
        """The one line a folded block is known by."""
        emphasis = retro_themes.block_emphasis(block.theme)
        label = "thinking" if block.kind == "reasoning" else (block.language or "code")
        if block.kind == "code":
            count = f"{block.body.count(chr(10)) + 1} lines"
        else:
            count = f"{len(block.body):,} chars"
        header = Text()
        header.append(f" {'▸' if block.folded else '▾'} {label} ", style=f"bold {emphasis}")
        header.append(f" {count}", style="dim")
        header.append(
            "  ·  click to expand" if block.folded else "  ·  click to fold",
            style="dim italic",
        )
        return self._pad(header, width, paint)

    def _prose(self, block: Block) -> Text:
        return semantic.highlight(
            block.body,
            theme=block.theme,
            enabled=bool(getattr(self.ui, "semantic", True)),
        )

    def _reasoning(self, block: Block, width: int, paint: str) -> Text:
        """Thinking: indented, italic, on the reasoning band."""
        body = "\n".join("  " + line for line in block.body.split("\n"))
        text = semantic.highlight(
            body,
            theme=block.theme,
            enabled=bool(getattr(self.ui, "semantic", True)),
        )
        return self._pad(text, width, f"{paint} italic".strip())

    def _code(self, block: Block, background: str, width: int) -> Syntax:
        """Code on the code band, through rich's own highlighter."""
        source = block.body
        if background:
            # Syntax paints its background over the lines it is handed, no
            # further, so they are padded out first; the cell of padding the
            # Syntax itself adds is the recess the slab sits in.
            inner = max(8, width - 2)
            source = "\n".join(
                line + " " * max(0, inner - cell_len(line))
                for line in source.split("\n")
            )
        return Syntax(
            source,
            block.language or "text",
            theme=SYNTAX_THEME_BY_UI.get(block.theme, "monokai"),
            word_wrap=True,
            padding=(0, 1),
            background_color=background or None,
        )

    def _render(self, block: Block) -> list[Any]:
        """The renderables for ``block``: a header, and a body unless folded."""
        background = retro_themes.block_background(block.theme, block.kind)
        paint = f"on {background}" if background else ""
        width = self.band_width()
        if block.kind not in retro_themes.BLOCK_KINDS:
            return [self._prose(block)]
        header = self._header(block, width, paint)
        if block.folded:
            return [header]
        if block.kind == "code":
            return [header, self._code(block, background, width)]
        return [header, self._reasoning(block, width, paint)]

    def _draw(self, block: Block, scroll_end: bool | None = None) -> None:
        """Render one block and record the rows it took."""
        start = len(self.lines)
        for renderable in self._render(block):
            self.write(renderable, width=self.band_width(), scroll_end=scroll_end)
        self._spans.append((start, len(self.lines)))

    def _add(
        self, kind: str, body: str, language: str = "", folded: bool = False
    ) -> Block:
        """Append a block and draw it.

        A code block settles on its lexer here, once, rather than at every
        draw: an alias pygments knows is used as written, otherwise the code
        itself is sniffed for proof-grade evidence (see :func:`_sniff`). The
        header then has a real language to name even for an untagged fence.
        """
        if kind == "code":
            language = resolve_language(language, body)
        block = Block(
            kind=kind,
            body=body,
            language=language,
            theme=getattr(self.app, "theme", "") or "",
            folded=folded,
        )
        self.blocks.append(block)
        self._draw(block)
        return block

    # -- public writes -----------------------------------------------------

    def write_prose(self, text: str) -> None:
        self._add("prose", text)

    def write_code(self, language: str, code: str) -> None:
        self._add("code", code, language=language)

    def write_reasoning(self, text: str, folded: bool = True) -> None:
        """Thinking: a block of its own, folded unless told otherwise."""
        self._add("reasoning", text, folded=folded)

    # -- folding -----------------------------------------------------------

    def rerender(self) -> None:
        """Draw the whole transcript again from its blocks.

        The only way a fold can change the height of something already
        written: the log is append-only, so every row below the fold has to be
        laid down again. The viewport is held across the redraw, because a fold
        should not throw the reader out of the place they were reading.
        """
        scroll = int(self.scroll_offset.y)
        self.clear()
        self._spans = []
        for block in self.blocks:
            self._draw(block, scroll_end=False)
        if scroll:
            self.scroll_to(y=min(scroll, int(self.max_scroll_y)), animate=False)

    @property
    def spans(self) -> list[tuple[int, int]]:
        """The half-open row range each block occupies, in block order."""
        return list(self._spans)

    def set_folded(self, kind: str, folded: bool) -> int:
        """Fold or unfold every block of ``kind``; returns how many changed."""
        changed = 0
        for block in self.blocks:
            if block.kind == kind and block.folded != folded:
                block.folded = folded
                changed += 1
        if changed:
            self.rerender()
        return changed

    def toggle(self, block: Block) -> bool:
        """Fold or unfold one block, holding the reader's place."""
        index = self._index_of(block)
        if index < 0:
            return False
        old = self._spans[index] if index < len(self._spans) else (0, 0)
        scroll = int(self.scroll_offset.y)
        block.folded = not block.folded
        self.rerender()
        new = self._spans[index] if index < len(self._spans) else old
        delta = (new[1] - new[0]) - (old[1] - old[0])
        if delta and old[0] < scroll:
            # The block changed height above the reader; follow it exactly.
            self.scroll_to(y=max(0, scroll + delta), animate=False)
        return True

    def _index_of(self, block: Block) -> int:
        for index, candidate in enumerate(self.blocks):
            if candidate is block:
                return index
        return -1

    def header_at(self, row: int) -> Block | None:
        """The foldable block whose header sits on ``row``, if any."""
        for index, (start, _end) in enumerate(self._spans):
            if start != row or index >= len(self.blocks):
                continue
            block = self.blocks[index]
            if block.kind in retro_themes.BLOCK_KINDS:
                return block
        return None

    def _row_at(self, event: Any) -> int:
        """The transcript row under a mouse event, or -1 if it is not on text.

        Two offsets have to be undone, and each one hid the other while the
        other was untested. The widget's own ``y`` counts its border and padding
        (``MouseEvent.get_content_offset`` is what subtracts them, and Textual's
        own ``Input`` reads it the same way), and it does *not* count the log's
        internal scroll, which ``render_line`` adds back itself. Both are needed:
        with the border alone a press landed one row high, and with the scroll
        alone a press in a scrolled transcript landed rows off.
        """
        offset = event.get_content_offset(self)
        if offset is None:
            return -1
        return int(offset.y) + int(self.scroll_offset.y)

    def on_mouse_down(self, event: Any) -> None:
        """A press on a block's header folds or unfolds that block.

        Acting on the press, not on ``Click``, is deliberate. Textual only
        makes a Click when the *same widget* is under the pointer at press and
        release (``App.on_event``), and it hands a widget only the pointer's own
        ``y`` -- so any press the terminal reports a cell off, or one that ends
        on the bar below, silently did nothing. Pairing the press with the
        release is no better: a hand that wobbles one row drops the fold. The
        press itself always reaches the widget under the pointer, so folding
        there is the one path that cannot be lost. A drag that starts on a
        header row therefore folds it, which is the price of a click that
        always works.
        """
        block = self.header_at(self._row_at(event))
        if block is not None:
            self.toggle(block)

    def retag(self, theme: str) -> int:
        """Re-theme every block so the log matches ``theme``.

        Bands, fold headers and semantic colours are resolved as a block is
        drawn, so switching themes has to re-resolve them: without this a
        reader who changes theme keeps the old palette on everything already on
        screen (which is how a commodore-blue band survives into matrix).

        Returns the number of blocks that had to change.
        """
        stale = [block for block in self.blocks if block.theme != theme]
        if not stale:
            return 0
        for block in stale:
            block.theme = theme
        self.rerender()
        return len(stale)

    def fold_report(self) -> list[tuple[str, int, int]]:
        """``(kind, folded, total)`` for each kind that can fold."""
        report = []
        for kind in retro_themes.BLOCK_KINDS:
            blocks = [block for block in self.blocks if block.kind == kind]
            report.append((kind, sum(1 for block in blocks if block.folded), len(blocks)))
        return report


class PastePad(ModalScreen[str | None]):
    BINDINGS = [
        Binding("f2", "send", "send", priority=True),
        Binding("escape", "cancel", "cancel", priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Static("paste — F2 send · Esc cancel", classes="modal-title"),
            TextArea(id="paste-input", classes="modal-input"),
            Static("", classes="modal-status"),
            id="paste-wrap",
        )

    def action_send(self) -> None:
        self.dismiss(self.query_one("#paste-input", TextArea).text.rstrip("\n"))

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilePicker(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel", priority=True)]

    def __init__(self, root: Path, **kw: Any) -> None:
        super().__init__(**kw)
        self.root = Path(root)

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"choose a base prompt under {self.root} — Enter picks · Esc cancels",
                   classes="modal-title"),
            DirectoryTree(str(self.root), id="picker-tree"),
            Static("", classes="modal-status"),
            id="picker-wrap",
        )

    @on(DirectoryTree.FileSelected)
    def _picked(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(Path(event.path))

    def action_cancel(self) -> None:
        self.dismiss(None)


class RunWizard(ModalScreen[dict[str, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel", priority=True)]

    FIELDS = ("objective", "target", "modes", "family", "attempts")
    HINTS = {
        "objective": "what the cycle is trying to make the target do (blank cancels)",
        "target": "the model the run attacks (blank keeps the current target)",
        "modes": "comma list, blank for all seven in order",
        "family": "CL4R1T4S family to mount as the target's deployment, blank = bare",
        "attempts": "hard cap on attempts, blank = modes × rounds",
    }

    def __init__(self, targets: list[str], modes: list[str], families: list[str], **kw: Any) -> None:
        super().__init__(**kw)
        self.targets = targets
        self.modes = modes
        self.families = families
        self.values: dict[str, str] = {}
        self.step = 0

    def compose(self) -> ComposeResult:
        yield Container(
            Static(self._title(), classes="modal-title", id="wizard-title"),
            Static(self.HINTS[self.FIELDS[self.step]], id="wizard-hint", classes="modal-status"),
            Input(placeholder=self.FIELDS[self.step], id="wizard-input", classes="modal-input"),
            Static("", id="wizard-status", classes="modal-status"),
            id="wizard-wrap",
        )

    def _title(self) -> str:
        return f"start a cycle — step {self.step + 1}/{len(self.FIELDS)} · {self.FIELDS[self.step]}  (Tab from the prompt = pick from list)"

    def on_mount(self) -> None:
        self.query_one("#wizard-input", Input).focus()

    def on_key(self, event: Any) -> None:
        if event.key == "tab":
            self._pick_from_list()
            event.stop()

    def _pick_from_list(self) -> None:
        """Swap the input for a list of known values for this field."""
        field = self.FIELDS[self.step]
        supply: list[str] = []
        if field == "target":
            supply = self.targets
        elif field == "modes":
            supply = self.modes
        elif field == "family":
            supply = self.families
        if supply:
            self.set_class(True, "picking")
            self.push_screen(
                ListPick(supply, title=f"pick {field}"),
                self._pick_done,
            )

    def _pick_done(self, picked: Any) -> None:
        if picked:
            self.query_one("#wizard-input", Input).value = str(picked)

    @on(Input.Submitted)
    def _accept(self, event: Input.Submitted) -> None:
        field = self.FIELDS[self.step]
        self.values[field] = event.value.strip()
        self.step += 1
        if self.step >= len(self.FIELDS):
            self.dismiss(self.values)
            return
        event.input.value = ""
        event.input.placeholder = self.FIELDS[self.step]
        self.query_one("#wizard-title", Static).update(self._title())
        self.query_one("#wizard-hint", Static).update(self.HINTS[self.FIELDS[self.step]])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ListPick(ModalScreen[str | None]):
    """A simple dropdown-style picker layered over a modal (wizard/picker)."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", priority=True),
        Binding("up", "up", show=False),
        Binding("down", "down", show=False),
        Binding("enter", "pick", show=False),
    ]

    def __init__(self, options: list[str], *, title: str = "pick", **kw: Any) -> None:
        super().__init__(**kw)
        self.options = options
        self.title_text = title
        self.sel = 0

    def compose(self) -> ComposeResult:
        yield Container(
            Static(self.title_text + " — Up/Down move · Enter picks · Esc cancels", classes="modal-title"),
            Static("", id="pick-list", classes="modal-input"),
            Static("", id="pick-status", classes="modal-status"),
            id="pick-wrap",
        )

    def on_mount(self) -> None:
        self.query_one("#pick-list", Static).focus()
        self._repaint()

    def _repaint(self) -> None:
        rows = [("❯ " if i == self.sel else "  ") + o for i, o in enumerate(self.options)]
        self.query_one("#pick-list", Static).update("\n".join(rows) if rows else "(none)")
        self.query_one("#pick-status", Static).update(f"{self.sel + 1}/{len(self.options)}")

    def action_up(self) -> None:
        if self.sel > 0:
            self.sel -= 1
        self._repaint()

    def action_down(self) -> None:
        if self.sel < len(self.options) - 1:
            self.sel += 1
        self._repaint()

    def action_pick(self) -> None:
        self.dismiss(self.options[self.sel] if self.options else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MultiSelectPicker(ModalScreen[list[str] | None]):
    """A multi-select picker: arrow keys move, Space toggles, Enter confirms."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", priority=True),
        Binding("up", "up", show=False),
        Binding("down", "down", show=False),
        Binding("space", "toggle", "toggle", show=False),
        Binding("enter", "confirm", "confirm", show=False),
    ]

    def __init__(
        self,
        options: list[str],
        *,
        title: str = "select",
        default_selected: list[str] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.options = options
        self.title_text = title
        self.sel = 0
        self.selected: set[int] = set()
        if default_selected:
            for i, opt in enumerate(options):
                if opt in default_selected:
                    self.selected.add(i)

    def compose(self) -> ComposeResult:
        yield Container(
            Static(
                self.title_text
                + " — ↑/↓ move · Space toggle · Enter confirm · Esc cancel",
                classes="modal-title",
            ),
            Static("", id="pick-list", classes="modal-input"),
            Static("", id="pick-status", classes="modal-status"),
            id="pick-wrap",
        )

    def on_mount(self) -> None:
        self.query_one("#pick-list", Static).focus()
        self._repaint()

    def _repaint(self) -> None:
        rows = []
        for i, opt in enumerate(self.options):
            marker = "❯ " if i == self.sel else "  "
            check = "[✓] " if i in self.selected else "[ ] "
            rows.append(f"{marker}{check}{opt}")
        self.query_one("#pick-list", Static).update(
            "\n".join(rows) if rows else "(none)"
        )
        self.query_one("#pick-status", Static).update(
            f"{len(self.selected)}/{len(self.options)} selected · row {self.sel + 1}/{len(self.options)}"
        )

    def action_up(self) -> None:
        if self.sel > 0:
            self.sel -= 1
        self._repaint()

    def action_down(self) -> None:
        if self.sel < len(self.options) - 1:
            self.sel += 1
        self._repaint()

    def action_toggle(self) -> None:
        if self.sel in self.selected:
            self.selected.remove(self.sel)
        else:
            self.selected.add(self.sel)
        self._repaint()

    def action_confirm(self) -> None:
        chosen = [self.options[i] for i in sorted(self.selected)]
        self.dismiss(chosen if chosen else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SkillsEditor(ModalScreen[str | None]):
    """Create, edit, and delete skills without leaving the chat.

    The left column lists every discovered skill with its scope (workspace
    or profile); the right column is a plain TextArea over the raw SKILL.md
    -- frontmatter and body exactly as on disk, because the format is the
    portability contract and an editor that hid it would be a different,
    lesser tool. F2 saves (validating that the frontmatter parses and has a
    name), ctrl-n starts a new skill from the template, ctrl-d deletes the
    open one after a confirm.

    Writing goes to the skill's own file; a new skill is created in the
    workspace (``<root>/skills/<slug>/SKILL.md``), never the profile dir --
    the operator's global skills are read-mostly from the chat's point of
    view, and the shadowing rule means a workspace copy is the correct way
    to override one anyway.
    """

    BINDINGS = [
        Binding("f2", "save", "save", priority=True),
        Binding("escape", "cancel", "cancel", priority=True),
        Binding("ctrl-n", "new", "new"),
        Binding("ctrl-d", "delete", "delete"),
    ]

    TEMPLATE = (
        "---\n"
        "name: my-skill\n"
        "description: One line saying when this skill applies.\n"
        "trigger:\n"
        "  keywords: []\n"
        "---\n\n"
        "# My Skill\n\n"
        "1. First step of the procedure.\n"
        "2. Second step.\n"
    )

    def __init__(self, config: Any, select: str = "", **kw: Any) -> None:
        super().__init__(**kw)
        self.config = config
        self.initial = select
        from .skills import list_skills

        self.skills = list_skills(Path(config.chat.root))
        self.current: Path | None = None

    def compose(self) -> ComposeResult:
        yield Container(
            Static("skills editor — ↑/↓ pick · F2 save · ctrl-n new · ctrl-d delete · Esc close",
                   classes="modal-title"),
            Horizontal(
                Static("", id="skill-list", classes="modal-input"),
                Vertical(
                    TextArea("", id="skill-body", classes="modal-input"),
                    Static("", id="skill-status", classes="modal-status"),
                ),
                id="skill-columns",
            ),
            id="skill-editor-wrap",
        )

    def on_mount(self) -> None:
        self._repaint_list()
        if self.initial:
            self._open(self.initial)
        elif self.skills:
            self._open(self.skills[0].path)
        else:
            self.action_new()

    # -- helpers -----------------------------------------------------------
    def _repaint_list(self) -> None:
        from .skills import list_skills

        self.skills = list_skills(Path(self.config.chat.root))
        root = Path(self.config.chat.root)
        lines = []
        for skill in self.skills:
            in_ws = root in skill.path.parents
            scope = "ws" if in_ws else "~"
            mark = "❯ " if self.current and skill.path == self.current else "  "
            lines.append(f"{mark}{skill.name}  [{scope}]")
        self.query_one("#skill-list", Static).update(
            "\n".join(lines) if lines else "(no skills — ctrl-n to create one)"
        )

    def _open(self, which: str | Path) -> bool:
        from .skills import read_skill

        target = None
        for skill in self.skills:
            if str(which) in (skill.name, skill.path.stem, skill.path.parent.name,
                              str(skill.path)):
                target = skill
                break
        if target is None and isinstance(which, Path) and which.is_file():
            target = read_skill([s for s in self.skills], str(which))
        if target is None:
            return False
        self.current = target.path
        editor = self.query_one("#skill-body", TextArea)
        editor.text = target.path.read_text(encoding="utf-8")
        self._status(f"editing {target.path}")
        self._repaint_list()
        return True

    def _status(self, text: str) -> None:
        self.query_one("#skill-status", Static).update(text)

    # -- validation ----------------------------------------------------------
    def _parsed(self) -> tuple[dict[str, Any], str]:
        """(frontmatter, body) of the editor's text, or ({}, '') on a bad fence."""
        text = self.query_one("#skill-body", TextArea).text
        from .skills import _FRONTMATTER_RE, _parse_yaml

        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {}, text
        return _parse_yaml(match.group(1)), text[match.end():]

    def _validate(self) -> str:
        meta, body = self._parsed()
        name = str(meta.get("name") or "").strip()
        if not name:
            return "frontmatter needs a name: (no frontmatter, or name: is empty)"
        if not str(meta.get("description") or "").strip():
            return "frontmatter needs a description: one line saying when it applies"
        if not body.strip():
            return "the body is empty — a skill is its procedure"
        return ""

    # -- actions ---------------------------------------------------------------
    def action_save(self) -> None:
        problem = self._validate()
        if problem:
            self._status(f"not saved — {problem}")
            return
        text = self.query_one("#skill-body", TextArea).text
        assert self.current is not None
        self.current.parent.mkdir(parents=True, exist_ok=True)
        self.current.write_text(text, encoding="utf-8")
        meta, _body = self._parsed()
        self._status(f"saved {self.current}")
        self._repaint_list()

    def action_new(self) -> None:
        root = Path(self.config.chat.root) / "skills"
        base = "my-skill"
        slug, n = base, 2
        while (root / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        self.current = root / slug / "SKILL.md"
        editor = self.query_one("#skill-body", TextArea)
        editor.text = self.TEMPLATE
        self._status(f"new skill — edit and F2 to save to {self.current}")
        self._repaint_list()

    def action_delete(self) -> None:
        if self.current is None:
            return
        doomed = self.current
        if doomed.parent.is_dir() and (doomed.parent / "SKILL.md") == doomed:
            import shutil

            shutil.rmtree(doomed.parent)
        else:
            doomed.unlink(missing_ok=True)
        self.current = None
        self._repaint_list()
        if self.skills:
            self._open(self.skills[0].path)
        else:
            self.query_one("#skill-body", TextArea).text = ""
            self._status("deleted; ctrl-n to create a new skill")

    def action_cancel(self) -> None:
        self.dismiss(None)


class OptionsScreen(ModalScreen[None]):
    """Theme + remembered Ollama endpoints (stored as a small JSON file)."""

    BINDINGS = [Binding("escape", "cancel", "cancel", priority=True)]
    DEFAULT_CSS = "#options-wrap { width: 84; max-height: 90%; } .modal-input { padding: 0 1; }"
    can_focus = True
    can_focus_children = True

    def __init__(self, ui: Any, **kw: Any) -> None:
        super().__init__(**kw)
        self.ui = ui
        self.theme_sel = max(0, THEMES.index(self.ui.app.theme) if self.ui.app and self.ui.app.theme in THEMES else 0)
        self.api_sel = 0
        self.section = 0  # 0 theme, 1 api

    def compose(self) -> ComposeResult:
        from textual.widgets import Select

        yield Container(
            Static("options — Esc closes", classes="modal-title"),
            Static("theme", classes="modal-status"),
            Static("", id="opt-theme-list", classes="modal-input"),
            Static("", id="opt-api-title", classes="modal-status"),
            Static("", id="opt-api-list", classes="modal-input"),
            Static("set OLLAMA_URL=… and press S here to add the current endpoint", classes="modal-status"),
            id="options-wrap",
        )

    def on_mount(self) -> None:
        # Anchor focus so Tab/arrow keys reach the host Input first.
        self.query_one("#opt-theme-list", Static).focus()
        self._repaint()

    def _repaint(self) -> None:
        rows = [("❯ " if i == self.theme_sel else "  ") + name for i, name in enumerate(THEMES)]
        self.query_one("#opt-theme-list", Static).update("\n".join(rows))
        from .catalog import known_apis

        apis = known_apis(self.ui.config)
        title = "saved api endpoints"
        lines = []
        for i, url in enumerate(apis):
            current = " (current)" if url == self.ui.config.ollama_url else ""
            lines.append(("❯ " if i == self.api_sel and self.section == 1 else "  ") + url + current)
        self.query_one("#opt-api-title", Static).update(title)
        self.query_one("#opt-api-list", Static).update("\n".join(lines) if lines else "(none saved)")

    async def _on_key(self, event: Any) -> None:
        if event.key == "tab":
            self.section = 1 - self.section
            event.stop()
            self._repaint()
            return
        if self.section == 0:
            if event.key == "up" and self.theme_sel > 0:
                self.theme_sel -= 1
                event.stop()
            elif event.key == "down" and self.theme_sel < len(THEMES) - 1:
                self.theme_sel += 1
                event.stop()
            elif event.key == "enter":
                self.ui._set_theme(THEMES[self.theme_sel])
                event.stop()
        else:
            from .catalog import known_apis

            apis = known_apis(self.ui.config)
            if event.key == "up" and self.api_sel > 0:
                self.api_sel -= 1
                event.stop()
            elif event.key == "down" and apis and self.api_sel < len(apis) - 1:
                self.api_sel += 1
                event.stop()
            elif event.key == "enter" and apis:
                from .catalog import set_api

                set_api(self.ui.config, apis[self.api_sel])
                event.stop()
                self._repaint()
        self._repaint()

    def action_cancel(self) -> None:
        self.dismiss(None)

class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("f1", "close", "close", priority=True),
        Binding("escape", "close", "close", priority=True),
    ]
    DEFAULT_CSS = "#help-wrap { width: 84; max-height: 80%; }"

    def compose(self) -> ComposeResult:
        yield Container(
            Static("help — F1/Esc closes", classes="modal-title"),
            VerticalScroll(Static(HELP_TEXT, classes="modal-input"), id="help-scroll"),
            id="help-wrap",
        )

    def on_mount(self) -> None:
        # Focus a scrollable widget so Esc (and F1) reach the screen's own
        # bindings rather than being hand-picked at the App level.
        self.query_one("#help-scroll", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class MatrixRain:
    """A curtain of falling glyphs, drawn onto the chat's own cells.

    This is a grid of data, not a widget. A widget would be *above* the text in
    the layout, which in Textual means the text underneath stops being
    rendered at all (the compositor clips a covered widget out of the frame),
    so an overlay widget shows glyphs on an empty band instead of on the chat.
    ``Transcript.render_line`` asks this object for a row's glyphs and paints
    them over that row's strip, which is what makes the rain land on the text.

    The glyph set and hues are the matrix theme's: half-width katakana in the
    film's greens, with light combining-mark distortion, painted on black. Only
    that theme rains -- see :func:`rain_for_theme`.
    """

    #: Seconds between frames; the transcript's interval calls ``tick``.
    interval = 0.12

    #: Half-width katakana render in one cell, so the grid stays aligned.
    _KATAKANA = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎ0123456789:+*<>="
    #: Light zalgo: one combining mark, now and then, never stacked.
    _ZALGO = ("\u0301", "\u0300", "\u0308", "\u0323")
    #: Rows of trail behind each head, brightest first.
    _TRAIL = 7
    _BRIGHT = ("#C8FFD0", "#9BFFB0", "#6BFF8A", "#00FF41", "#00E03A",
               "#00B830", "#009125")

    def __init__(
        self,
        glyphs: str = _KATAKANA,
        *,
        colours: Sequence[str] = _BRIGHT,
        zalgo: Sequence[str] = _ZALGO,
        trail: int = _TRAIL,
        density: float = 0.15,
        background: str | None = None,
    ) -> None:
        self.glyphs = glyphs
        self.colours = list(colours) or list(self._BRIGHT)
        self.zalgo = tuple(zalgo)
        self.trail = max(1, int(trail))
        #: Chance a trailing cell takes a combining mark (the zalgo "dissolve").
        self.density = density
        #: Paint the glyph's own cell this colour. The matrix needs it: its
        #: glyphs are meant to sit on black, and every other theme has a lighter
        #: background the rain would otherwise pick up.
        self.background = background
        self._heads: list[int] = []
        self._rows: dict[int, list[tuple[int, str, str]]] = {}

    # -- animation ---------------------------------------------------------
    def tick(self, width: int, height: int) -> None:
        """Advance every column's head one row and rebuild this frame."""
        if width <= 0 or height <= 0:
            return
        if len(self._heads) != width:
            self._heads = [random.randint(-height, 0) for _ in range(width)]
        rows: dict[int, list[tuple[int, str, str]]] = {}
        for column, head in enumerate(self._heads):
            for step in range(self.trail):
                row = head - step
                if 0 <= row < height:
                    glyph = random.choice(self.glyphs)
                    if step and self.zalgo and random.random() < self.density:
                        glyph += random.choice(self.zalgo)
                    colour = self.colours[min(step, len(self.colours) - 1)]
                    rows.setdefault(row, []).append((column, glyph, colour))
            self._heads[column] = head + 1
            if head - self.trail >= height:
                self._heads[column] = random.randint(-height, 0)
        self._rows = rows

    @property
    def active(self) -> bool:
        return bool(self._rows)

    # -- painting ----------------------------------------------------------
    def overlay(self, strip: Strip, y: int) -> Strip:
        """``strip`` with row ``y`` of the grid painted over it, cell for cell."""
        cells = self._rows.get(y)
        if not cells:
            return strip
        width = strip.cell_length
        wanted = {
            column: (glyph, colour)
            for column, glyph, colour in cells
            if 0 <= column < width
        }
        if not wanted:
            return strip
        cuts: set[int] = set()
        for column in wanted:
            cuts.add(column)
            cuts.add(column + 1)
        pieces = strip.divide(sorted(cuts))
        painted: list[Segment] = []
        position = 0
        for piece in pieces:
            if position in wanted and piece.cell_length == 1:
                glyph, colour = wanted[position]
                style = Style.parse(colour)
                if self.background is not None:
                    style += Style(bgcolor=self.background)
                painted.append(Segment(glyph, style))
            else:
                painted.extend(piece)
            position += piece.cell_length
        return Strip(painted, width)


def rain_for_theme(theme: str, colours: Sequence[str]) -> MatrixRain | None:
    """The falling-glyph overlay ``theme`` gets, or ``None`` for the rest.

    The rain is the matrix theme's effect, not a generic one: only ``matrix``
    returns an overlay here. Every other theme gets the cycling border and
    nothing else, so a plain theme is never covered in glyphs.
    """
    if theme != "matrix":
        return None
    #: Katakana on black, the way the film's terminal reads. The colours stay
    #: the matrix greens rather than the border's palette: a pink rain is not
    #: this theme.
    return MatrixRain(
        MatrixRain._KATAKANA,
        colours=MatrixRain._BRIGHT,
        trail=MatrixRain._TRAIL,
        density=0.15,
        background="#000000",
    )


class ChatApp(App[Any]):
    """The full-screen chat UI."""

    CSS = """
    Screen { layout: vertical; }
    #topbar { height: 1; background: $primary; color: $text; padding: 0 1; }
    #runbar { height: 1; background: $warning; color: $text; padding: 0 1; }
    #bottombar { height: 1; background: $primary; color: $text; padding: 0 1; }
    #transcript { border: solid $surface-lighten-1; }
    #content { height: 1fr; overflow: hidden; }
    #input { border: solid $primary; }
    #paste-wrap, #picker-wrap, #wizard-wrap, #help-wrap {
        border: solid $accent; background: $surface; padding: 1 2; width: 84;
        max-height: 80%;
    }
    #help-wrap { height: 70%; }
    /* The choice pickers are a strip above the input bar, not a takeover:
       a bounded panel, like a command palette. Two rules do this: the screen
       the picker lives on aligns the panel (align on #pick-wrap itself would
       only place its children inside it), and the panel is sized so a long
       option list scrolls within its box rather than growing the window. */
    ListPick, MultiSelectPicker {
        align: center bottom;
    }
    #pick-wrap {
        border: solid $accent; background: $surface; padding: 0 2;
        width: 84; height: auto; max-height: 40%;
    }
    /* The skills editor is a workspace, not a strip: full width, nearly full
       height, two columns. */
    #skill-editor-wrap {
        border: solid $accent; background: $surface; padding: 0 1;
        width: 96%; height: 88%;
    }
    #skill-columns { height: 1fr; }
    #skill-list {
        width: 30; height: auto; max-height: 100%;
        background: $surface-darken-2; padding: 0 1;
    }
    #skill-body { height: 1fr; }
    #skill-status { height: 1; }
    .modal-title { background: $surface-lighten-1; color: $text; padding: 0 1; }
    .modal-status { color: $text-muted; padding-top: 1; }
    .modal-input { background: $surface-darken-2; }
    #picker-tree { max-height: 26; background: $surface; }
    """

    BINDINGS = [
        Binding("f1", "help", "help", priority=True),
        Binding("c-q", "quit", "quit", priority=True),
        Binding("f2", "paste", "paste", priority=True),
        Binding("f3", "picker", "picker", priority=True),
        Binding("f4", "wizard", "wizard", priority=True),
        # App never binds Esc: modals opt in to closing themselves, the menu
        # owns its own toggle, and the base screen's binding is the fallback.
    ]
    # Enter is intentionally NOT bound here: the prompt's Input.Submitted must
    # reach it when menus are closed. An open menu takes focus (see
    # menubar.MenuBar.open), so Enter reaches the menu's own key handler.

    busy = reactive(False)

    def __init__(self, ui: "TextualChatUI", **kw: Any) -> None:
        super().__init__(**kw)
        # Before anything can set self.theme: an unregistered name silently
        # falls back to textual-dark instead of failing.
        retro_themes.register(self)
        self.ui = ui
        self.menubar: MenuBar | None = None
        #: Inference indicator: the interval that cycles the transcript
        #: border, the palette it walks, and the matrix rain overlay.
        self._inference_timer: Any = None
        self._inference_colors: list[str] = []
        self._inference_color_index = 0
        self._inference_border: Any = None
        self._matrix_rain: Any = None

    # -- layout ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield MenuBar(self.ui._menus(), id="menubar")
        yield Static(self.ui._top_bar(), id="topbar")
        with Container(id="content"):
            yield Transcript(self.ui, id="transcript")
        yield Static("", id="runbar")
        yield Static(self.ui._bottom_bar(), id="bottombar")
        yield HistoryInput(
            placeholder="chat…  (/help for commands, Esc for menu)",
            id="input",
            suggester=self.ui._make_suggester(),
        )
        yield Footer()

    def on_mount(self) -> None:
        self.ui._ui_thread_id = threading.get_ident()
        saved = retro_themes.load_saved_theme(THEMES)
        if saved:
            self.theme = saved
        self.menubar = self.query_one("#menubar", MenuBar)
        self.query_one("#input", Input).focus()
        self._draw_splash()
        self.ui.greet()
        self.set_interval(1.0, self._tick_run_bar)

    def _draw_splash(self) -> None:
        """Draw the name in the theme's own face, once, at boot.

        Drawn glyphs are decoration, so they take the transcript's own
        foreground: nothing here should be harder to read than the chat.
        """
        transcript = self.query_one("#transcript", Transcript)
        for line in fonts.render("K0B0L", fonts.font_for(self.theme)):
            transcript.write_prose(line)

    # -- inference indicator ------------------------------------------------
    def start_inference_effect(self) -> None:
        """Begin the theme's "working" indicator (idempotent).

        Runs on the app thread (see ``TextualChatUI._dispatch``), so the timer
        taken here is owned by the message pump that will fire it -- asking for
        one from the turn's thread is what raised "no running event loop".
        """
        if self._inference_timer is not None:
            return
        transcript = self._transcript()
        if transcript is None:
            return
        self._inference_colors = self._inference_palette()
        self._inference_color_index = 0
        self._inference_border = transcript.styles.border
        self._inference_timer = self.set_interval(0.1, self._cycle_inference_effect)
        # Only the matrix theme rains; every other theme just cycles the border.
        self._matrix_rain = rain_for_theme(self.theme, self._inference_colors)
        if self._matrix_rain is not None:
            transcript.start_rain(self._matrix_rain)

    def stop_inference_effect(self) -> None:
        """End the indicator and hand the transcript its own border back."""
        if self._inference_timer is not None:
            self._inference_timer.stop()
            self._inference_timer = None
        transcript = self._transcript()
        if transcript is not None:
            transcript.stop_rain()
            if self._inference_border is not None:
                transcript.styles.border = self._inference_border
        self._matrix_rain = None

    def _transcript(self) -> "Transcript | None":
        try:
            return self.query_one("#transcript", Transcript)
        except Exception:  # noqa: BLE001 - not mounted yet is not an error here
            return None

    def watch_theme(self, theme: str) -> None:
        """Repaint the transcript's blocks whenever the theme changes.

        Textual's own ``_watch_theme`` restyles the app; the blocks are ours,
        and they resolve their band and header colours as they are drawn. So a
        theme switch has to re-resolve them here or everything already on
        screen keeps the previous palette -- the reported "stuck on blue".
        """
        transcript = self._transcript()
        if transcript is not None:
            transcript.retag(theme)

    def _cycle_inference_effect(self) -> None:
        if not self._inference_colors:
            return
        self._inference_color_index = (
            self._inference_color_index + 1
        ) % len(self._inference_colors)
        transcript = self._transcript()
        if transcript is not None:
            transcript.styles.border = (
                "solid",
                self._inference_colors[self._inference_color_index],
            )

    def _inference_palette(self) -> list[str]:
        """The colours this theme cycles, taken from the theme itself.

        A per-theme table would go stale the moment a theme is added, so the
        theme's own register is the source of truth. The two heritage themes
        are the deliberate exceptions: the C64 wants the VIC-II border
        register, and matrix wants its single hue.
        """
        if self.theme == "commodore-64":
            # The sixteen colours the VIC-II could actually write to the
            # border register, in register order.
            return [
                "#000000", "#FFFFFF", "#68372B", "#70A4B2", "#6F3D86",
                "#588D43", "#352879", "#B8C76F", "#6F4F25", "#433900",
                "#9A6759", "#444444", "#6C6C6C", "#9AD284", "#6C5EB5",
                "#959595",
            ]
        if self.theme == "matrix":
            # One hue, as the theme is: the greens the rain fades through.
            return ["#00FF41", "#008F11", "#7CFF9B", "#B6FF00", "#00CC33"]
        palette: list[str] = []
        try:
            theme = self.get_theme(self.theme)
        except Exception:  # noqa: BLE001 - an unknown name is not worth a crash
            return ["#FFFF00", "#FF0000", "#00FF00", "#0000FF"]
        for attribute in ("primary", "accent", "secondary", "warning", "success"):
            color = getattr(theme, attribute, None)
            if color is None:
                continue
            hexed = getattr(color, "hex", None) or str(color)
            if hexed not in palette:
                palette.append(hexed)
        return palette or ["#FFFF00", "#FF0000", "#00FF00", "#0000FF"]

    # -- rendering ---------------------------------------------------------
    def render_line(self, text: str) -> None:
        self.query_one("#transcript", Transcript).write_prose(text)

    def render_code(self, language: str, code: str) -> None:
        self.query_one("#transcript", Transcript).write_code(language, code)

    def render_thinking(self, text: str) -> None:
        """Reasoning is always kept; ``/think`` decides only whether it opens.

        Writing it folded and letting the reader unfold it means reasoning is
        never lost to a display preference, which is what it used to be.
        """
        self.query_one("#transcript", Transcript).write_reasoning(
            text, folded=not self.ui.show_thinking
        )

    def fold_kind(self, kind: str, folded: bool) -> None:
        """Fold or unfold every ``kind`` block already on screen."""
        transcript = self.query_one("#transcript", Transcript)
        changed = transcript.set_folded(kind, folded)
        if changed:
            self.render_status()

    def render_status(self) -> None:
        self.query_one("#topbar", Static).update(self.ui._top_bar())
        self.query_one("#bottombar", Static).update(self.ui._bottom_bar())
        run = self.ui._run_bar()
        bar = self.query_one("#runbar", Static)
        bar.update(run)
        bar.display = bool(self.ui.runs is not None and self.ui.runs.ever_started)

    def _tick_run_bar(self) -> None:
        if self.ui.runs is not None and self.ui.runs.ever_started:
            self.ui.runs.poll()
            self.render_status()

    def watch_busy(self, busy: bool) -> None:
        self.render_status()

    # -- input -------------------------------------------------------------
    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        if isinstance(event.input, HistoryInput) and text.strip():
            event.input.note_submitted(text)
        self.ui.accept(text)

    # -- actions ----------------------------------------------------------
    def action_menu_or_cancel(self) -> None:
        if self.menubar is None:
            return
        # While a modal is up, Esc belongs to the modal. Textual dispatches
        # to its own bindings there; the menu flag is our indicator.
        if self.screen is not self.screen.__class__ and self.screen.modal is not False:
            self.screen.dismiss(None)
            return
        if self.menubar.menus_open:
            self.menubar.close_all()
            self.query_one("#input", Input).focus()
            return
        self.menubar.open()

    def menu_closed(self) -> None:
        self.query_one("#input", Input).focus()

    def _on_key(self, event: Any) -> None:
        # The menu bar normally holds focus while it is open, so it reads its
        # own arrow keys there. This is the fallback for when focus sits
        # elsewhere with a menu up.
        if self.menubar is not None and self.menubar.menus_open and event.key in (
            "left", "right", "up", "down", "enter", "escape"
        ):
            event.prevent_default()
            event.stop()
            self.menubar._on_key(event)
            return
        # Nothing else claimed it; leave focus where it is.
        if event.key == "escape" and self.menubar is not None:
            if self.menubar.menus_open:
                self.menubar.close_all()
            else:
                self.menubar.open()
            event.stop()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_paste(self) -> None:
        self.ui.action_paste()

    def action_picker(self) -> None:
        self.ui.action_picker()

    def action_wizard(self) -> None:
        self.ui.action_wizard()

    def action_quit(self) -> None:
        if self.menubar is not None:
            self.menubar.close_all()
        self.exit(result=0)

    # -- modals ------------------------------------------------------------
    def open_paste(self, on_done: Callable[[str | None], None]) -> None:
        self.push_screen(PastePad(), on_done)

    def open_picker(self, root: Path, on_pick: Callable[[Path | None], None]) -> None:
        self.push_screen(FilePicker(root), on_pick)

    def open_wizard(self, targets: list[str], modes: list[str], families: list[str],
                    on_done: Callable[[dict[str, str] | None], None]) -> None:
        self.push_screen(RunWizard(targets, modes, families), on_done)


class TextualChatUI(ChatUI):
    """ChatUI over the Textual app; hooks keep their engine signature."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.app: ChatApp | None = None
        self.semantic = semantic.enabled(self.config)
        self._busy = False
        self._ui_thread_id = threading.get_ident()
        self._stream: list[str] = []
        self._thinking: list[str] = []
        self._fence_language = ""
        self._fence_buffer: list[str] = []
        self._stream_needs_flush = False

    # -- menus ------------------------------------------------------------
    def _menus(self) -> list[tuple[str, list[MenuItem]]]:
        from . import catalog

        models = catalog.known_models(self.config) or []
        chat_items = [
            MenuItem(label=name, action=lambda n=name: self.dispatch(f"/model {n}"))
            for name in models
        ]
        chat_items.append(MenuItem(label="Other…", action=lambda: self._stage_command("/model ")))
        target_items = [
            MenuItem(label=name, action=lambda n=name: self.dispatch(f"/target {n}"))
            for name in models
        ]
        target_items.append(MenuItem(label="Other…", action=lambda: self._stage_command("/target ")))

        mode_items = [
            MenuItem(label=f"{name} — {one}",
                     action=lambda n=name: self._stage_command(f"/run start modes={n} "))
            for name, one in catalog.mode_choices()
        ]
        family_items = [
            MenuItem(label=name, action=lambda f=name: self._stage_command(f"/run start family={f} "))
            for name in catalog.deployment_choices()
        ]

        winner_items: list[MenuItem] = []
        winners_dir = self.config.logdir / "winning-prompts"
        if winners_dir.is_dir():
            for path in sorted(winners_dir.glob("winner-*.md"), reverse=True)[:10]:
                winner_items.append(MenuItem(label=path.name, action=lambda p=path: self._show_file(p)))
        if not winner_items:
            winner_items = [MenuItem(label="(no winning prompts archived yet)")]

        theme_items = [MenuItem(label=name, action=lambda n=name: self._set_theme(n)) for name in THEMES]

        return [
            ("Session", [
                MenuItem(label="Chat model", submenu=chat_items),
                MenuItem(label="Target model", submenu=target_items),
                MenuItem(label="Base prompt from file…", action=self.action_picker),
                MenuItem(label="Workspace root…", action=lambda: self._stage_command("/root ")),
                MenuItem(label="Save session…", action=lambda: self._stage_command("/session save ")),
                MenuItem(label="Load session…", action=lambda: self._stage_command("/session load ")),
                MenuItem(label="Paste a block", action=self.action_paste),
                MenuItem(label="Clear conversation", action=lambda: self.dispatch("/clear")),
                MenuItem(label="History", action=lambda: self.dispatch("/history")),
                MenuItem(label="Options…", action=self.action_options),
            ]),
            ("Run", [
                MenuItem(label="Start run (wizard)…", action=self.action_wizard),
                MenuItem(label="Start run…", action=lambda: self._stage_command("/run start ")),
                MenuItem(label="…with mode", submenu=mode_items),
                MenuItem(label="…under deployment family", submenu=family_items),
                MenuItem(label="Run status", action=lambda: self.dispatch("/run status")),
                MenuItem(label="Tail run log", action=lambda: self.dispatch("/run tail")),
                MenuItem(label="Stop run", action=lambda: self.dispatch("/run stop")),
                MenuItem(label="Winning prompts", submenu=winner_items),
            ]),
            ("View", [
                MenuItem(label="Tool output: off", action=lambda: self._set_tools("off")),
                MenuItem(label="Tool output: summary", action=lambda: self._set_tools("summary")),
                MenuItem(label="Tool output: full", action=lambda: self._set_tools("full")),
                MenuItem(label="Skills", submenu=self._skills_submenu()),
                MenuItem(label="Theme", submenu=theme_items),
                MenuItem(label="Data colouring", action=lambda: self.dispatch(
                    f"/semantic {'off' if self.semantic else 'on'}")),
                MenuItem(label="Toggle reasoning", action=self._toggle_thinking),
                MenuItem(label="Fold all reasoning", action=lambda: self.dispatch("/fold reasoning")),
                MenuItem(label="Fold all code", action=lambda: self.dispatch("/fold code")),
                MenuItem(label="Unfold everything", action=lambda: self.dispatch("/unfold all")),
            ]),
            ("Help", [
                MenuItem(label="Help panel", action=self.action_help),
                MenuItem(label="Product docs", action=lambda: self.dispatch("/docs")),
                MenuItem(label="Soul file", action=lambda: self.dispatch("/soul")),
                MenuItem(label="Quit", action=self._exit),
            ]),
        ]

    def inference_start(self) -> None:
        """Called when the model starts generating a response.

        The engine calls this on the turn's own thread, so the work is handed
        to the app thread and named after the App's method, not this object's.
        """
        super().inference_start()
        self._dispatch("start_inference_effect")

    def inference_end(self) -> None:
        """Called when the model finishes generating a response."""
        super().inference_end()
        self._dispatch("stop_inference_effect")

    def _set_theme(self, name: str) -> None:
        resolved = theme_name(name)
        if not resolved:
            self.line(f"unknown or ambiguous theme {name!r}; /theme lists them")
            return
        if self.app is None:
            return
        self.app.theme = resolved
        retro_themes.save_theme(resolved)
        self.line(f"theme: {resolved}")

    def _theme_command(self, argument: str) -> None:
        current = self.app.theme if self.app is not None else "(none)"
        if not argument:
            self.line("themes: " + ", ".join(THEMES))
            self.line(f"current: {current}")
            return
        self._set_theme(argument)

    def _semantic_command(self, argument: str) -> None:
        word = argument.strip().lower()
        if word in ("on", "off"):
            self.semantic = word == "on"
        elif word:
            self.line(f"/semantic: {word!r} is not a setting — try /semantic on or /semantic off")
            return
        state = "on" if self.semantic else "off"
        theme = self.app.theme if self.app is not None else "(none)"
        roles = len(semantic.styles_for(theme))
        self.line(f"data colouring: {state} — theme {theme!r} colours {roles} kinds of datum")
        self.line("lines already in the transcript keep the theme they arrived under")

    def dispatch(self, text: str) -> bool:
        """/theme, /semantic, and /skills edit, then everything else."""
        lowered = text.strip().lower()
        if lowered.startswith("/theme") and (
            len(lowered) == 6 or lowered[6].isspace()
        ):
            self._theme_command(text.strip()[6:])
            return True
        if lowered.startswith("/semantic") and (
            len(lowered) == 9 or lowered[9].isspace()
        ):
            self._semantic_command(text.strip()[9:])
            return True
        if lowered.startswith("/skills") and lowered.split()[1:2] == ["edit"]:
            self._open_skills_editor(text.strip().split(" ", 2)[-1].strip()
                                     if len(text.strip().split()) > 2 else "")
            return True
        return super().dispatch(text)

    def _open_skills_editor(self, select: str = "") -> None:
        """Push the skills editor; on close, rebuild prompts (skills changed)."""
        if self.app is None:
            return
        self.app.push_screen(SkillsEditor(self.config, select=select),
                             lambda _result: self._skills_editor_closed())

    def _skills_editor_closed(self) -> None:
        """The editor may have created, changed, or deleted a skill."""
        self.session.reset()
        self.line("skills re-scanned; system prompt rebuilt")

    def _skills_submenu(self) -> list[MenuItem]:
        from .skills import list_skills

        items = []
        for skill in list_skills(self.session.workspace.root):
            active = skill.name in self._selected_skills
            mark = "☑ " if active else "☐ "
            items.append(MenuItem(label=mark + skill.name, action=lambda n=skill.path.stem: self.dispatch(
                f"/skills {'disable' if active else 'enable'} {n}"
            )))
        if not items:
            items = [MenuItem(label="(no skills in workspace/skills or ~/.k0b0l-skills)")]
        items.append(MenuItem(label="Edit skills…",
                              action=lambda: self._open_skills_editor()))
        return items

    # -- engine-facing hooks ------------------------------------------------
    def _dispatch(self, method: str, *args: Any) -> None:
        if self.app is None:
            return
        if threading.get_ident() == self._ui_thread_id:
            getattr(self.app, method)(*args)
        else:
            self.app.call_from_thread(getattr(self.app, method), *args)

    def _write(self, text: str) -> None:
        # Fenced code blocks get real syntax highlighting; everything else goes
        # to the transcript as prose, where semantic colour picks out the data.
        # Runs on the app thread or through call_from_thread.
        # Simple fence splitter: odd chunks are code, even are prose.
        parts = text.split("```")
        for index, chunk in enumerate(parts):
            if not chunk:
                continue
            if index % 2 == 1:
                language, code = split_fence(chunk)
                # A tool-call block is the engine's own plumbing, not content
                # for the operator: the ⚙ panel above already shows the call,
                # and rendering the raw JSON here too reads as the same call
                # arriving twice.
                if language in ("tool", "tool_call") or code.lstrip().startswith('{"name"'):
                    continue
                code = code.strip("\n")
                if code and self.app is not None:
                    self._dispatch("render_code", language, code)
            else:
                if chunk.strip() and self.app is not None:
                    self._dispatch("render_line", chunk)

    def _refresh_bars(self) -> None:
        self._dispatch("render_status")

    def line(self, text: str = "") -> None:
        if self.app is None:
            return
        self._dispatch("render_line", text)

    def turn_start(self, model: str, protocol: str) -> None:
        self._busy = True
        self._stream = []
        self._thinking = []
        self.line(f"\n[model] {model} · {protocol} tools")
        self._refresh_bars()

    def turn_end(self) -> None:
        self._flush_thinking()
        if self._stream:
            self._write("".join(self._stream))
        self._stream = []
        self._thinking = []
        self._busy = False
        self._refresh_bars()

    def delta(self, text: str) -> None:
        # Prose in a round follows that round's reasoning, so this is where the
        # reasoning block closes and stops growing.
        self._flush_thinking()
        self._stream.append(text)

    def thinking(self, text: str) -> None:
        """Hold reasoning until the round moves on to something else.

        The engine streams this a chunk at a time, so the pieces are gathered
        and committed as one block --- otherwise a fold would have hundreds of
        one-line blocks to work with instead of one thought.
        """
        self._thinking.append(text)

    def _flush_thinking(self) -> None:
        """Commit the reasoning gathered so far as a single foldable block."""
        if not self._thinking:
            return
        body = "".join(self._thinking)
        self._thinking = []
        self._dispatch("render_thinking", body)

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self._flush_thinking()
        self._write(f"  ⚙ {summarize_call(name, arguments)}\n")

    def tool_result(self, name: str, result: ToolResult) -> None:
        status = "ok" if result.ok else "error"
        self._write(f"    {name} → {status}\n")
        if self.tool_output != "off":
            body = result.text
            if self.tool_output == "summary":
                body = self._clip_lines(body)
            prefix = "" if result.ok else "error: "
            for line in body.splitlines() or [""]:
                self._write(f"      {prefix}{line}\n")
                prefix = ""

    def notice(self, text: str) -> None:
        self._flush_thinking()
        self._write(f"  · {text}\n")

    def error(self, text: str) -> None:
        self._flush_thinking()
        self._write(f"  ! {text}\n")

    def status(self, stats: TurnStats) -> None:
        self._last_stats = stats
        body = f"prompt {stats.prompt_tokens}" if stats.prompt_tokens else ""
        if stats.output_tokens:
            rate = stats.output_tokens / stats.seconds if stats.seconds else 0
            body += f" · out {stats.output_tokens} ({rate:.1f} tok/s)"
        body += f" · {stats.seconds:.1f}s"
        if stats.done_reason and stats.done_reason != "stop":
            body += f" · {stats.done_reason}"
        self._write("  · " + body.lstrip(" ·") + "\n")

    def round_end(self, stats: TurnStats) -> None:
        self.status(stats)

    # -- bars ---------------------------------------------------------------
    def _top_bar(self) -> str:
        chat = self.session.config
        think = "think:open" if self.show_thinking else "think:folded"
        return (
            f"K0B0L │ {chat.model} │ root: {chat.root} │ "
            f"tools:{self.tool_output} {think}{self._fold_summary()} │ Esc menu · F1 help"
        )

    def _fold_summary(self) -> str:
        """What is currently folded away, for the top bar.

        Only shows folded blocks: a transcript with nothing folded is the
        ordinary case and does not need saying.
        """
        if self.app is None:
            return ""
        try:
            transcript = self.app.query_one("#transcript", Transcript)
        except Exception:  # noqa: BLE001 - the widget may not be mounted yet
            return ""
        parts = [
            f"{count} {kind}"
            for kind, count, _total in transcript.fold_report()
            if count
        ]
        return " · folded " + ", ".join(parts) if parts else ""

    def _bottom_bar(self) -> str:
        return " working… " if self._busy else " ready "

    def _run_bar(self) -> str:
        return self.runs.status_line()

    # -- operator entry points ----------------------------------------------
    def accept(self, text: str) -> None:
        """The Input widget's submit path. Commands first, models second."""
        if not text.strip():
            return
        head = text.strip().split(None, 1)[0].lower()
        if self._busy and not head.startswith("/"):
            self.notice("a turn is already running; wait a moment")
            return
        self.line(f"you> {text}")
        try:
            if self.dispatch(text):
                return
        except EOFError:
            self._exit()
            return
        self._busy = True
        self._refresh_bars()
        worker = threading.Thread(target=self._turn_worker, args=(text,),
                                  daemon=True, name="k0b0l-turn")
        worker.start()

    def _turn_worker(self, text: str) -> None:
        error: Exception | None = None
        try:
            self.ask(text)
        except Exception as exc:  # noqa: BLE001
            error = exc
        if self.app is not None:
            self.app.call_from_thread(self._turn_done, error)

    def _turn_done(self, error: Exception | None) -> None:
        self._busy = False
        if error is not None:
            self.error(str(error))
        self._refresh_bars()

    def open_menu_screen(self) -> None:
        if self.app is not None:
            self.app.action_menu_or_cancel()

    # -- modal openers, called from the app ---------------------------------
    def action_paste(self) -> None:
        if self.app is None:
            return
        self.app.push_screen(PastePad(), self._paste_done)

    def _paste_done(self, text: Any) -> None:
        if text:
            self.line("paste> (block of %d line(s))" % text.count("\n"))
            self.accept(text)

    def action_picker(self) -> None:
        if self.app is None:
            return
        self.app.push_screen(FilePicker(self.session.workspace.root), self._picker_done)

    def _picker_done(self, path: Any) -> None:
        if path:
            self._stage_command(f"/run start ofile={path} ")

    def action_wizard(self) -> None:
        if self.app is None:
            return
        from . import catalog

        self.app.push_screen(
            RunWizard(
                targets=self._catalog_models(),
                modes=[name for name, _ in catalog.mode_choices()],
                families=catalog.deployment_choices(),
            ),
            self._wizard_done,
        )

    def action_options(self) -> None:
        if self.app is None:
            return
        self.app.push_screen(OptionsScreen(self))

    def _wizard_done(self, values: Any) -> None:
        if not values:
            return
        parts = []
        if values.get("target"):
            parts.append(f"target={values['target']}")
        if values.get("modes"):
            parts.append(f"modes={values['modes']}")
        if values.get("family"):
            parts.append(f"family={values['family']}")
        if values.get("attempts"):
            parts.append(f"attempts={values['attempts']}")
        objective = values.get("objective", "")
        command = "/run start " + " ".join(parts + [objective])
        self.line(f"you> {command}")
        self.line(self._run_command(command[len("/run "):]))

    def action_help(self) -> None:
        if self.app is not None:
            self.app.action_help()

    def _exit(self) -> None:
        if self.app is not None:
            self.app.exit(result=0)

    def _set_tools(self, level: str) -> None:
        self.tool_output = level
        self.line(f"tool output → chat: {level}")

    def refresh_folds(self, kind: str, folded: bool) -> None:
        """Apply a fold choice to the blocks already on screen."""
        if self.app is not None:
            self._dispatch("fold_kind", kind, folded)

    def _toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        self.line(f"reasoning is {'unfolded' if self.show_thinking else 'folded away'}")
        self.refresh_folds("reasoning", not self.show_thinking)

    def _stage_command(self, text: str) -> None:
        if self.app is not None:
            input_widget = self.app.query_one("#input", Input)
            input_widget.value = text
            input_widget.focus()

    def _show_file(self, path: Path) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.error(f"cannot read {path}: {exc}")
            return
        self.line(f"--- {path} ---")
        for line in self._clip_lines(text, 40).splitlines():
            self.line(line)

    def _catalog_models(self) -> list[str]:
        from . import catalog

        return catalog.known_models(self.config)

    def _make_suggester(self) -> Any:
        try:
            return SuggestorForChat(self.config, self.session.workspace.root)
        except Exception:  # pragma: no cover
            return None

    def loop(self, input_fn: Callable[[str], str] | None = None) -> int:
        self.app = ChatApp(self)
        self.app.run()
        if self.runs is not None and self.runs.running:
            self.runs.stop()
        self.app = None
        return 0


def run_textual(config: Any) -> int | None:
    """Entry point for the Textual chat; ``None`` when it cannot run here."""
    from .tui import _pick_client

    client = _pick_client(config)
    try:
        client.version()
    except OllamaError as exc:
        print(f"Ollama is not reachable at {config.ollama_url}: {exc}")
        return 1
    chat = config.chat
    if not Path(chat.root).is_dir():
        print(f"chat workspace root is not a directory: {chat.root}")
        return 1
    ui = TextualChatUI(client, config, tool_output=chat.tool_output,
                       show_thinking=config.show_thinking)
    return ui.loop()
