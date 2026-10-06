"""The interactive chat front end.

Two renderers, picked at startup. With ``rich`` and ``prompt_toolkit`` available
the session streams the reply into a live region, renders markdown, styles each
tool call as a panel, and keeps input history. Without them it degrades to a
plain line-mode loop that does the same things with ``print``.

Tool output is rendered *into the conversation*, never dumped straight to the
terminal: every call and its result becomes part of the transcript, and
``/tools off|summary|full`` decides how much of the result is shown. That is the
difference between a chat you can scroll back through and a shell session where
the model's reasoning and the tool noise are interleaved on different streams.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TextIO

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .runctl import RunController

from . import themes as retro_themes
from .chat import ChatHooks, ChatSession, TurnStats
from .client import OllamaClient, OllamaError
from .config import ChatConfig, Config
from .runctl import RunController
from .tools import TOOL_NAMES, TOOL_VERBOSITY, ToolResult, summarize_call

#: Lines of a tool result shown at ``summary`` verbosity.
SUMMARY_LINES = 12

#: A chat turn can legitimately take minutes on this hardware.
CHAT_TIMEOUT = 3600.0

#: Block kinds that can be folded away. The transcript owns the same list; a
#: front end that cannot fold simply ignores these.
FOLD_KINDS = retro_themes.BLOCK_KINDS

HELP = """commands
  /help              this list
  /tools <level>     tool output in the chat: off, summary, full
  /think on|off      reasoning blocks: unfolded, or folded away (off)
  /fold [what]       fold reasoning|code|all (default all); /unfold opens them
  /read <path>       print a file into the chat
  /docs [topic]      list the product docs, or print one (the model has read_docs)
  /soul              show the soul file this session loaded
  /skills list|enable|disable   manage skills: list, or pin one into every turn
  /skills edit [name]          open the skills editor (Textual front end)
  /session save|load|list <name>  persist or resume this chat
  /run start|status|tail|stop   spawn and watch a cycle run (operator-only)
  /paste             read a multi-line block; finish with a line containing only .
  /model [name]      show or switch the chat model
  /models            list the models the endpoint reports
  /target [name]     show or set the model the next /run targets
  /root [path]       show or change the workspace root
  /clear             forget the conversation (keeps the system message)
  /history           messages held, and how many tool calls have run
  /compact           summarize the conversation and continue from the summary
  /image [model] <prompt>   generate an image locally (pony|qwen|chroma)
  /exit              leave (ctrl-d works too)
"""


def ask_endpoint_on_failure(config: "Config", *, running: bool) -> str:
    """When localhost is dead, the operator probably meant a remote API.

    Every chat/loop entry point probes the endpoint before doing anything
    else. A connection failure on a localhost address is usually not a
    broken harness but a different intent: the model is served somewhere
    else (a LAN box, a rented GPU, a remote /v1 endpoint). Rather than
    exiting, ask once -- interactive TTYs only -- and retry the probe with
    whatever comes back.

    Returns the URL to use (the original when the operator declines or no
    TTY is available). The answer is echoed back with the API_URL hint so
    the next start does not have to ask again.
    """
    import sys
    from urllib.parse import urlparse

    if not running or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return config.api_url
    host = urlparse(config.api_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return config.api_url  # already pointed elsewhere; leave it alone
    try:
        print(
            f"\nThe model endpoint at {config.api_url} is not reachable.\n"
            "If your models are served remotely, enter that endpoint now\n"
            "(e.g. http://192.168.1.50:11434 or https://host.example/v1);\n"
            "press Enter to give up.",
            file=sys.stderr,
        )
        answer = input("endpoint url (empty = exit): ").strip()
    except (EOFError, KeyboardInterrupt):
        return config.api_url
    if not answer:
        return config.api_url
    # Accept a bare host:port the way an operator types it. Whether the
    # native or OpenAI client is picked follows the same rule as always:
    # _pick_client decides by the /v1 suffix, not by the host.
    if "://" not in answer:
        answer = "http://" + answer
    # Persist the answer as the saved default so every subsequent load
    # uses it without asking again; API_URL still overrides when set.
    from .catalog import set_default_api

    if set_default_api(answer):
        print(
            f"using {answer} (saved as your default; set API_URL to override)",
            file=sys.stderr,
        )
    else:
        print(
            f"using {answer} for this session (could not save it permanently)",
            file=sys.stderr,
        )
    return answer


def _pick_client(config: Config) -> Any:
    """Ollama-shaped endpoints get the native client; /v1 endpoints get the
    OpenAI chat-completions one. Authorization (if the endpoint record carries
    a key) comes from ~/.k0b0l-apis.json.
    """
    from .catalog import _apis_path
    from .client import OpenAIClient

    base = config.ollama_url.rstrip("/")
    if base.endswith("/v1"):
        key = ""
        try:
            for entry in __import__("json").loads(_apis_path().read_text(encoding="utf-8")):
                if isinstance(entry, dict) and entry.get("base_url", "").rstrip("/") == base:
                    key = str(entry.get("api_key", ""))
                    break
        except (OSError, ValueError):
            pass
        return OpenAIClient(base, api_key=key, timeout=CHAT_TIMEOUT)
    return OllamaClient(base, timeout=CHAT_TIMEOUT)


class ChatUI(ChatHooks):
    """Session lifecycle, slash commands, and output clipping.

    Subclasses supply the rendering. Everything else -- building the session,
    switching models, changing the workspace root, deciding how much tool output
    to show -- lives here so both front ends behave identically.
    """

    def __init__(
        self,
        client: OllamaClient,
        config: Config,
        *,
        tool_output: str = "summary",
        show_thinking: bool = False,
    ) -> None:
        self.client = client
        self.config = config
        self.tool_output = tool_output if tool_output in TOOL_VERBOSITY else "summary"
        self.show_thinking = show_thinking
        self._selected_skills: list[str] = []
        #: MCP servers named by the config, or None when the session runs without
        #: them. Built before the session so the first turn already knows them.
        self.mcp = self._build_mcp(config.chat)
        self.session = self._build(config.chat)
        #: Operator-owned single-run spawner (see runctl.py). Not a model tool.
        self.runs = RunController(config)
        #: Set by each ``loop``. Slash commands that need more lines than one
        #: must read them from whatever owns the terminal, not from stdin --
        #: prompt_toolkit has it in the rich front end.
        self.reader: Callable[[str], str] | None = None

    # -- session lifecycle ------------------------------------------------
    def _build_mcp(self, chat: ChatConfig):
        """Read the MCP config and dial it on a background thread.

        ``npx``-style servers take seconds to answer ``initialize``, and none of
        that belongs in front of the prompt: the registry is handed to the
        session straight away (so the model sees whatever is already connected)
        and fills in as servers come up. A config that cannot be read is
        recorded, not raised -- ``/mcp`` will say what happened.
        """
        if not getattr(chat, "mcp", False):
            return None
        from .mcp import MCPRegistry

        try:
            registry = MCPRegistry(getattr(chat, "mcp_config", "") or None, root=chat.root)
            registry.load()
        except Exception as exc:  # noqa: BLE001 - a bad config must not stop the chat
            trace = getattr(self, "notice", None)
            if callable(trace):
                trace(f"MCP config could not be read: {exc}")
            return None
        if registry.servers:
            threading.Thread(
                target=registry.start_all, name="mcp-start", daemon=True
            ).start()
        return registry

    def _build(self, chat: ChatConfig) -> ChatSession:
        from .infotools import InfoTools

        return ChatSession(
            self.client, chat, hooks=self, info=InfoTools(self.config), mcp=self.mcp
        )

    def _skills(self) -> list[Any]:
        from .skills import list_skills

        return list_skills(self.session.workspace.root)

    def _skills_all(self) -> list[Any]:
        """Every discovered skill: the progressive-disclosure index source.

        The system prompt is built *inside* ChatSession.__init__, so the
        session attribute does not exist yet on the first call; an empty
        index then is correct (nothing is discovered before the workspace
        is known).
        """
        session = getattr(self, "session", None)
        if session is None:
            return []
        from .skills import list_skills

        return list_skills(session.workspace.root)

    def _skills_active(self) -> list[Any]:
        from .skills import read_skill

        out = []
        for name in self._selected_skills:
            found = read_skill(self._skills(), name)
            if found is not None:
                out.append(found)
        return out

    def rebuild(self, **changes: Any) -> None:
        """Replace the chat settings and start a fresh session."""
        self.config.chat = replace(self.config.chat, **changes)
        self.session = self._build(self.config.chat)

    # -- rendering hooks the subclasses implement -------------------------
    def line(self, text: str = "") -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def refresh_folds(self, kind: str, folded: bool) -> None:
        """Fold or unfold the ``kind`` blocks already written.

        Only the full-screen chat keeps blocks to fold, so the base does
        nothing: there is nothing on screen to change and nothing to say.
        """
        del kind, folded

    def turn_start(self, model: str, protocol: str) -> None:
        self.line(f"[{model} · {protocol} tools]")

    def delta(self, text: str) -> None:
        self.line(text)

    def thinking(self, text: str) -> None:
        if self.show_thinking:
            self.line(text)

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.line(f"  ⚙ {summarize_call(name, arguments)}")

    def tool_result(self, name: str, result: ToolResult) -> None:
        # This label is the only thing that says which call a body belongs to,
        # so it is printed even when the body itself is suppressed.
        self.line(f"    {name} → {'ok' if result.ok else 'error'}")
        if self.tool_output == "off":
            return
        body = result.text
        if self.tool_output == "summary":
            body = self._clip_lines(body)
        prefix = "" if result.ok else "error: "
        for line in body.splitlines() or [""]:
            self.line(f"      {prefix}{line}")
            prefix = ""

    def notice(self, text: str) -> None:
        self.line(f"  · {text}")

    def show_image(self, relative_path: str) -> None:
        """An image arrived; the base front end can only name it."""
        self.line(f"  · image saved: {relative_path}")

    def error(self, text: str) -> None:
        self.line(f"  ! {text}")

    def status(self, stats: TurnStats) -> None:
        parts = []
        if stats.prompt_tokens:
            parts.append(f"prompt {stats.prompt_tokens}")
        if stats.output_tokens:
            rate = stats.output_tokens / stats.seconds if stats.seconds else 0
            parts.append(f"out {stats.output_tokens} ({rate:.1f} tok/s)")
        parts.append(f"{stats.seconds:.1f}s")
        if stats.done_reason and stats.done_reason != "stop":
            parts.append(stats.done_reason)
        self.line(f"  · {' · '.join(parts)}")

    def round_end(self, stats: TurnStats) -> None:
        self.status(stats)

    @staticmethod
    def _clip_lines(text: str, limit: int = SUMMARY_LINES) -> str:
        lines = text.splitlines()
        if len(lines) <= limit:
            return text
        shown = "\n".join(lines[:limit])
        return f"{shown}\n… {len(lines) - limit} more lines (/tools full to show)"

    # -- commands ---------------------------------------------------------
    def dispatch(self, text: str) -> bool:
        """Handle a slash command. Returns True when the input was consumed."""
        if not text.startswith("/"):
            return False
        parts = text.split(maxsplit=1)
        command, argument = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

        if command in ("/exit", "/quit", "/q"):
            raise EOFError
        if command == "/help":
            self.line(HELP.rstrip())
        elif command == "/tools":
            if argument in TOOL_VERBOSITY:
                self.tool_output = argument
                self.line(f"tool output → chat: {argument}")
            else:
                self.line(f"tool output is {self.tool_output!r}; use {'|'.join(TOOL_VERBOSITY)}")
        elif command == "/think":
            if argument in ("on", "off"):
                self.show_thinking = argument == "on"
            self.line(f"reasoning is {'shown' if self.show_thinking else 'hidden'}")
            self.refresh_folds("reasoning", not self.show_thinking)
        elif command in ("/fold", "/unfold"):
            folded = command == "/fold"
            kinds = [word for word in argument.split() if word]
            if not kinds or kinds == ["all"]:
                kinds = list(FOLD_KINDS)
            unknown = [word for word in kinds if word not in FOLD_KINDS]
            if unknown:
                self.line(f"fold what? reasoning|code|all (not {', '.join(unknown)})")
            else:
                for kind in dict.fromkeys(kinds):
                    if kind == "reasoning":
                        # Folding reasoning is a standing choice as well as a
                        # one-off: the next block arrives the way this leaves it.
                        self.show_thinking = not folded
                    self.refresh_folds(kind, folded)
                self.line(f"{'folded' if folded else 'unfolded'} {', '.join(dict.fromkeys(kinds))}")
        elif command == "/read":
            if not argument:
                self.line("usage: /read <path>")
            else:
                result = self.session.workspace.call("read_file", {"path": argument})
                for line in result.text.splitlines():
                    self.line(line)
        elif command == "/paste":
            reader = self.reader
            if reader is None:
                self.line("no interactive input available for /paste")
            else:
                self.line("paste a block; end it with a line containing only .")
                pasted: list[str] = []
                while True:
                    raw = reader("... ")
                    if raw.strip() == ".":
                        break
                    pasted.append(raw)
                if pasted:
                    self.ask("\n".join(pasted))
        elif command == "/model":
            if not argument:
                self.line(f"model: {self.session.config.model}")
            else:
                self.rebuild(model=argument, protocol="auto")
                self.line(f"model: {argument} (fresh session)")
        elif command == "/models":
            # The endpoint is authoritative: a router server lists every model
            # it can serve, and a single-model server lists the one it loaded.
            from .client import OllamaError, format_model_row, make_client

            try:
                rows = make_client(self.config.api_url).model_details()
            except OllamaError as exc:
                self.line(f"cannot list models: {exc}")
            else:
                if not rows:
                    self.line("the server reports no models")
                else:
                    self.line(f"models ({len(rows)}):")
                    for row in rows:
                        self.line("  " + format_model_row(row))
        elif command == "/target":
            from dataclasses import replace as _replace

            if not argument:
                self.line(f"target: {self.config.role('TARGET').model or '(none set)'}")
            else:
                self.config.roles["TARGET"] = _replace(self.config.role("TARGET"), model=argument)
                self.line(f"target model: {argument} (used by the next /run start)")
        elif command == "/root":
            if not argument:
                self.line(f"root: {self.session.workspace.root}")
            else:
                self.rebuild(root=Path(argument).expanduser().resolve())
                self.line(f"root: {self.session.workspace.root} (fresh session)")
        elif command == "/clear":
            self.session.reset()
            self.line("conversation cleared")
        elif command == "/history":
            self.line(
                f"{len(self.session.messages)} messages, {self.session.turns} turns, "
                f"{self.session.tool_calls_made} tool calls"
            )
        elif command == "/compact":
            self.line(self.session.compress_command())
        elif command == "/image":
            # The operator's shortcut into the same generate_image path the
            # model has: guards, models, and rendering all match.
            from .tools import Workspace

            rest = argument.strip()
            if not rest or rest in ("list", "help"):
                self.line("usage: /image <prompt>   (or: /image pony|qwen|chroma <prompt>)")
                self.line("models: pony (fast, tags), qwen (adherent), chroma (flux-class)")
                return True
            parts = rest.split(maxsplit=1)
            model = "pony"
            prompt = rest
            if parts[0].lower() in ("pony", "qwen", "chroma") and len(parts) > 1:
                model, prompt = parts[0].lower(), parts[1].strip()
            ws = Workspace(self.session.workspace.root, allow_exec=False,
                           ui=self)
            result = ws.call("generate_image",
                            {"prompt": prompt, "model": model})
            self.line(result.text)
        elif command == "/skills":
            verb, _, rest = argument.partition(" ")
            verb, rest = verb.strip().lower(), rest.strip()
            skills = self._skills()
            if verb == "list":
                if not skills:
                    self.line(f"no skills under {self.session.workspace.root}/skills or ~/.k0b0l-skills")
                else:
                    self.line("skills: " + ", ".join(skill.name for skill in skills))
            elif verb == "enable":
                if not rest:
                    self.line("usage: /skills enable <name>")
                else:
                    if rest not in self._selected_skills:
                        self._selected_skills.append(rest)
                    self.session.reset()
                    self.line(f"skills active: {', '.join(self._selected_skills)}")
            elif verb == "disable":
                if not rest:
                    self.line("usage: /skills disable <name>")
                else:
                    self._selected_skills = [n for n in self._selected_skills if n != rest]
                    self.session.reset()
                    self.line(f"skills active: {', '.join(self._selected_skills) or '(none)'}")
            elif verb == "edit":
                # Only the full-screen front end has a modal editor; the base
                # says so rather than pretending a line mode could host one.
                self.line("the skills editor needs the Textual front end "
                          "(start the chat without --chat-plain)")
            else:
                self.line("usage: /skills list | enable <name> | disable <name> | edit [name]")
        elif command == "/mcp":
            self._command_mcp(argument)
        elif command == "/exec":
            self._command_exec(argument)
        elif command == "/docs":
            from .docs import docs_dir, list_docs, read_doc

            if not argument:
                docs = list_docs(self.config)
                self.line(f"docs under {docs_dir(self.config)}:")
                for doc in docs:
                    self.line(f"  {doc.name:<12} {doc.title}")
                    if doc.summary:
                        self.line(f"  {'':<12} {doc.summary}")
                if not docs:
                    self.line("  (none installed)")
                self.line("usage: /docs <topic>")
            else:
                text = read_doc(self.config, argument)
                if text is None:
                    self.line(f"no document {argument!r}; /docs lists them")
                else:
                    self.line(text)
        elif command == "/soul":
            from .soul import load_soul

            soul = load_soul(self.config, self.session.workspace.root)
            self.line(soul.describe())
            if soul.loaded:
                self.line("")
                self.line(soul.text)
        elif command == "/session":
            verb, _, rest = argument.partition(" ")
            verb, rest = verb.strip().lower(), rest.strip()
            if verb == "save":
                if not rest:
                    self.line("usage: /session save <name>")
                else:
                    self._session_save(rest)
            elif verb == "load":
                if not rest:
                    self.line("usage: /session load <name>")
                else:
                    self._session_load(rest)
            elif verb == "list":
                from .infotools import _session_dir

                try:
                    names = sorted(p.stem for p in _session_dir().glob("*.json"))
                except OSError as exc:
                    self.line(f"cannot list sessions: {exc}")
                    return True
                self.line("saved sessions: " + (", ".join(names) if names else "(none)"))
            else:
                self.line("usage: /session save <name> | /session load <name> | /session list")
        elif command == "/run":
            self.line(self._run_command(argument))
        else:
            self.line(f"unknown command {command!r}; /help")
        return True

    # -- session persistence --------------------------------------------------
    def _session_path(self, name: str) -> Path:
        from .infotools import _session_dir

        return _session_dir() / (name + ".json")

    def _session_save(self, name: str) -> None:
        path = self._session_path(name)
        import json

        payload = {"messages": self.session.messages}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.line(f"session saved to {path}")

    def _session_load(self, name: str) -> None:
        path = self._session_path(name)
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            self.line(f"no session named {name!r}")
            return
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self.line(f"session {name!r} is empty or unreadable")
            return
        self.session.messages[:] = messages
        self.session.turns = sum(1 for m in messages if m.get("role") == "user")
        self.session.tool_calls_made = sum(
            len((m.get("tool_calls") or [])) for m in messages
        )
        self.line(f"session {name!r} loaded ({len(messages)} messages)")

    def _run_command(self, argument: str) -> str:
        """The /run family: operator-side cycle control, not a model tool."""
        if self.runs is None:
            return "run control is unavailable in this session"
        verb, _, rest = argument.partition(" ")
        verb, rest = verb.strip().lower(), rest.strip()
        if verb == "start":
            from .runctl import parse_start_args

            parsed = parse_start_args(rest)
            if parsed is None:
                return "usage: /run start [target=M] [modes=a,b] [attempts=N] <objective>"
            objective, overrides = parsed
            return self.runs.start(
                objective,
                modes=overrides.get("modes", ""),
                attempts=int(overrides["attempts"]) if overrides.get("attempts", "").isdigit() else 0,
                target=overrides.get("target", ""),
                family=overrides.get("family", ""),
                skills=overrides.get("skills", ""),
            )
        if verb == "status":
            return self.runs.status()
        if verb == "stop":
            return self.runs.stop()
        if verb == "tail":
            count = int(rest) if rest.isdigit() else 15
            return self.runs.tail(count)
        return "usage: /run start [...] <objective> | /run status | /run tail [N] | /run stop"

    # -- the loop ---------------------------------------------------------
    def ask(self, text: str) -> str:
        """Send one message and render the turn. Subclasses wrap this."""
        return self.session.send(text)

    def loop(self, input_fn: Callable[[str], str]) -> int:
        raise NotImplementedError  # pragma: no cover - abstract

    def greet(self) -> None:
        for key, value in self.session.describe():
            self.line(f"{key:>10}: {value}")
        self.line("type /help for commands, /exit to leave")


class PlainChat(ChatUI):
    """Line-mode fallback: same behaviour, no styling, no live region."""

    def __init__(self, *args: Any, out: TextIO | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.out = out or sys.stdout
        self._at_line_start = True

    def line(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)
        self._at_line_start = True

    def delta(self, text: str) -> None:
        print(text, file=self.out, end="", flush=True)
        self._at_line_start = text.endswith("\n")

    def loop(self, input_fn: Callable[[str], str]) -> int:
        self.greet()
        self.reader = input_fn
        while True:
            try:
                text = input_fn("\nyou> ")
            except (EOFError, KeyboardInterrupt):
                self.line()
                return 0
            if not text.strip():
                continue
            try:
                if self.dispatch(text):
                    continue
                self.ask(text)
            except EOFError:
                return 0
            except OllamaError as exc:
                self.error(str(exc))
            if not self._at_line_start:
                self.line()


class RichChat(ChatUI):
    """Streaming chat with styled tool panels, via rich and prompt_toolkit."""

    def __init__(self, *args: Any, console: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        from rich.console import Console

        self.console = console or Console(highlight=False)
        self._live: Any = None
        self._buffer: list[str] = []
        self._panels: list[Any] = []
        self._thinking: list[str] = []
        # Inference indicator
        self._inference_in_progress: bool = False
        self._spinner_index: int = 0
        self._spinner_frames = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']
        #: Advances the spinner while the model is silent; see _advance_spinner.
        self._spinner_timer: Any = None
        #: _render_live runs from the turn's thread and from the spinner timer.
        self._render_lock = threading.Lock()

    def inference_start(self) -> None:
        """The session began generating: show the indicator."""
        self.set_inference_in_progress(True)
        self._spinner_index = 0
        self._advance_spinner()

    def inference_end(self) -> None:
        """The session stopped generating: take the indicator down."""
        self.set_inference_in_progress(False)
        self._cancel_spinner_timer()

    def set_inference_in_progress(self, flag: bool) -> None:
        """Called by the session when inference starts/stops."""
        self._inference_in_progress = flag
        if not flag:
            # Reset spinner when stopping
            self._spinner_index = 0

    def _cancel_spinner_timer(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.cancel()
            self._spinner_timer = None

    def _schedule_spinner(self) -> None:
        """Queue the next spinner frame. Never renders, so it is safe to call
        from inside a render (see _render_live -> _ensure_live)."""
        if not self._inference_in_progress or self._spinner_timer is not None:
            return
        timer = threading.Timer(1 / 12, self._advance_spinner)
        timer.daemon = True
        self._spinner_timer = timer
        timer.start()

    def _advance_spinner(self) -> None:
        """Redraw one frame, then queue the next while inference is running.

        Rich's Live only redraws when it is handed something new, so a
        generation that has not emitted a delta yet would otherwise sit on a
        frozen frame. The chain stops itself as soon as the flag clears.
        """
        self._spinner_timer = None
        if not self._inference_in_progress:
            return
        self._render_live()
        self._schedule_spinner()

    def _panel(self, title: str, body: str, style: str) -> Any:
        from rich.panel import Panel
        from rich.text import Text

        return Panel(Text(body), title=title, border_style=style, expand=True)

    def _render_live(self) -> None:
        from rich.console import Group
        from rich.markdown import Markdown
        from rich.text import Text

        # Entered from the turn's thread and from the spinner timer, so the
        # live region is only ever rebuilt by one of them at a time.
        with self._render_lock:
            blocks: list[Any] = []
            if self.show_thinking and self._thinking:
                blocks.append(self._panel("reasoning", "".join(self._thinking), "dim"))
            blocks.extend(self._panels)
            text = "".join(self._buffer)
            if text.strip():
                try:
                    blocks.append(Markdown(text))
                except Exception:  # noqa: BLE001 - never let rendering kill a turn
                    blocks.append(Text(text))
            # The inference indicator: a spinner that turns while the model works.
            if self._inference_in_progress:
                spinner = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
                self._spinner_index += 1
                blocks.insert(0, Text(spinner, style="cyan"))
            self._ensure_live()
            self._live.update(Group(*blocks) if blocks else Text("…"))

    def line(self, text: str = "") -> None:
        self.console.print(text)

    def turn_start(self, model: str, protocol: str) -> None:
        # The live region is started lazily by the first thing that has to be
        # shown. Rich's Console.print knows nothing about an active Live, so
        # anything printed between rounds has to happen while none is running.
        self._buffer, self._panels, self._thinking = [], [], []
        self.console.print(f"[dim]{model} · {protocol} tools[/dim]")

    def _ensure_live(self) -> None:
        if self._live is None:
            from rich.live import Live

            self._live = Live(
                self._placeholder(),
                console=self.console,
                refresh_per_second=12,
                transient=True,
            )
            self._live.start()
            # round_end tears the live region down between rounds, so the
            # spinner has to be re-armed whenever the region comes back.
            self._schedule_spinner()

    def _placeholder(self) -> Any:
        from rich.text import Text

        return Text("…")

    def _stop_live(self) -> None:
        # The spinner has to stop before the region it draws into goes away,
        # and no render may be mid-flight while the Live is being stopped.
        self._cancel_spinner_timer()
        with self._render_lock:
            if self._live is not None:
                self._live.stop()
                self._live = None

    def delta(self, text: str) -> None:
        self._buffer.append(text)
        self._render_live()

    def thinking(self, text: str) -> None:
        self._thinking.append(text)
        if self.show_thinking:
            self._render_live()

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self._panels.append(self._panel(f"⚙ {name}", summarize_call(name, arguments), "cyan"))
        self._render_live()

    def tool_result(self, name: str, result: ToolResult) -> None:
        if self.tool_output == "off":
            self._panels.append(self._panel(f"✓ {name}" if result.ok else f"✗ {name}", "", "green" if result.ok else "red"))
            self._render_live()
            return
        body = result.text
        if self.tool_output == "summary":
            body = self._clip_lines(body)
        style = "green" if result.ok else "red"
        title = f"⚙ {name} → {'ok' if result.ok else 'error'}"
        self._panels.append(self._panel(title, body, style))
        self._render_live()

    def notice(self, text: str) -> None:
        self._panels.append(self._panel("note", text, "yellow"))
        self._render_live()

    def error(self, text: str) -> None:
        self._stop_live()
        self.console.print(f"[bold red]error[/bold red] {text}")

    def round_end(self, stats: TurnStats) -> None:
        """Flush one generation: prose, tool panels, its own cost."""
        captured = list(self._buffer)
        self._stop_live()
        for block in self._panels:
            self.console.print(block)
        text = "".join(captured)
        if text.strip():
            from rich.markdown import Markdown

            try:
                self.console.print(Markdown(text))
            except Exception:  # noqa: BLE001
                self.console.print(text)
        self.status(stats)
        self._panels, self._buffer, self._thinking = [], [], []

    def turn_end(self) -> None:
        self._stop_live()

    # -- the loop ---------------------------------------------------------
    def loop(self, input_fn: Callable[[str], str] | None = None) -> int:
        self.greet()
        prompt = input_fn or self._prompt_session()
        self.reader = prompt
        while True:
            try:
                text = prompt("\nyou> ")
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return 0
            if not text or not text.strip():
                continue
            try:
                if self.dispatch(text):
                    continue
                self.ask(text)
            except EOFError:
                return 0
            except OllamaError as exc:
                self.error(str(exc))

    def _prompt_session(self) -> Callable[[str], str]:
        """A prompt_toolkit session with persistent history, if available."""
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory
        except ImportError:  # pragma: no cover - plain input still works
            return input
        from .themes import migrate_state

        history = migrate_state(Path.home() / ".k0b0l-chat-history")
        session = PromptSession(history=FileHistory(str(history)))
        return lambda message: session.prompt(message)


def run_chat(config: Config, *, force_plain: bool = False) -> int:
    """Entry point for ``--chat``. Returns a process exit code."""
    client = _pick_client(config)
    try:
        # Probe only: any successful answer proves the endpoint is up.
        client.version()
    except OllamaError as exc:
        # A dead localhost endpoint usually means the models are served
        # remotely; ask once (interactive TTYs) and retry before failing.
        retry_url = ask_endpoint_on_failure(config, running=True)
        if retry_url != config.api_url:
            config.ollama_url = retry_url
            client = _pick_client(config)
        try:
            client.version()
        except OllamaError:
            print(
                f"{client.backend} is not reachable at {config.api_url}: {exc}",
                file=sys.stderr,
            )
            return 1

    console = None
    # prompt_toolkit and rich both want a terminal. Piped input gets the plain
    # front end, which is the one that still works without one.
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not force_plain and interactive:
        try:
            from rich.console import Console

            console = Console(highlight=False)
        except ImportError:
            console = None

    chat = config.chat
    root = Path(chat.root)
    if not root.is_dir():
        print(f"chat workspace root is not a directory: {root}", file=sys.stderr)
        return 1

    ui: ChatUI
    if console is None:
        ui = PlainChat(
            client, config, tool_output=chat.tool_output, show_thinking=config.show_thinking
        )
        print(f"K0B0L chat · {client.backend} · {len(TOOL_NAMES)} file tools · /help")
        return ui.loop(input)

    ui = RichChat(
        client, config, console=console, tool_output=chat.tool_output,
        show_thinking=config.show_thinking,
    )
    console.print(
        f"[bold]K0B0L chat[/bold] [dim]· {client.backend} · "
        f"{len(TOOL_NAMES)} file tools · /help[/dim]"
    )
    return ui.loop()
