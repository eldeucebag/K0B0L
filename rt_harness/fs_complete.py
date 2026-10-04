"""Completion sources for the chat prompt.

Three sources, chained on one buffer: slash commands, file paths under the
workspace root, and context-sensitive values after specific commands
(``/model <name>``, ``modes=`` on ``/run start``). Everything is local; the
model list is cached briefly so typing is not a network wait.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from prompt_toolkit.completion import Completer, Completion

COMMANDS = [
    "/help", "/tools", "/think", "/read", "/run", "/paste", "/model",
    "/target", "/root", "/clear", "/history", "/exit",
]

RUN_KEYS = ["start", "status", "tail", "stop"]

_cache: dict[str, Any] = {"when": 0.0, "models": []}


def _models(config: Any, ttl: float = 30.0) -> list[str]:
    now = time.monotonic()
    if now - _cache["when"] > ttl:
        from . import catalog

        _cache["when"] = now
        _cache["models"] = catalog.known_models(config)
    return _cache["models"]


def _modes() -> list[str]:
    from . import catalog

    return [name for name, _ in catalog.mode_choices()]


def _families() -> list[str]:
    from . import catalog

    return catalog.deployment_choices() or []


class ChatCompleter(Completer):
    """One completer for the whole prompt line."""

    def __init__(self, config: Any, root: Path) -> None:
        self.config = config
        self.root = Path(root)

    def _paths_for(self, prefix: str) -> Iterable[Completion]:
        head = prefix or ""
        base = self.root
        try:
            directory = (base / head).parent if "/" in head else base
            if not directory.is_dir():
                return
            for entry in sorted(directory.iterdir()):
                if entry.name.startswith(".") and not head.endswith("."):
                    continue
                name = entry.name + ("/" if entry.is_dir() else "")
                if not name.startswith(Path(head).name):
                    continue
                yield Completion(
                    name,
                    start_position=-len(Path(head).name),
                    display=name,
                )
        except OSError:
            return

    def get_completions(self, document: Any, complete_event: Any) -> Iterable[Completion]:
        text = document.text_before_cursor
        stripped = text.lstrip()
        if not stripped:
            return
        if stripped.startswith("/") and " " not in stripped:
            word = stripped
            for cmd in COMMANDS:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
            return
        verb, _, rest = stripped.partition(" ")
        head = rest.split()[-1] if rest else ""
        if verb in ("/read", "/root"):
            yield from self._paths_for(head)
            return
        if verb in ("/model", "/target"):
            for name in _models(self.config):
                if name.startswith(head):
                    yield Completion(name, start_position=-len(head))
            return
        if verb == "/run":
            first, _, tail = rest.partition(" ")
            if first == "" or (first in RUN_KEYS and not tail):
                for key in RUN_KEYS:
                    if key.startswith(first):
                        yield Completion(key, start_position=-len(first))
                return
            if first == "start":
                if head.startswith("modes="):
                    for name in _modes():
                        if name.startswith(head[len("modes="):]):
                            yield Completion(f"modes={name}", start_position=-len(head))
                elif head.startswith("family="):
                    for family in _families():
                        if family.startswith(head[len("family="):]):
                            yield Completion(f"family={family}", start_position=-len(head))
                elif head.startswith("ofile=") or head.startswith("target=") or head.startswith("attempts="):
                    return
                elif head.endswith("="):
                    return
                else:
                    for token in ("target=", "modes=", "family=", "attempts=", "ofile="):
                        if token.startswith(head):
                            yield Completion(token, start_position=-len(head))
            return
        # Prose turns: complete a path after whitespace when it looks like one.
        if "/" in head:
            yield from self._paths_for(head)
