"""The soul file: standing identity, loaded automatically unless it is off.

CORE.md is not the operator's system prompt (``CHAT_SYSTEM_FILE``) and not a
skill (opt-in, per project). It is the assistant's own standing text -- voice,
defaults, how it works with this operator -- and it loads by itself so a session
never starts without it.

Lookup order, first hit wins:

1. ``CHAT_SOUL_FILE`` / ``--soul PATH`` when set,
2. ``CORE.md`` in the session's working root (a per-project core),
3. ``CORE.md`` next to the package (the shipped default).

``SOUL.md`` is still accepted at every step, so a session that already has one
keeps working; ``CORE.md`` wins when both sit in the same directory.

``load_soul`` never raises: a missing or unreadable file is reported as
unloaded and the session runs without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ChatConfig

#: Filenames accepted inside a directory, in order. CORE.md is the name;
#: SOUL.md is still read, so a session that already has one keeps working.
SOUL_NAMES = ("CORE.md", "core.md", "SOUL.md", "soul.md")

#: The core file that ships with the harness.
DEFAULT_SOUL_PATH = Path(__file__).resolve().parents[1] / SOUL_NAMES[0]

#: How the block introduces itself, so the model knows what it is reading.
SOUL_INTRO = (
    "Your soul file follows: standing identity, not a task. Let it shape your "
    "voice, your defaults, and how you work with this operator. The operator's "
    "request in this session outranks it wherever the two disagree, and unless "
    "they ask, do not quote it back at them."
)


@dataclass(frozen=True)
class Soul:
    """A loaded soul, or the reason there is none."""

    path: Path | None
    text: str
    source: str  # "file" | "disabled" | "missing"
    #: What was tried, in order. Kept so "/soul" can show its work.
    looked: tuple[Path, ...] = ()

    @property
    def loaded(self) -> bool:
        return bool(self.text)

    def describe(self) -> str:
        if self.source == "disabled":
            return "soul: disabled (CHAT_SOUL=0 / --no-soul)"
        if not self.loaded:
            where = ", ".join(str(path) for path in self.looked) or ", ".join(SOUL_NAMES)
            return f"soul: no file found (looked for {where})"
        return f"soul: {self.path} ({len(self.text)} chars)"


def _settings(config: Any) -> tuple[bool, str]:
    """Read the soul settings off a :class:`ChatConfig` or a whole ``Config``."""
    if not hasattr(config, "soul"):
        config = getattr(config, "chat", config)
    return bool(getattr(config, "soul", True)), str(getattr(config, "soul_file", ""))


def candidates(config: "ChatConfig", root: Path | None = None) -> list[Path]:
    """Every path a soul could come from, in the order they are tried."""
    _on, soul_file = _settings(config)
    paths: list[Path] = []
    if soul_file:
        paths.append(Path(soul_file).expanduser())
    if root is not None:
        paths.extend(root / name for name in SOUL_NAMES)
    if DEFAULT_SOUL_PATH not in paths:
        paths.append(DEFAULT_SOUL_PATH)
    return paths


def load_soul(config: "ChatConfig", root: Path | None = None) -> Soul:
    """Read the soul this session should run under."""
    on, _file = _settings(config)
    if not on:
        return Soul(None, "", "disabled")
    looked = tuple(candidates(config, root))
    for path in looked:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return Soul(path, text, "file", looked)
    return Soul(None, "", "missing", looked)


def soul_block(soul: Soul) -> str:
    """The system-message block for a loaded soul (``""`` when there is none)."""
    if not soul.loaded:
        return ""
    where = f" (from {soul.path})" if soul.path else ""
    return f"{SOUL_INTRO}{where}\n\n{soul.text}"
