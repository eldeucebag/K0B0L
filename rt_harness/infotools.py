"""Read-only info tools so the chat can answer questions about the harness.

These deliberately answer, not act: anything that changes a run still goes
through the operator's ``/run`` commands, because letting an abliterated model
spawn subprocesses is the hole the no-shell rule exists to close. Everything
here is data the harness already publishes, re-presented to the model.

``read_docs`` is the exception in shape but not in kind: it takes a topic name
and returns that markdown file from :mod:`rt_harness.docs` verbatim, so a
capability question gets the shipped answer instead of a confident guess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import catalog, deployment

REPO = Path(__file__).resolve().parents[1]


def _session_dir() -> Path:
    """Where chat sessions persist between runs."""
    from .themes import migrate_state

    path = migrate_state(Path.home() / ".k0b0l-sessions")
    path.mkdir(parents=True, exist_ok=True)
    return path

HARNESS_HELP = """K0B0L: a red-team harness over local Ollama models.

Concepts
- An *artifact* (BASE_PROMPT_FILE) is a style/format spec a target model adopts.
- The *cycle* (--cycle) has the strategist read the artifact, then the attacker
  mutates it per attack mode, sending each candidate to the target verbatim. A
  `complied` verdict is a bypass; `partial` pulls the escalation mode next.
- Attack modes: """ + ", ".join(name for name, _ in catalog.mode_choices()) + """.
- On a win the harness writes a technical write-up under redteam-logs/writeups
  (how it works, mitigations) and the verbatim winning prompt under
  redteam-logs/winning-prompts.

Operator commands (this chat)
- /run start [target=MODEL] [modes=a,b] [attempts=N] <objective> — spawn a cycle.
- /run status | tail [N] | stop — watch or stop the current run.
- /model NAME — which model answers you.  /target NAME — which one runs target.
- /tools off|summary|full — how much of a tool result you see in the pane.
- /paste /clear /history /exit.

CLI (shell, not the model)
- python3 thinlizzy.py --cycle --objective "..."   # the attack loop
- python3 thinlizzy.py --chat                      # this session
- python3 thinlizzy.py --list-modes / --list-families
"""


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


INFO_TOOL_SCHEMAS: list[dict[str, Any]] = [
    _schema("harness_help", "Explain what the K0B0L harness is and how to use it.", {}, []),
    _schema("list_models", "List the models the configured server reports.", {}, []),
    _schema("list_modes", "List the attack modes the cycle can run.", {}, []),
    _schema("list_deployments", "List deployment families in the CL4R1T4S catalog.", {}, []),
    _schema("list_sessions", "List saved chat sessions (see /session save|load).", {}, []),
    _schema(
        "read_docs",
        "Read one product document about what this harness can do. Use this to "
        "answer capability questions instead of guessing.",
        {
            "name": {
                "type": "string",
                "description": "Topic name from the docs index, e.g. commands, runs, themes.",
            }
        },
        ["name"],
    ),
]

#: Info tools that take arguments, with the keys they accept. Spelled out here
#: rather than introspected, so model input only reaches a handler that opted
#: in -- the rest must stay argument-free.
ARG_TOOLS: dict[str, tuple[str, ...]] = {"read_docs": ("name",)}


class InfoTools:
    """Answers that need config or a network probe; the read-only side."""

    def __init__(self, config: Any) -> None:
        self.config = config

    def harness_help(self) -> "Any":
        from .tools import ToolResult

        return ToolResult(True, HARNESS_HELP)

    def list_sessions(self) -> "Any":
        from .tools import ToolResult

        sessions = _session_dir()
        try:
            names = sorted(p.stem for p in sessions.glob("*.json"))
        except OSError as exc:
            return ToolResult(False, f"cannot list sessions: {exc}")
        return ToolResult(True, "\n".join(names) if names else "(no saved sessions)")

    def list_models(self) -> "Any":
        from .client import OllamaError, make_client
        from .tools import ToolResult

        # Go through the client factory rather than a hand-built URL: llama.cpp
        # has no /api/tags (that is Ollama's route), so a literal
        # "{api_url}/api/tags" 404s against the /v1 endpoint.
        try:
            models = make_client(self.config.api_url).models()
        except OllamaError as exc:
            return ToolResult(False, f"server unreachable at {self.config.api_url}: {exc}")
        return ToolResult(True, "\n".join(models) if models else "(no models)")

    def list_modes(self) -> "Any":
        from .tools import ToolResult

        lines = [f"{name}: {one}" for name, one in catalog.mode_choices()]
        return ToolResult(True, "\n".join(lines))

    def list_deployments(self) -> "Any":
        from .tools import ToolResult

        try:
            return ToolResult(True, "\n".join(deployment.families()))
        except deployment.DeploymentError as exc:
            return ToolResult(False, str(exc))

    def read_docs(self, name: str) -> "Any":
        from .docs import list_docs, read_doc
        from .tools import ToolResult

        text = read_doc(self.config, name)
        if text is None:
            topics = ", ".join(doc.name for doc in list_docs(self.config)) or "(none installed)"
            return ToolResult(False, f"no document named {name!r}; topics: {topics}")
        return ToolResult(True, text)

    def call(self, name: str, arguments: dict[str, Any]) -> "Any":
        from .tools import ToolResult

        handler = getattr(self, name, None)
        if handler is None or name.startswith("_"):
            return ToolResult(False, f"unknown info tool {name!r}")
        if not isinstance(arguments, dict):
            return ToolResult(False, f"{name} takes a JSON object")
        if name in ARG_TOOLS:
            kwargs = {key: str(arguments.get(key, "")).strip() for key in ARG_TOOLS[name]}
            missing = [key for key, value in kwargs.items() if not value]
            if missing:
                return ToolResult(False, f"{name} needs {', '.join(missing)}")
            try:
                return handler(**kwargs)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(False, f"{name} failed: {exc}")
        if arguments:
            return ToolResult(False, f"{name} takes no arguments")
        try:
            return handler()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"{name} failed: {exc}")
