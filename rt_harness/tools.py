"""A confined file workspace for the interactive chat.

The chat model can read, search, edit and *run* files under one root directory
and nothing else. Editing uses exact-string replacement rather than rewriting
whole files, the same contract as the ``patch`` tool this project is developed
with: the match must be unique unless ``replace_all`` is set, so a model cannot
reformat a file it only meant to touch in one place.

Script execution is the one deliberate exception to "no command ever comes out
of the model's mouth", and it is fenced in on four sides:

* the script must already be a file **inside the workspace root** -- the model
  picks a path, never a command line;
* the program is chosen from an allow-list keyed on the file's suffix or its
  shebang, or by an ``interpreter`` argument that must also be on the list, so
  ``bash -c "..."`` is not expressible;
* ``shell=True`` is never used and no argument is ever interpolated into a
  string that a shell then re-parses;
* every run has a wall-clock timeout and its output is capped, so a runaway
  script cannot wedge the session or flood the context window.

That is still enough to do damage inside the workspace -- that is the point of
the feature -- so it is the operator's switch, not the model's: ``CHAT_ALLOW_EXEC=0``
(or ``--no-exec``, or the View menu) turns it off and the tool reports so.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Ceilings that keep one tool call from swallowing the context window. A model
#: that needs more can ask again with a narrower request.
MAX_READ_CHARS = 24_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_HITS = 60
MAX_WRITE_CHARS = 200_000

#: What of a script's combined output reaches the model. The rest is dropped
#: with a note, rather than read into memory in the first place.
MAX_EXEC_OUTPUT = 20_000
#: Wall clock per run, and the ceiling a model may ask for.
DEFAULT_EXEC_TIMEOUT = 60
MAX_EXEC_TIMEOUT = 600

#: Suffix -> program. Everything a script can be run *by* has to be in here (or
#: named by a shebang, or passed as ``interpreter`` and found here), which is
#: what keeps the tool a script runner rather than a shell.
INTERPRETERS: dict[str, tuple[str, ...]] = {
    ".py": (sys.executable or "python3",),
    ".pyw": (sys.executable or "python3",),
    ".sh": ("/bin/bash",),
    ".bash": ("/bin/bash",),
    ".zsh": ("/bin/zsh",),
    ".fish": ("/usr/bin/fish",),
    ".js": ("node",),
    ".mjs": ("node",),
    ".cjs": ("node",),
    ".ts": ("deno", "run"),
    ".rb": ("ruby",),
    ".pl": ("perl",),
    ".pm": ("perl",),
    ".php": ("php",),
    ".lua": ("lua",),
    ".r": ("Rscript",),
    ".tcl": ("tclsh",),
    ".awk": ("awk", "-f"),
    ".sed": ("sed", "-f"),
}

#: Programs ``interpreter`` may name. Deliberately a list of interpreters, not
#: of commands: ``interpreter="bash"`` is fine, ``interpreter="/bin/rm"`` is not.
ALLOWED_PROGRAMS = frozenset(
    name for program in INTERPRETERS.values() for name in program[:1]
) | frozenset(
    {"python", "python3", "python3.10", "python3.11", "python3.12", "nodejs", "deno",
     "bun", "env", "bash", "sh", "dash", "ruby", "perl", "php", "lua", "Rscript"}
)

#: How much of a tool's output is rendered into the chat. The model always
#: receives the full result; this governs what the operator sees.
TOOL_VERBOSITY = ("off", "summary", "full")

#: Directories never worth walking in a chat about a project.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)


class ToolError(RuntimeError):
    """A tool call could not be carried out. Reported to the model, not raised."""


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one tool call, as shown to the model and to the operator."""

    ok: bool
    text: str


def _clip(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return (
        f"{text[:limit]}\n"
        f"… truncated: {what} is {len(text)} characters, shown to {limit}. "
        f"Narrow the request to see the rest."
    )


class Workspace:
    """File operations confined to ``root``."""

    def __init__(
        self, root: str | Path, *, allow_exec: bool = True, ui: Any = None
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        #: Script execution is the operator's switch, not the model's; see the
        #: module docstring for what the fence is made of.
        self.allow_exec = allow_exec
        #: UI reference for tools that need to push screens (e.g. ask_user_choice).
        self._ui = ui

    # -- path handling ----------------------------------------------------
    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        """Resolve ``raw`` against the root, refusing anything outside it."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path must be a non-empty string")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # Resolve first so a symlink pointing outside the root is caught too.
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ToolError(f"{raw!r} is outside the workspace root ({self.root})")
        if must_exist and not resolved.exists():
            raise ToolError(f"{raw!r} does not exist")
        return resolved

    def label(self, path: Path) -> str:
        """Path as the operator should see it: relative to the root when inside."""
        try:
            return str(path.relative_to(self.root)) or "."
        except ValueError:
            return str(path)

    # -- tools ------------------------------------------------------------
    def read_file(self, path: str, start: int = 1, limit: int = 0) -> ToolResult:
        """Read a text file, numbered, from ``start`` for ``limit`` lines."""
        target = self.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{path!r} is a directory; use list_files")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start or 1))
        if start > len(lines):
            raise ToolError(f"{path!r} has {len(lines)} lines; start={start} is past the end")
        end = len(lines) if not limit else min(len(lines), start - 1 + int(limit))
        body = "\n".join(f"{number}|{lines[number - 1]}" for number in range(start, end + 1))
        header = f"{self.label(target)}: lines {start}-{end} of {len(lines)}"
        return ToolResult(True, _clip(f"{header}\n{body}", MAX_READ_CHARS, path))

    def list_files(self, path: str = ".", pattern: str = "*") -> ToolResult:
        """List entries under a directory, directories marked with a slash."""
        target = self.resolve(path or ".", must_exist=True)
        if target.is_file():
            return ToolResult(True, f"{self.label(target)} (file)")
        entries: list[str] = []
        for child in sorted(target.glob(pattern or "*")):
            if child.name in SKIP_DIRS:
                continue
            entries.append(f"{self.label(child)}{'/' if child.is_dir() else ''}")
            if len(entries) >= MAX_LIST_ENTRIES:
                entries.append(f"… truncated at {MAX_LIST_ENTRIES} entries")
                break
        if not entries:
            return ToolResult(True, f"{self.label(target)}: no entries match {pattern!r}")
        return ToolResult(True, "\n".join(entries))

    def search_files(
        self, pattern: str, path: str = ".", glob: str = "*", ignore_case: bool = False
    ) -> ToolResult:
        """Regex search file contents under a directory."""
        target = self.resolve(path or ".", must_exist=True)
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(f"invalid regex {pattern!r}: {exc}") from exc
        files = [target] if target.is_file() else sorted(target.rglob(glob or "*"))
        hits: list[str] = []
        scanned = 0
        for candidate in files:
            if not candidate.is_file() or any(part in SKIP_DIRS for part in candidate.parts):
                continue
            scanned += 1
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{self.label(candidate)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        hits.append(f"… truncated at {MAX_SEARCH_HITS} matches")
                        return ToolResult(True, "\n".join(hits))
        if not hits:
            return ToolResult(True, f"no matches for {pattern!r} in {scanned} files")
        return ToolResult(True, "\n".join(hits))

    def write_file(self, path: str, content: str) -> ToolResult:
        """Create or overwrite a file with ``content``."""
        if len(content) > MAX_WRITE_CHARS:
            raise ToolError(f"content is {len(content)} chars; the limit is {MAX_WRITE_CHARS}")
        target = self.resolve(path)
        if target.is_dir():
            raise ToolError(f"{path!r} is a directory")
        existed = target.exists()
        before = target.stat().st_size if existed else 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        verb = "overwrote" if existed else "created"
        detail = f" ({before} bytes before)" if existed else ""
        return ToolResult(
            True,
            f"{verb} {self.label(target)}{detail}: {len(content)} chars, "
            f"{len(content.splitlines())} lines",
        )

    def edit_file(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> ToolResult:
        """Replace an exact string in a file, requiring a unique match."""
        target = self.resolve(path, must_exist=True)
        content = target.read_text(encoding="utf-8")
        if not old_string:
            raise ToolError("old_string must not be empty")
        if old_string == new_string:
            # A no-op edit reported as success reads as "the fix landed". It is
            # almost always the model having lost track of the file.
            raise ToolError("old_string and new_string are identical; nothing to change")
        found = content.count(old_string)
        if found == 0:
            raise ToolError(f"old_string does not appear in {path!r}; read the file first")
        if found > 1 and not replace_all:
            raise ToolError(
                f"old_string appears {found} times in {path!r}; include more surrounding "
                f"context to make it unique, or set replace_all"
            )
        updated = content.replace(old_string, new_string) if replace_all else content.replace(
            old_string, new_string, 1
        )
        target.write_text(updated, encoding="utf-8")
        first_line = content[: content.index(old_string)].count("\n") + 1
        return ToolResult(
            True,
            f"edited {self.label(target)}: {found if replace_all else 1} replacement(s) "
            f"starting at line {first_line}, {len(old_string)} -> {len(new_string)} chars",
        )

    # -- execution ---------------------------------------------------------
    def _shebang(self, target: Path) -> list[str]:
        """The ``#!`` line of ``target`` as argv, or ``[]`` when there is none."""
        try:
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline(300).strip()
        except OSError:
            return []
        if not first.startswith("#!"):
            return []
        try:
            argv = shlex.split(first[2:].strip())
        except ValueError:
            return []
        if not argv:
            return []
        # ``#!/usr/bin/env python3`` means python3, not env -- and env would
        # otherwise be the program the allow-list has to vouch for.
        if Path(argv[0]).name == "env":
            argv = [part for part in argv[1:] if not part.startswith("-")]
        return argv

    def _program(self, target: Path, interpreter: str) -> list[str]:
        """argv prefix that runs ``target``, or a ToolError explaining the refusal."""
        if interpreter:
            argv = shlex.split(interpreter)
            if not argv:
                raise ToolError("interpreter must not be empty")
            if argv[0] not in ALLOWED_PROGRAMS and Path(argv[0]).name not in ALLOWED_PROGRAMS:
                raise ToolError(
                    f"interpreter {argv[0]!r} is not an interpreter this tool will run; "
                    f"one of these is: {', '.join(sorted(ALLOWED_PROGRAMS))}"
                )
            return argv
        known = INTERPRETERS.get(target.suffix.lower())
        if known:
            return list(known)
        shebang = self._shebang(target)
        if shebang:
            if shebang[0] in ALLOWED_PROGRAMS or Path(shebang[0]).name in ALLOWED_PROGRAMS:
                return shebang
            raise ToolError(
                f"{target.name}'s shebang names {shebang[0]!r}, which is not an "
                f"interpreter this tool will run; pass interpreter=… if that is wrong"
            )
        suffix = target.suffix or "no suffix"
        raise ToolError(
            f"no interpreter is known for {suffix!r}; pass interpreter=… "
            f"(known suffixes: {', '.join(sorted(INTERPRETERS))})"
        )

    def run_script(
        self,
        path: str,
        args: str = "",
        interpreter: str = "",
        cwd: str = "",
        stdin: str = "",
        timeout: int = 0,
    ) -> ToolResult:
        """Run a script that already exists inside the workspace.

        ``args`` is split with :mod:`shlex` and handed to the program as argv --
        no shell ever re-parses it. Output is capped and the process group is
        killed on timeout, so a runaway script cannot hold the session.
        """
        if not self.allow_exec:
            raise ToolError(
                "running scripts is off in this session; the operator enables it "
                "with CHAT_ALLOW_EXEC=1 (or --exec, or the View menu)"
            )
        target = self.resolve(path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{path!r} is a directory; run_script runs a file")
        workdir = self.resolve(cwd) if cwd else target.parent
        if not workdir.is_dir():
            raise ToolError(f"cwd {self.label(workdir)!r} is not a directory")
        argv = self._program(target, interpreter) + [str(target)] + shlex.split(args or "")
        try:
            seconds = int(timeout) if timeout else DEFAULT_EXEC_TIMEOUT
        except (TypeError, ValueError):
            raise ToolError(f"timeout must be a number of seconds, got {timeout!r}") from None
        seconds = max(1, min(MAX_EXEC_TIMEOUT, seconds))

        started = time.monotonic()
        timed_out = False
        try:
            # A temporary file rather than a pipe: a script that prints for ten
            # minutes then hits the timeout is otherwise buffered in RAM first.
            with tempfile.TemporaryFile() as sink:
                process = subprocess.Popen(  # noqa: S603 - argv, never a shell string
                    argv,
                    cwd=str(workdir),
                    stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    # Its own process group, so killing the timeout reaps any
                    # children the script spawned rather than orphaning them.
                    start_new_session=True,
                )
                try:
                    process.communicate((stdin or "").encode("utf-8"), timeout=seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        process.kill()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                elapsed = time.monotonic() - started
                sink.seek(0)
                raw = sink.read(MAX_EXEC_OUTPUT + 1)
        except FileNotFoundError as exc:
            raise ToolError(
                f"{argv[0]!r} is not installed here, so {target.name} cannot run ({exc})"
            ) from exc
        except PermissionError as exc:
            raise ToolError(f"{target.name} is not executable and could not be run: {exc}") from exc

        text = raw[:MAX_EXEC_OUTPUT].decode("utf-8", "replace")
        shown = "".join(shlex.quote(part) for part in argv[:1]) + " " + self.label(target)
        header = f"{shown} in {self.label(workdir)} -> exit {process.returncode} in {elapsed:.2f}s"
        if timed_out:
            header = (
                f"{shown} in {self.label(workdir)} -> killed after {seconds}s "
                f"(timeout); output so far follows"
            )
        body = text.rstrip("\n")
        if len(raw) > MAX_EXEC_OUTPUT:
            body += (
                f"\n… output truncated at {MAX_EXEC_OUTPUT} characters; "
                f"the script printed more. Write it to a file and read that instead."
            )
        return ToolResult(
            ok=(not timed_out and process.returncode == 0),
            text=f"{header}\n{body}" if body else f"{header}\n(no output)",
        )

    # -- dispatch ---------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run one tool by name. Failures come back as ``ok=False``."""
        handlers = {
            "read_file": self.read_file,
            "list_files": self.list_files,
            "search_files": self.search_files,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_script": self.run_script,
            "ask_user_choice": self.ask_user_choice,
            "remember": self.remember,
            "recall": self.recall,
            "connect_memories": self.connect_memories,
            "expand_memory": self.expand_memory,
            "compress_context": self.compress_context,
            "generate_image": self.generate_image,
            "load_skill": self.load_skill,
        }
        handler = handlers.get(name)
        if handler is None:
            return ToolResult(False, f"unknown tool {name!r}; available: {', '.join(handlers)}")
        if not isinstance(arguments, dict):
            return ToolResult(False, f"arguments must be an object, got {type(arguments).__name__}")
        try:
            return handler(**arguments)
        except ToolError as exc:
            return ToolResult(False, str(exc))
        except TypeError as exc:
            return ToolResult(False, f"bad arguments for {name}: {exc}")
        except OSError as exc:
            return ToolResult(False, f"{name} failed: {exc}")

    def ask_user_choice(
        self,
        prompt: str,
        options: list[str],
        *,
        allow_multiple: bool = False,
        default_selected: list[str] | None = None,
    ) -> ToolResult:
        """Ask the operator to pick from a list of options.

        Blocks the turn until the operator answers. The picker is a modal the
        Textual front end owns, so this needs the real UI: with no app bound
        (plain front end, scripted session) it returns an error rather than
        waiting on a screen that can never appear. The operator's answer comes
        back as a JSON list of the chosen option strings.
        """
        # Imported here, not at module level: textual_chat imports this module
        # (ToolResult), so a module-level import is circular and silently
        # degrades to None when tools.py loads first -- which is the order the
        # chat itself loads in.
        try:
            from .textual_chat import ListPick, MultiSelectPicker
        except ImportError:
            return ToolResult(
                False,
                "selection UI not available (the Textual front end is not installed)",
            )
        # The workspace holds whatever object wired itself in as ``ui`` -- in the
        # chat that is the ChatSession, which reaches the app through its hooks;
        # a test may hand over the front end itself. Either shape is fine.
        app = getattr(self._ui, "app", None)
        if app is None:
            app = getattr(getattr(self._ui, "hooks", None), "app", None)
        if app is None:
            return ToolResult(
                False,
                "no interactive UI is bound to this session; "
                "user choices need the Textual chat",
            )

        from concurrent.futures import Future, TimeoutError as FutureTimeoutError

        future: Future = Future()

        def _done(result: Any) -> None:
            # Normalise both shapes (a list, or one picked string) and swallow a
            # late answer: a timeout may already have failed the future.
            chosen = result if isinstance(result, list) else ([result] if result else [])
            try:
                future.set_result(chosen)
            except Exception:  # noqa: BLE001 - InvalidStateError after a timeout
                pass

        picker: Any
        if allow_multiple:
            picker = MultiSelectPicker(
                options, title=prompt, default_selected=default_selected or []
            )
        else:
            picker = ListPick(options, title=prompt)

        def _push() -> None:
            app.push_screen(picker, _done)

        try:
            if threading.current_thread() is threading.main_thread():
                _push()
            else:
                # The turn runs on a daemon thread; Textual owns its widgets.
                app.call_from_thread(_push)
        except Exception as exc:  # noqa: BLE001 - a broken UI must not kill the turn
            return ToolResult(False, f"could not open the selection UI: {exc}")

        try:
            chosen = future.result(timeout=300)  # 5-minute ceiling
        except FutureTimeoutError:
            # The operator never answered: take the picker down rather than
            # leave it blocking whatever comes next.
            try:
                app.call_from_thread(picker.dismiss, None)
            except Exception:  # noqa: BLE001
                pass
            return ToolResult(
                False,
                "no selection was made within 300s; ask again, or proceed "
                "with your best choice and say which you took",
            )

        if not chosen:
            return ToolResult(False, "the operator cancelled the selection")
        return ToolResult(True, json.dumps(chosen))

    def remember(
        self,
        body: str,
        *,
        domain: str = "",
        importance: float = 0.5,
    ) -> ToolResult:
        """Write a durable note to the long-term research memory.

        The model-facing half of the memory feature: a research session that
        learns a fact writes it once, and every later session -- any model,
        any workspace -- recalls it. Provenance (which model wrote it, when)
        is stamped by the session, so the model cannot misattribute.
        """
        body = (body or "").strip()
        if not body:
            return ToolResult(False, "a memory needs a body: one clear sentence")
        # The UI object is the ChatSession in the chat; it owns the store and
        # the model name. Without one (a bare workspace in a test) refuse
        # cleanly rather than writing unattributed rows.
        session = getattr(self._ui, "memory", None) is not None
        if not session:
            return ToolResult(False, "no session is bound to this workspace")
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.5
        # ``self._ui`` is the ChatSession (see chat.py): remember() carries
        # the model name and turn provenance.
        wrote = self._ui.remember(body, domain=domain, importance=importance,
                                  source="model")
        if not wrote:
            return ToolResult(False, "the long-term memory store is unavailable")
        return ToolResult(
            True,
            f"remembered ({domain or 'no domain'}): {body[:120]}",
        )

    def recall(
        self,
        query: str,
        *,
        domain: str = "",
        limit: int = 0,
    ) -> ToolResult:
        """Search the model's own long-term memory.

        The other half of self-memorization: the model asks for its own
        earlier notes whenever it judges them relevant -- before answering a
        research question, when a task resumes, when a fact feels half-known.
        Rows come back with the model and date that wrote them, because a
        memory the model cannot attribute is a memory it cannot trust.
        """
        if getattr(self._ui, "memory", None) is None:
            return ToolResult(False, "no session is bound to this workspace")
        try:
            limit = max(0, min(int(limit), 32))
        except (TypeError, ValueError):
            limit = 0
        rows = self._ui.recall(query or "", domain=domain, limit=limit)
        if not rows:
            return ToolResult(
                True,
                "no earlier memories matched; the store only holds what a "
                "session chose to remember",
            )
        out = []
        for row in rows:
            import time as _time

            stamp = _time.strftime("%Y-%m-%d", _time.localtime(row.created))
            who = f" by {row.model}" if row.model else ""
            scope = f" [{row.domain}]" if row.domain else ""
            # The id is the handle the graph tools take: connect_memories and
            # expand_memory work on what recall returned, so every row
            # carries its id.
            out.append(f"- #{row.id} {row.body}{scope} ({stamp}{who})")
        return ToolResult(True, "\n".join(out))

    def connect_memories(self, from_id: int, to_id: int, *,
                         why: str = "") -> ToolResult:
        """Record the model's own judgement that two facts belong together.

        An explicit edge outranks every derived one in hop-recall and
        survives later rewording of both bodies. ``why`` is optional
        provenance, kept in the edge's kind so the claim itself is
        inspectable.
        """
        store = getattr(self._ui, "memory", None)
        if store is None:
            return ToolResult(False, "no session is bound to this workspace")
        try:
            from_id, to_id = int(from_id), int(to_id)
        except (TypeError, ValueError):
            return ToolResult(False, "from_id and to_id must be memory ids "
                              "(the #N in recall results)")
        if from_id == to_id:
            return ToolResult(False, "a memory cannot be linked to itself")
        bodies = store._db.execute(
            "SELECT id FROM memories WHERE id IN (?, ?)", (from_id, to_id)
        ).fetchall()
        if len(bodies) != 2:
            found = {int(b["id"]) for b in bodies}
            missing = [i for i in (from_id, to_id) if i not in found]
            return ToolResult(
                False,
                f"no memory with id {missing[0]} -- ids come from recall "
                "results, and forget() may have removed it",
            )
        kind = (why or "related").strip()[:40] or "related"
        store.connect(from_id, to_id, kind=kind, weight=0.9, origin="explicit")
        return ToolResult(True, f"linked #{from_id} <-> #{to_id} ({kind})")

    def expand_memory(self, memory_id: int, *, hops: int = 1,
                      limit: int = 8) -> ToolResult:
        """Hop-recall: the facts connected to one memory, nearest first.

        The graph walk that answers "what else do I know around this?":
        recall finds the entry point, this expands the thought. Neighbours
        rank by edge weight (explicit links first), then importance, then
        recency.
        """
        store = getattr(self._ui, "memory", None)
        if store is None:
            return ToolResult(False, "no session is bound to this workspace")
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return ToolResult(False, "memory_id must be a memory id (the #N "
                              "in recall results)")
        row = store._db.execute(
            "SELECT body FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return ToolResult(False, f"no memory with id {memory_id}")
        try:
            hops = max(1, min(int(hops), 2))
        except (TypeError, ValueError):
            hops = 1
        try:
            limit = max(1, min(int(limit), 32))
        except (TypeError, ValueError):
            limit = 8
        found = store.neighbors(memory_id, hops=hops, limit=limit)
        if not found:
            return ToolResult(True, "this memory has no connected facts yet "
                              "(connect_memories adds them)")
        import time as _time

        out = []
        for mem in found:
            stamp = _time.strftime("%Y-%m-%d", _time.localtime(mem.created))
            who = f" by {mem.model}" if mem.model else ""
            scope = f" [{mem.domain}]" if mem.domain else ""
            out.append(f"- #{mem.id} {mem.body}{scope} ({stamp}{who})")
        return ToolResult(True, "\n".join(out))

    def compress_context(self) -> ToolResult:
        """Compress the whole conversation into a summary (see /compact).

        The model-facing half of context compression: a session whose
        history is running long can fold everything so far into one dense
        summary, write the durable facts to the memory store, and recall
        earlier memories into the compacted context. The next turn reads
        the summary as its entire prior conversation.

        Call it when the history is heavy with tool traffic or many
        exchanges, or when told to. The result confirms what happened; the
        compressed context itself arrives as the next message.
        """
        session = getattr(self._ui, "compress", None)
        if not callable(session):
            return ToolResult(False, "no session is bound to this workspace")
        source_count = len([
            m for m in self._ui.messages
            if getattr(m, "get", lambda *_: None)("role") != "system"
        ])
        if source_count < 2:
            return ToolResult(True, "nothing to compress yet; the "
                              "conversation is too short to need it")
        report = self._ui.compress(via_tool=True)
        if not report or report.startswith("nothing"):
            return ToolResult(True, report or "nothing to compress yet")
        return ToolResult(True, report + ". The compressed context "
                          "arrives as the next message; continue from it "
                          "without calling this again.")

    def generate_image(
        self,
        prompt: str,
        model: str = "",
        size: str = "",
        steps: int = 0,
        seed: int = 0,
        negative_prompt: str = "",
    ) -> ToolResult:
        """Generate an image locally and save it into the workspace.

        Runs the harness's image service (tools/imagegen.py) against the
        three local uncensored models: ``pony`` (Pony Diffusion V6 XL --
        fast, tag-style prompting), ``qwen`` (Qwen-Image-2.1-UC with the
        Heretic encoder -- best prompt adherence), ``chroma`` (uncensored
        Flux-class). The image lands under images/ in the workspace root
        and is shown to the operator in the chat.

        Image generation and the text models share the one GPU: the
        service refuses if the model server is holding the card. Expect
        roughly a minute for pony and a few minutes for qwen/chroma at
        768x768. The path is returned; describe the image to the operator
        from the prompt you chose.
        """
        runner = Path(__file__).resolve().parent.parent / "tools" / "imagegen.py"
        if not runner.is_file():
            return ToolResult(False, "the image service (tools/imagegen.py) "
                              "is not installed in this harness checkout")
        allowed = ("pony", "qwen", "chroma")
        model = (model or "").strip().lower() or "pony"
        if model not in allowed:
            return ToolResult(False, f"model must be one of {', '.join(allowed)}")
        out_dir = self.root / "images"
        argv = [sys.executable, str(runner), "gen", model, prompt or ""]
        if size:
            argv += ["--size", str(size)]
        if steps:
            argv += ["--steps", str(steps)]
        if seed:
            argv += ["--seed", str(seed)]
        if negative_prompt:
            argv += ["--neg", negative_prompt]
        argv += ["--out", str(out_dir / f"{model}-{int(time.time())}.png")]
        expected_out = argv[argv.index("--out") + 1]
        try:
            # Ten minutes: qwen/chroma run minutes per image on this GPU.
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                argv, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return ToolResult(False, "image generation timed out after ten "
                              "minutes; try fewer steps or the pony model")
        except OSError as exc:
            return ToolResult(False, f"could not run the image service: {exc}")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            # The runner explains GPU-busy and missing-model conditions
            # itself; pass the last line through, it is the human part.
            last = detail.splitlines()[-1] if detail else "unknown error"
            return ToolResult(False, f"image generation failed: {last}")
        # The --out we passed is authoritative; the runner appends a
        # duration to its "saved <path>" line, so parsing it back is
        # fragile -- the path is known, and the file proves it ran.
        saved = expected_out
        if not Path(saved).is_file():
            return ToolResult(False, "the image service finished without "
                              f"writing {saved}")
        try:
            relative = Path(saved).resolve().relative_to(self.root)
        except ValueError:
            return ToolResult(False, f"the image service saved outside the "
                              f"workspace: {saved}")
        # Tell the UI to show it: hooks.image() when a front end is bound.
        show = getattr(self._ui, "show_image", None)
        if callable(show):
            try:
                show(str(relative))
            except Exception:  # noqa: BLE001 - a UI failure must not lose the file
                pass
        return ToolResult(True, f"image saved to images/{relative} "
                          f"(shown to the operator); prompt was: "
                          f"{(prompt or '')[:80]}")

    def load_skill(self, name: str) -> ToolResult:
        """Load one skill's full body on demand (progressive disclosure).

        The system message carries only each skill's name and description;
        this is how the model opens one before working in its area. The
        body returns as the tool result, which lands in the conversation
        where the model (and the operator) can both see it.

        Discovery happens here, from the workspace root, rather than through
        the front end's skill list: the ChatSession is what the workspace
        holds as ``ui``, and it has no reason to carry a skills index of its
        own.
        """
        from .skills import list_skills, read_skill, resolve_chain

        all_skills = list_skills(self.root)
        if not all_skills:
            return ToolResult(False, "no skills are installed in this harness")
        chain = resolve_chain(all_skills, name or "")
        if not chain:
            known = ", ".join(s.name for s in all_skills[:12])
            return ToolResult(False, f"no skill named {name!r}; installed: {known}")
        parts: list[str] = []
        for index, skill in enumerate(chain):
            header = (skill.body if index == len(chain) - 1 else
                      f"[prerequisite: {skill.name}]\n{skill.body}")
            parts.append(header)
        skill = chain[-1]
        if skill.resources:
            parts.append(
                "\nResources beside this skill (read_file them as needed): "
                + ", ".join(p.name for p in skill.resources)
            )
        unmet = [r for r in skill.requires
                 if not any((s.name or "").lower() == r.lower() for s in chain)]
        if unmet:
            parts.append(
                "\nUnresolvable requires (not installed): " + ", ".join(unmet)
            )
        return ToolResult(True, "\n\n".join(parts))


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema(
        "read_file",
        "Read a UTF-8 text file. Returns line-numbered content.",
        {
            "path": {"type": "string", "description": "File path, relative to the workspace root"},
            "start": {"type": "integer", "description": "First line to read (1-based)"},
            "limit": {"type": "integer", "description": "How many lines to read; 0 for all"},
        },
        ["path"],
    ),
    _schema(
        "list_files",
        "List files and directories under a path in the workspace.",
        {
            "path": {"type": "string", "description": "Directory to list; defaults to the root"},
            "pattern": {"type": "string", "description": "Glob filter, e.g. '*.py'"},
        },
        [],
    ),
    _schema(
        "search_files",
        "Search file contents with a regular expression.",
        {
            "pattern": {"type": "string", "description": "Regular expression to search for"},
            "path": {"type": "string", "description": "Directory or file to search"},
            "glob": {"type": "string", "description": "Only search files matching this glob"},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
        },
        ["pattern"],
    ),
    _schema(
        "write_file",
        "Create or overwrite a file with new content.",
        {
            "path": {"type": "string", "description": "File path, relative to the workspace root"},
            "content": {"type": "string", "description": "Full new contents of the file"},
        },
        ["path", "content"],
    ),
    _schema(
        "edit_file",
        "Replace an exact string in a file. old_string must be unique unless replace_all is true.",
        {
            "path": {"type": "string", "description": "File path, relative to the workspace root"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        ["path", "old_string", "new_string"],
    ),
    _schema(
        "run_script",
        (
            "Run a script that already exists in the workspace, and return its combined "
            "output. The program is chosen from the file's suffix (or shebang, or the "
            "interpreter argument). Not a shell: there is no way to pass a command line."
        ),
        {
            "path": {"type": "string", "description": "Script path, relative to the workspace root"},
            "args": {"type": "string", "description": "Arguments for the script, as one shell-quoted string"},
            "interpreter": {"type": "string", "description": "Override the program, e.g. 'python3' or 'bash'"},
            "cwd": {"type": "string", "description": "Working directory; defaults to the script's own directory"},
            "stdin": {"type": "string", "description": "Text to feed the script on standard input"},
            "timeout": {"type": "integer", "description": f"Seconds before the script is killed (max {MAX_EXEC_TIMEOUT})"},
        },
        ["path"],
    ),
    _schema(
        "ask_user_choice",
        "Ask the operator to pick from a list of options. Returns the chosen option(s) as a JSON list.",
        {
            "prompt": {
                "type": "string",
                "description": "The question or instruction to show the operator",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The list of options to choose from",
            },
            "allow_multiple": {
                "type": "boolean",
                "description": "Whether the operator may select multiple options",
                "default": False,
            },
            "default_selected": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pre-selected options (for allow_multiple=true)",
            },
        },
        ["prompt", "options"],
    ),
    _schema(
        "remember",
        "Save a durable fact to long-term memory, shared across models and "
        "sessions. One clear sentence; it will be recalled verbatim later.",
        {
            "body": {
                "type": "string",
                "description": "The fact, as one self-contained sentence",
            },
            "domain": {
                "type": "string",
                "description": "Optional scope tag, e.g. 'serving' or 'research'; "
                "recalls can be narrowed to it",
                "default": "",
            },
            "importance": {
                "type": "number",
                "description": "0.0-1.0; higher facts are recalled first",
                "default": 0.5,
            },
        },
        ["body"],
    ),
    _schema(
        "recall",
        "Search your own long-term memory: earlier facts you (or another "
        "model in this harness) chose to remember. Call it when starting a "
        "research task, resuming one, or when a fact feels half-known.",
        {
            "query": {
                "type": "string",
                "description": "Words to look for; natural language works. "
                "Empty returns your most important recent notes.",
            },
            "domain": {
                "type": "string",
                "description": "Narrow to one scope tag, or empty for all",
                "default": "",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return (0 = default 12)",
                "default": 0,
            },
        },
        [],
    ),
    _schema(
        "connect_memories",
        "Link two memories as belonging in one thought. An explicit link "
        "outranks derived ones in expand_memory and survives rewording. "
        "Use it when you notice two recalled facts are really one subject.",
        {
            "from_id": {
                "type": "integer",
                "description": "Memory id (the #N in recall results)",
            },
            "to_id": {
                "type": "integer",
                "description": "The other memory id",
            },
            "why": {
                "type": "string",
                "description": "Optional short reason; kept on the edge",
                "default": "",
            },
        },
        ["from_id", "to_id"],
    ),
    _schema(
        "expand_memory",
        "Walk the memory graph from one memory: the facts connected to it, "
        "nearest first. Call after recall when a fact looks like the entry "
        "point of a larger subject.",
        {
            "memory_id": {
                "type": "integer",
                "description": "Memory id (the #N in recall results)",
            },
            "hops": {
                "type": "integer",
                "description": "1 = direct neighbours (default), 2 = one further chain",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows (default 8)",
                "default": 8,
            },
        },
        ["memory_id"],
    ),
    _schema(
        "compress_context",
        "Fold this conversation's history into one dense summary and "
        "continue from it: durable facts go to the long-term memory, and "
        "earlier memories are recalled into the compacted context. Call "
        "when the history is long or heavy with tool traffic, or when "
        "told to compact. The result confirms; the compressed context "
        "arrives as the next message.",
        {},
        [],
    ),
    _schema(
        "generate_image",
        "Generate an image locally from a text prompt and show it to the "
        "operator. Models: pony (fast, tag-style prompting), qwen (best "
        "prompt adherence, slower), chroma (Flux-class). The image is "
        "saved under images/ and rendered in the chat. The image service "
        "and the text models share the one GPU; if it reports the GPU is "
        "busy, tell the operator to stop the model server first.",
        {
            "prompt": {
                "type": "string",
                "description": "What to draw; for pony, comma-separated tags work best",
            },
            "model": {
                "type": "string",
                "description": "pony | qwen | chroma (default pony)",
                "default": "pony",
                "enum": ["pony", "qwen", "chroma"],
            },
            "size": {
                "type": "string",
                "description": "WxH, e.g. 768x768 (default)",
                "default": "768x768",
            },
            "steps": {
                "type": "integer",
                "description": "Sampling steps (default: 20-28 by model)",
                "default": 0,
            },
            "seed": {
                "type": "integer",
                "description": "Reproducibility seed (0 = time-based)",
                "default": 0,
            },
            "negative_prompt": {
                "type": "string",
                "description": "What to avoid (pony especially)",
                "default": "",
            },
        },
        ["prompt"],
    ),
    _schema(
        "load_skill",
        "Open one installed skill's full procedure. The system message lists "
        "every skill by name; call this before working in a skill's area.",
        {
            "name": {
                "type": "string",
                "description": "Skill name from the list (prefixes work)",
            },
        },
        ["name"],
    ),
]

TOOL_NAMES = tuple(schema["function"]["name"] for schema in TOOL_SCHEMAS)

#: What a model is told when it has no native tool template. Ollama passes tool
#: schemas to models through the chat template, which some abliterated GGUFs
#: ship without -- those models advertise a ``tools`` capability they cannot
#: honour, so the protocol is spelled out in the system message instead.
TEXT_TOOL_PROTOCOL = """You can use tools by replying with a fenced code block:

```tool
{"name": "read_file", "arguments": {"path": "example.py"}}
```

Rules:
- Emit at most one tool block per reply, and no prose alongside it.
- The block is removed from what the operator sees; the tool's result comes
  back to you as the next message.
- When you are done, reply normally with no tool block.

Available tools:
<<TOOLS>>
"""

#: Braces in this template are literal JSON, so ``str.format`` is unusable --
#: substitution is a plain replace.
TOOLS_PLACEHOLDER = "<<TOOLS>>"

TEXT_CALL_RE = re.compile(r"```(?:tool|tool_call|json)?\s*(\{.*?\})\s*```", re.DOTALL)

#: Some models spell tool calls the way their own training did rather than the
#: way the protocol prompt asks. Both spellings name a real tool with real
#: arguments or they are ignored, so accepting the tag form costs nothing.
TAG_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def text_protocol_prompt(include_info: bool = False) -> str:
    """The system-message addendum describing the text tool protocol."""
    lines = []
    schemas = TOOL_SCHEMAS
    if include_info:
        from .infotools import INFO_TOOL_SCHEMAS

        schemas = schemas + INFO_TOOL_SCHEMAS
    for schema in schemas:
        function = schema["function"]
        args = ", ".join(function["parameters"]["properties"]) or "none"
        lines.append(f"- {function['name']}({args}): {function['description']}")
    return TEXT_TOOL_PROTOCOL.replace(TOOLS_PLACEHOLDER, "\n".join(lines))


def parse_text_calls(text: str, extra_names: tuple[str, ...] = ()) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """Split model output into visible prose and text-protocol tool calls.

    Returns ``(prose, calls)``. Only well-formed calls naming a real tool are
    accepted; a near-miss block is left in the prose rather than silently
    dropped, so the operator can see what the model actually wrote.
    """
    known = TOOL_NAMES + tuple(extra_names)
    calls: list[tuple[str, dict[str, Any]]] = []
    spans: list[tuple[int, int]] = []
    matches = [
        match for pattern in (TEXT_CALL_RE, TAG_CALL_RE) for match in pattern.finditer(text)
    ]
    matches.sort(key=lambda found: found.start())
    for match in matches:
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name") or payload.get("tool")
        arguments = payload.get("arguments", payload.get("args", {}))
        if not isinstance(name, str):
            continue
        if name not in known:
            continue
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            continue
        calls.append((name, arguments))
        spans.append((match.start(), match.end()))

    prose = text
    for start, end in reversed(spans):
        prose = prose[:start] + prose[end:]
    return prose.strip(), calls


def normalize_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments as Ollama returns them: an object, or a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def summarize_call(name: str, arguments: dict[str, Any]) -> str:
    """One-line rendering of a tool call, for the transcript header."""
    if not arguments:
        return name
    parts = []
    for key, value in arguments.items():
        text = str(value)
        if len(text) > 60:
            text = f"{text[:57]}…"
        text = text.replace("\n", "\\n")
        parts.append(f"{key}={text}")
    return f"{name}({', '.join(parts)})"
