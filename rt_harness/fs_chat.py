"""The full-screen chat front end, on prompt_toolkit's Application.

RichChat is an inline renderer: the transcript is the terminal scrollback and a
Live region owns the last turn. This front end takes over the whole terminal
instead: a scrollable transcript pane, a top status bar, a bottom status line,
a menu bar with drop-down menus (Session / View / Exit; F10 opens it, Esc
closes), and a single-line prompt that accepts multiline pastes through a
modal paste pad (/paste or F2).

The engine is unchanged. The difference that matters here is threading: the
model call cannot run on the event loop, or the UI freezes for the entire
generation. Each send() therefore goes through run_in_executor, and the
rendering hooks only mutate buffers and call ``app.invalidate()`` -- the one
cross-thread call prompt_toolkit makes safe. Slash commands behave exactly as
in the other front ends; anything needing an argument is typed the same way,
with the menu acting as a shortcut that stages the command into the prompt.
"""

from __future__ import annotations

import functools
import threading
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.history import FileHistory

from .chat import TurnStats
from .client import OllamaError
from .tools import TOOL_VERBOSITY, ToolResult, summarize_call
from .tui import ChatUI

#: Key that submits the paste pad. F2 is a single, portable key sequence.
PASTE_SUBMIT_KEY = "f2"

FS_HELP = """commands (menu: Esc; the paste pad opens on /paste or the Session menu)
  /help              this list (F1 opens the help panel)
  /tools <level>     tool output in the chat: off, summary, full
  /think on|off      show the model's reasoning tokens
  /read <path>       print a file into the chat
  /run start|status|tail|stop   spawn and watch a cycle run (operator-only; status bar shows it)
  /paste             open the multiline paste pad (F2 sends, Esc cancels)
  /model [name]      show or switch the chat model
  /models            list the models the endpoint reports
  /target [name]     show or set the model the next /run start targets
  /root [path]       show or change the workspace root
  /clear             forget the conversation (keeps the system message)
  /history           messages held, and how many tool calls have run
  /exit              leave (Ctrl-Q works too)
"""

HELP_PANEL = """help panel — keyboard and run syntax

keys
  Esc          open/close the menu bar; cancel the paste pad
  F1           this panel (Esc or F1 again closes it)
  F2           send the paste pad
  Ctrl-Q       leave
  Up/Down      scroll prompt history when the prompt is focused,
               move the selection in the file picker when it is open

/run start syntax
  /run start [target=MODEL] [modes=a,b] [attempts=N] [family=NAME] <objective>
  modes:    seam, graft, vocabulary, inversion, analogy, compaction, escalation
  family:   a CL4R1T4S family such as anthropic or openai
  ofile=    read the objective text from a file instead of typing it
"""


class FullScreenChat(ChatUI):
    """Owns the whole terminal: transcript pane, bars, menu, prompt."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._app: Any = None  # the Application, set once it exists
        self._busy = False
        self._last_stats: TurnStats | None = None
        self._stream: list[str] = []  # prose of the turn in flight
        self._thinking: list[str] = []

    # -- transcript --------------------------------------------------------
    def _append(self, text: str) -> None:
        # The transcript pane is built with read_only=True, which guards the
        # buffer against *user* edits; our own writes go through the buffer
        # with the readonly filter temporarily forced off. Buffer.text alone
        # raises EditReadOnlyBuffer; temporarily swapping the read_only flag
        # on the control would also work, but set_document() ignores filters
        # and does exactly the replace-and-place-cursor we want.
        buffer = self.transcript.buffer  # type: ignore[attr-defined]
        from prompt_toolkit.document import Document

        new_text = buffer.text + text
        buffer.set_document(Document(new_text, len(new_text)), bypass_readonly=True)

    def _append_block(self, title: str, body: str, marker: str) -> None:
        heading = f"{marker} {title}"
        self._append("\n" + heading + "\n")
        self._append("─" * len(heading) + "\n")
        if body:
            self._append(body.rstrip("\n") + "\n")

    def _refresh(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def _flush_turn(self) -> None:
        """Fold the in-flight stream into the transcript."""
        text = "".join(self._stream)
        if text.strip():
            self._append(text if text.startswith("\n") else "\n" + text)
            if not text.endswith("\n"):
                self._append("\n")
        self._stream = []
        self._thinking = []

    # -- hooks (called from the worker thread) ---------------------------
    def line(self, text: str = "") -> None:
        self._append(text + "\n")
        self._refresh()

    def turn_start(self, model: str, protocol: str) -> None:
        self._busy = True
        self._stream = []
        self._thinking = []
        self._append(f"\n[model] {model} · {protocol} tools\n")
        self._refresh()

    def turn_end(self) -> None:
        self._flush_turn()
        self._busy = False
        self._refresh()

    def delta(self, text: str) -> None:
        # The stream pane shows the turn as it happens; it is folded into the
        # transcript at round_end so the transcript reads as one conversation.
        self._stream.append(text)
        self._refresh()

    def thinking(self, text: str) -> None:
        self._thinking.append(text)
        self._refresh()

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self._flush_turn()
        self._append(f"\n  ⚙ {summarize_call(name, arguments)}\n")
        self._refresh()

    def tool_result(self, name: str, result: ToolResult) -> None:
        status = "ok" if result.ok else "error"
        self._append(f"    {name} → {status}\n")
        if self.tool_output != "off":
            body = result.text
            if self.tool_output == "summary":
                body = self._clip_lines(body)
            prefix = "" if result.ok else "error: "
            for line in body.splitlines() or [""]:
                self._append(f"      {prefix}{line}\n")
                prefix = ""
        self._refresh()

    def notice(self, text: str) -> None:
        self._append(f"  · {text}\n")
        self._refresh()

    def error(self, text: str) -> None:
        self._append(f"  ! {text}\n")
        self._refresh()

    def status(self, stats: TurnStats) -> None:
        self._last_stats = stats
        parts = []
        if stats.prompt_tokens:
            parts.append(f"prompt {stats.prompt_tokens}")
        if stats.output_tokens:
            rate = stats.output_tokens / stats.seconds if stats.seconds else 0
            parts.append(f"out {stats.output_tokens} ({rate:.1f} tok/s)")
        parts.append(f"{stats.seconds:.1f}s")
        if stats.done_reason and stats.done_reason != "stop":
            parts.append(stats.done_reason)
        self._append(f"  · {' · '.join(parts)}\n")
        self._refresh()

    def round_end(self, stats: TurnStats) -> None:
        self._flush_turn()
        self.status(stats)

    # -- bars --------------------------------------------------------
    def _top_bar(self) -> str:
        chat = self.session.config
        think = "think:on" if self.show_thinking else "think:off"
        return (
            f" K0B0L | {chat.model} | root: {chat.root} | "
            f"tools:{self.tool_output} {think} | Esc menu · /help "
        )

    def _bottom_bar(self) -> str:
        if self._busy:
            return " working… (blocking I/O runs off the event loop; Esc, F10 stay live) "
        return " ready "

    def _run_bar(self) -> str:
        return " " + self.runs.status_line() + " "

    def _run_active(self) -> bool:
        return self.runs is not None and self.runs.ever_started

    def _tick(self) -> None:
        """Event-loop heartbeat: refresh the run bar while a run is alive."""
        if self._app is None:
            return
        if self.runs is not None and self.runs.ever_started:
            self.runs.poll()
            self._app.invalidate()
            self._app.loop.call_later(1.0, self._tick)

    def _stream_text(self) -> str:
        parts: list[str] = []
        if self.show_thinking and self._thinking:
            parts.append("— reasoning —\n" + "".join(self._thinking))
        text = "".join(self._stream)
        if text:
            parts.append(text)
        if not parts:
            return ""
        return "\n\n" + "\n".join(parts) + "\n"

    # -- menu actions ------------------------------------------------
    def _stage_command(self, command: str) -> None:
        """Put a command into the prompt and hand it the focus."""
        self.input.buffer.text = command  # type: ignore[attr-defined]
        self.input.buffer.cursor_position = len(command)  # type: ignore[attr-defined]
        if self._app is not None:
            self._app.layout.focus(self.input)

    def _set_tools(self, level: str) -> None:
        self.tool_output = level
        self.line(f"tool output → chat: {level}")

    def _toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        self.line(f"reasoning is {'shown' if self.show_thinking else 'hidden'}")

    # -- the paste pad: a modal multiline area ---------------------------
    def _open_paste(self) -> None:
        from prompt_toolkit.layout.containers import Float

        if self._paste_float not in self._floats.floats:
            self._paste_area.buffer.reset()
            self._floats.floats.append(self._paste_float)
            self._app.layout.focus(self._paste_area)  # type: ignore[union-attr]
        self.line("paste pad open: type or paste, F2 sends, Esc cancels")

    def _submit_paste(self) -> None:
        text = self._paste_area.buffer.text.rstrip("\n")  # type: ignore[attr-defined]
        self._close_paste()
        if text.strip():
            self._submit(text)

    def _close_paste(self) -> None:
        if self._paste_float in self._floats.floats:
            self._floats.floats.remove(self._paste_float)
        if self._app is not None:
            self._app.layout.focus(self.input)

    def _make_completer(self) -> Any:
        try:
            from .fs_complete import ChatCompleter

            return ChatCompleter(self.config, self.session.workspace.root)
        except Exception:  # pragma: no cover - degrade to no completions
            return None

    def _show_file(self, path: Path) -> None:
        """Render a local file into the transcript, bounded."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.error(f"cannot read {path}: {exc}")
            return
        self.line(f"--- {path} ---")
        for line in self._clip_lines(text, 40).splitlines():
            self.line(line)

    # -- modals ------------------------------------------------------------
    def _close_float(self, float_obj: Any, refocus: Any = None) -> None:
        if float_obj in self._floats.floats:
            self._floats.floats.remove(float_obj)
        if self._app is not None:
            self._app.layout.focus(refocus or self.input)

    def _open_help(self) -> None:
        from prompt_toolkit.layout.containers import Float, HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import Frame, TextArea

        if getattr(self, "_help_float", None) is None:
            area = TextArea(read_only=True, scrollbar=True, focusable=True,
                            wrap_lines=True, style="class:modal")
            area.buffer.text = HELP_PANEL + "\n" + FS_HELP
            self._help_float = Float(
                content=Frame(
                    HSplit([
                        area,
                        Window(FormattedTextControl("F1 or Esc closes"), height=1,
                               style="class:modalbar"),
                    ]),
                    title="help",
                ),
                left=6, right=6, top=1, bottom=1,
            )
            self._help_area = area
        if self._help_float not in self._floats.floats:
            self._floats.floats.append(self._help_float)
            self._app.layout.focus(self._help_area)  # type: ignore[union-attr]

    def _close_help(self) -> None:
        self._close_float(self._help_float, self.input)

    def _open_file_picker(self, on_pick: Callable[[Path], None]) -> None:
        """A modal file picker rooted at the workspace."""
        from prompt_toolkit.layout.containers import Float, HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import Frame, TextArea

        state = {"dir": self.session.workspace.root, "picked": None}
        listing = TextArea(read_only=True, scrollbar=True, focusable=True,
                           wrap_lines=False, style="class:modal")
        heading = Window(FormattedTextControl(lambda: str(state["dir"]) + "  — ↑/↓ move · Enter open/pick · Backspace up · Esc cancel"),
                         height=1, style="class:modalbar")
        self._picker_label = Window(FormattedTextControl(""), height=1, style="class:modalbar")

        def refresh(select: int = 0) -> None:
            entries: list[Path] = []
            try:
                if state["dir"] != self.session.workspace.root.root:
                    entries.append(state["dir"].parent)
                for entry in sorted(state["dir"].iterdir()):
                    if entry.name.startswith("."):
                        continue
                    entries.append(entry)
            except OSError as exc:
                listing.buffer.text = f"cannot list {state['dir']}: {exc}"
                state["entries"] = []
                return
            state["entries"] = entries
            state["sel"] = max(0, min(select, len(entries) - 1))
            rows = []
            for i, entry in enumerate(entries):
                marker = "❯" if i == state["sel"] else " "
                name = ".." if entries and i == 0 and entry == state["dir"].parent else entry.name + ("/" if entry.is_dir() else "")
                rows.append(f"{marker} {name}")
            listing.buffer.text = "\n".join(rows) or "(empty)"

        state["refresh"] = refresh
        refresh()

        float_obj = Float(
            content=Frame(HSplit([heading, listing, self._picker_label]), title="choose a base prompt"),
            left=6, right=6, top=1, bottom=1,
        )
        self._picker_float = float_obj
        self._picker_state = state
        self._picker_cb = on_pick

        if float_obj not in self._floats.floats:
            self._floats.floats.append(float_obj)
            self._app.layout.focus(listing)  # type: ignore[union-attr]
        self._picker_area = listing

    def _picker_move(self, delta: int) -> None:
        state = self._picker_state
        if not state.get("entries"):
            return
        state["refresh"](state["sel"] + delta)

    def _picker_enter(self) -> None:
        state = self._picker_state
        entries = state.get("entries", [])
        if not entries:
            return
        picked = entries[state["sel"]]
        if picked.is_dir():
            state["dir"] = picked
            state["refresh"](0)
            return
        self._picker_cb(picked)  # type: ignore[misc]
        self._close_picker()

    def _close_picker(self) -> None:
        self._close_float(self._picker_float, self.input)

    # -- the start-run wizard ------------------------------------------------
    WIZARD_FIELDS = ("objective", "target", "modes", "family", "attempts")

    def _open_wizard(self) -> None:
        from prompt_toolkit.layout.containers import Float, HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.widgets import Frame, TextArea

        state = {"step": 0, "values": {k: "" for k in self.WIZARD_FIELDS}}
        instructions = [
            "objective  — what the cycle is trying to make the target do.",
            "target     — the model the run attacks (blank keeps the current target).",
            "modes      — comma list, or blank for all seven in order.",
            "family     — a CL4R1T4S family to mount as the target's deployment (blank = bare).",
            "attempts   — hard cap on attempts (blank = modes × rounds).",
        ]
        input_area = TextArea(height=1, prompt="", multiline=False, wrap_lines=False,
                              style="class:modalinput")
        status = Window(FormattedTextControl(""), height=1, style="class:modalbar")
        guide = Window(
            FormattedTextControl(lambda: instructions[state["step"]]),
            height=1, style="class:modalguide",
        )

        def refresh() -> None:
            field = self.WIZARD_FIELDS[state["step"]]
            input_area.prompt = f"{field}> "
            input_area.buffer.text = str(state["values"][field])
            input_area.buffer.cursor_position = len(input_area.buffer.text)
            # _wizard_step_bar's control holds a lambda; bump the app to redraw.
            if self._app is not None:
                self._app.invalidate()

        self._wizard_input = input_area
        self._wizard_state = state
        step_bar = Window(
            FormattedTextControl(
                lambda: f"step {state['step'] + 1}/{len(self.WIZARD_FIELDS)} — {self.WIZARD_FIELDS[state['step']]}"
            ),
            height=1, style="class:modalbar",
        )
        self._wizard_step_bar = step_bar

        float_obj = Float(
            content=Frame(HSplit([step_bar, guide, input_area, status]), title="start a cycle  (Enter next · Esc cancel)"),
            left=6, right=6, top=1, bottom=2,
        )
        self._wizard_float = float_obj
        state["refresh"] = refresh
        if float_obj not in self._floats.floats:
            self._floats.floats.append(float_obj)
            self._app.layout.focus(input_area)  # type: ignore[union-attr]
        refresh()

    def _wizard_accept(self) -> None:
        state = self._wizard_state
        field = self.WIZARD_FIELDS[state["step"]]
        state["values"][field] = self._wizard_input.buffer.text.strip()  # type: ignore[attr-defined]
        state["step"] += 1
        if state["step"] >= len(self.WIZARD_FIELDS):
            self._close_wizard()
            values = state["values"]
            parts = []
            if values["target"]:
                parts.append(f"target={values['target']}")
            if values["modes"]:
                parts.append(f"modes={values['modes']}")
            if values["family"]:
                parts.append(f"family={values['family']}")
            if values["attempts"]:
                parts.append(f"attempts={values['attempts']}")
            objective = values["objective"]
            command = "/run start " + " ".join(parts + [objective]) if parts else f"/run start {objective}"
            self.line(f"you> {command}")
            self.line(self._run_command(command[len("/run "):]))
            return
        state["refresh"]()

    def _close_wizard(self) -> None:
        self._close_float(self._wizard_float, self.input)

    # -- sending ---------------------------------------------------------
    def _submit(self, text: str) -> None:
        """The prompt's accept handler: dispatch or schedule the turn."""
        if not text.strip():
            return
        # Commands must always be handled: queuing them behind a busy turn is
        # what makes /exit (and /run status) appear to hang under streaming.
        head = text.strip().split(None, 1)[0].lower()
        if self._busy and not head.startswith("/"):
            self.notice("a turn is already running; wait for the status line")
            return
        self.line(f"you> {text}")
        try:
            if self.dispatch(text):
                return
        except EOFError:
            # /exit is a command-level EOF; inside the Application that means
            # "leave the UI", not "kill the reader", so translate it. It must
            # run on the event loop, not from the worker thread's done-callback
            # machinery, or the exit races the repaint and is lost.
            app = self._app
            if app is not None:
                app.loop.call_soon(self._exit)
            return
        self._busy = True
        self._refresh()
        # A plain daemon thread, not the loop's executor: run_in_executor ties
        # the future to the default ThreadPoolExecutor, whose non-daemon workers
        # keep this process alive after app.exit when a call is still in flight.
        worker = threading.Thread(
            target=self._turn_worker, args=(text,), daemon=True, name="k0b0l-turn"
        )
        worker.start()

    def _turn_worker(self, text: str) -> None:
        """Runs off the event loop; reports back by call_soon_threadsafe."""
        error: Exception | None = None
        try:
            self.ask(text)
        except Exception as exc:  # noqa: BLE001 - the UI must learn about it
            error = exc
        if self._app is not None:
            self._app.loop.call_soon_threadsafe(self._turn_done, error)

    def _turn_done(self, error: Exception | None) -> None:
        # Back on the event loop.
        self._busy = False
        if error is not None:
            if isinstance(error, OllamaError):
                self.error(str(error))
            else:
                self.error(f"the turn failed: {error}")
        self._refresh()

    def _exit(self) -> None:
        if self._app is not None:
            app = self._app
            self._app = None
            app.exit(result=0)

    # -- the loop --------------------------------------------------------
    def loop(self, input_fn: Callable[[str], str] | None = None) -> int:
        from prompt_toolkit.application import Application
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import (
            ConditionalContainer,
            Float,
            FloatContainer,
            HSplit,
            VSplit,
            Window,
        )
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import TextArea

        self.transcript = TextArea(
            read_only=True,
            scrollbar=True,
            focusable=True,
            wrap_lines=True,
            style="class:transcript",
        )
        from .themes import migrate_state

        history_path = migrate_state(Path.home() / ".k0b0l-chat-history")
        self.input = TextArea(
            height=1,
            prompt="you> ",
            multiline=False,
            wrap_lines=False,
            history=FileHistory(str(history_path)),
            completer=self._make_completer(),
            complete_while_typing=False,
            accept_handler=lambda buffer: self._accept(),
            style="class:prompt",
        )
        self._stream_pane = Window(
            FormattedTextControl(self._stream_text, focusable=False),
            dont_extend_height=True,
        )
        self._run_pane = Window(
            FormattedTextControl(self._run_bar, focusable=False), height=1,
            style="class:runbar",
        )

        kb = KeyBindings()

        from prompt_toolkit.keys import Keys

        @kb.add(Keys.Escape)
        def _menu_or_cancel(event: Any) -> None:
            if self._paste_float in self._floats.floats:
                self._close_paste()
                return
            if getattr(self, "_help_float", None) in self._floats.floats:
                self._close_help()
                return
            if getattr(self, "_picker_float", None) in self._floats.floats:
                self._close_picker()
                return
            if getattr(self, "_wizard_float", None) in self._floats.floats:
                self._close_wizard()
                return
            menu = self._menu_container.window  # type: ignore[union-attr]
            if self._app.layout.has_focus(menu):
                self._app.layout.focus(self.input)  # type: ignore[union-attr]
            else:
                self._app.layout.focus(menu)  # type: ignore[union-attr]

        @kb.add(Keys.F1)
        def _help(event: Any) -> None:
            self._open_help() if getattr(self, "_help_float", None) not in self._floats.floats else self._close_help()

        @kb.add(Keys.F2)
        def _paste_send(event: Any) -> None:
            if self._paste_float in self._floats.floats:
                self._submit_paste()

        @kb.add("c-q")
        def _quit(event: Any) -> None:
            self._exit()

        @kb.add("up")
        def _picker_up(event: Any) -> None:
            if getattr(self, "_picker_float", None) in self._floats.floats:
                self._picker_move(-1)
            elif getattr(self, "_wizard_float", None) in self._floats.floats:
                pass
            else:
                event.current_buffer.history_backward()

        @kb.add("down")
        def _picker_down(event: Any) -> None:
            if getattr(self, "_picker_float", None) in self._floats.floats:
                self._picker_move(1)
            else:
                event.current_buffer.history_forward()

        @kb.add("enter")
        def _enter(event: Any) -> None:
            if getattr(self, "_picker_float", None) in self._floats.floats:
                self._picker_enter()
            elif getattr(self, "_wizard_float", None) in self._floats.floats:
                self._wizard_accept()
            else:
                event.current_buffer.validate_and_handle()

        @kb.add("backspace")
        def _backspace(event: Any) -> None:
            if getattr(self, "_picker_float", None) in self._floats.floats:
                state = self._picker_state
                if state["dir"] != state["dir"].parent:
                    state["dir"] = state["dir"].parent
                    state["refresh"](0)
            else:
                event.current_buffer.delete_before_cursor()

        self._kb = kb

        top = Window(FormattedTextControl(self._top_bar), height=1, style="class:top")
        bottom = Window(FormattedTextControl(self._bottom_bar), height=1,
                        style="class:bottom")
        root_column = HSplit([
            top,
            self.transcript,
            ConditionalContainer(self._stream_pane,
                                 filter=Condition(self._stream_visible)),
            ConditionalContainer(self._run_pane,
                                 filter=Condition(self._run_active)),
            bottom,
            self.input,
        ])
        self._body = root_column
        self._floats = FloatContainer(content=root_column, floats=[])

        self._paste_area = TextArea(multiline=True, scrollbar=True,
                                    style="class:paste",
                                    prompt="paste> ")
        from prompt_toolkit.widgets import Frame
        self._paste_float = Float(
            content=Frame(HSplit([
                self._paste_area,
                Window(FormattedTextControl(
                    "F2 send · Esc cancel"), height=1, style="class:bottom"),
            ])),
            left=4, right=4, top=2, bottom=2,
        )

        @kb.add(PASTE_SUBMIT_KEY)
        def _paste_send_legacy(event: Any) -> None:
            # `f2` and Keys.F2 both bound: prompt_toolkit resolves whichever name
            # the key sequence maps to on this terminal.
            if self._paste_float in self._floats.floats:
                self._submit_paste()

        self._menu_container = self._menu_for(root_column)
        app_column = HSplit([self._menu_container])
        self._floats.content = app_column

        style = Style.from_dict({
            "top": "reverse bg:#1f3a5f #ffffff",
            "bottom": "reverse bg:#1f3a5f #ffffff",
            "runbar": "bg:#3a3a00 #ffffff",
            "paste": "bg:#202020 #ffffff",
            "modal": "bg:#101820 #e5e5e5",
            "modalbar": "bg:#243447 #aaaaaa",
            "modalguide": "bg:#101820 #d0d0d0",
            "modalinput": "bg:#101820 #ffffff",
            "prompt": "bg:#000000 #ffffff",
        })

        self._app = Application(
            layout=Layout(self._floats, focused_element=self.input),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
            style=style,
        )
        # Our own /paste opens the modal pad rather than reading stdin: the
        # application owns the terminal now, so nothing else may read it.
        self.reader = None
        self._paste_override()
        self._help_override()
        self.greet()
        self._tick()
        self._app.run()
        if self.runs is not None and self.runs.running:
            self.runs.stop()
        self._app = None
        return 0

    # The menu builder above needs the body before it exists; build the menu
    # lazily once the layout is up, keeping MenuContainer as the root so its
    # drop-downs render above every pane.
    def _menu_for(self, body: Any) -> Any:
        from prompt_toolkit.widgets import MenuContainer, MenuItem

        from . import catalog

        def mi(label: str, handler: Callable[[], None] | None = None,
               children: list[Any] | None = None) -> Any:
            return MenuItem(label, handler=handler, children=children or [])

        current_target = self.config.role("TARGET").model
        target_items: list[Any] = []
        for name in catalog.known_models(self.config):
            mark = "● " if name == current_target else "  "
            target_items.append(mi(
                f"{mark}{name}",
                lambda n=name: self.dispatch(f"/target {n}"),
            ))
        if not target_items:
            target_items = [mi("(no models on the server; OLLAMA_URL reachable?)")]
        target_items.append(mi("Other…", lambda: self._stage_command("/target ")))

        chat_items: list[Any] = []
        current_chat = self.session.config.model
        for name in catalog.known_models(self.config):
            mark = "● " if name == current_chat else "  "
            chat_items.append(mi(
                f"{mark}{name}",
                lambda n=name: self.dispatch(f"/model {n}"),
            ))
        if not chat_items:
            chat_items = [mi("(no models on the server)")]
        chat_items.append(mi("Other…", lambda: self._stage_command("/model ")))

        mode_items = [mi(f"{name} — {one}", lambda n=name: self._stage_command(f"/run start modes={n} "))
                      for name, one in catalog.mode_choices()]
        mode_items.append(mi("All modes (default)", lambda: self._stage_command("/run start ")))

        family_items = [mi(family, lambda f=family: self._stage_command(f"/run start family={f} "))
                        for family in catalog.deployment_choices()]
        if not family_items:
            family_items = [mi("(catalog not generated; run tools/gen_catalog.py)")]

        winner_items: list[Any] = []
        winners_dir = self.config.logdir / "winning-prompts"
        if winners_dir.is_dir():
            for path in sorted(winners_dir.glob("winner-*.md"), reverse=True)[:10]:
                winner_items.append(
                    mi(path.name, lambda p=path: self._show_file(p))
                )
        winner_items = winner_items or [mi("(no winning prompts archived yet)")]

        return MenuContainer(
            body=body,
            menu_items=[
                mi("Session", children=[
                    mi("Chat model", children=chat_items),
                    mi("Target model", children=target_items),
                    mi("Base prompt from file…", lambda: self._open_file_picker(
                        lambda p: self._stage_command(f"/run start ofile={p} ")
                    )),
                    mi("Change workspace root…", lambda: self._stage_command("/root ")),
                    mi("Paste a block", self._open_paste),
                    mi("Clear conversation", lambda: self.dispatch("/clear")),
                    mi("History", lambda: self.dispatch("/history")),
                ]),
                mi("Run", children=[
                    mi("Start run (wizard)…", self._open_wizard),
                    mi("Start run…", lambda: self._stage_command("/run start ")),
                    mi("…with mode", children=mode_items),
                    mi("…under deployment family", children=family_items),
                    mi("Run status", lambda: self.dispatch("/run status")),
                    mi("Tail run log", lambda: self.dispatch("/run tail")),
                    mi("Stop run", lambda: self.dispatch("/run stop")),
                    mi("Winning prompts", children=winner_items),
                ]),
                mi("View", children=[
                    mi("Tool output: off", lambda: self._set_tools("off")),
                    mi("Tool output: summary", lambda: self._set_tools("summary")),
                    mi("Tool output: full", lambda: self._set_tools("full")),
                    mi("Toggle reasoning", self._toggle_thinking),
                    mi("Help card", lambda: self.dispatch("/help")),
                ]),
                mi("Exit", handler=self._exit),
            ],
        )

    def _paste_override(self) -> None:
        """/paste in full screen means the modal pad, not stdin reads."""
        base = self.dispatch

        def dispatch(text: str) -> bool:
            if text.strip().lower() == "/paste":
                self._open_paste()
                return True
            return base(text)

        self.dispatch = dispatch  # type: ignore[method-assign]

    def _help_override(self) -> None:
        """/help answers for this front end, not the shared line-mode one."""
        prior = self.dispatch

        def combined(text: str) -> bool:
            if text.strip().lower() == "/help":
                self.line(FS_HELP.rstrip())
                return True
            return prior(text)

        self.dispatch = combined  # type: ignore[method-assign]

    def _stream_visible(self) -> bool:
        return bool(self._stream or (self.show_thinking and self._thinking))

    def _accept(self) -> None:
        text = self.input.buffer.text  # type: ignore[attr-defined]
        self.input.buffer.reset()  # type: ignore[attr-defined]
        self._submit(text)


def run_fullscreen(config: Any) -> int | None:
    """Entry point for the full-screen chat; ``None`` when unavailable."""
    from .tui import _pick_client, ask_endpoint_on_failure

    client = _pick_client(config)
    try:
        client.version()
    except OllamaError as exc:
        retry_url = ask_endpoint_on_failure(config, running=True)
        if retry_url != config.api_url:
            config.ollama_url = retry_url
            client = _pick_client(config)
        try:
            client.version()
        except OllamaError:
            print(f"Ollama is not reachable at {config.ollama_url}: {exc}")
            return 1
    chat = config.chat
    if not Path(chat.root).is_dir():
        print(f"chat workspace root is not a directory: {chat.root}")
        return 1
    ui = FullScreenChat(
        client, config, tool_output=chat.tool_output, show_thinking=config.show_thinking
    )
    return ui.loop()
