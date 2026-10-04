"""The docs directory: what this harness can do, in markdown, for the model.

Asked "what can you do?", a session should answer from the product docs rather
than from whatever it can infer about the source. Three things read these files:

* the system message carries the index below -- topics, plus how to fetch one,
* the ``read_docs`` info tool returns a whole document on request,
* ``/docs`` prints one for the operator.

Every document is plain markdown whose first line is an ``# H1`` title and
whose first paragraph is a one-line summary; that pair is what the index shows.
A document that names file paths names *real* ones -- keep it that way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ChatConfig

#: The docs that ship with the harness.
DEFAULT_DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"

#: Longest summary the index will carry, in characters.
SUMMARY_CHARS = 140

#: How the index introduces itself in the system message.
DOCS_INTRO = (
    "Product documentation lives under the docs directory. For any question "
    "about what this harness can do, its commands, or its behaviour, fetch the "
    "matching document with the read_docs tool (or /docs <name>) and answer "
    "from it. The index below is a list of topics, not the answer; if no "
    "document covers the question, say so instead of guessing."
)


@dataclass(frozen=True)
class Doc:
    """One markdown file under the docs directory."""

    name: str
    path: Path
    title: str
    summary: str

    def line(self) -> str:
        return f"- {self.name}: {self.title} -- {self.summary}"


def docs_dir(config: "ChatConfig") -> Path:
    """Where this session's docs live: configured, else the shipped ones.

    Takes a :class:`ChatConfig` or a whole :class:`Config` (the info tools hold
    the latter), so the setting is read wherever it lives.
    """
    value = getattr(config, "docs_dir", "")
    if not value:
        value = getattr(getattr(config, "chat", None), "docs_dir", "")
    if value:
        return Path(value).expanduser()
    return DEFAULT_DOCS_DIR


def _parse(path: Path) -> Doc:
    title = path.stem
    summary = ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Doc(path.stem, path, title, summary)
    for block in text.splitlines():
        line = block.strip()
        if not line:
            continue
        if line.startswith("# "):
            title = line[2:].strip()
            continue
        if line.startswith("#"):
            continue
        summary = line
        break
    if len(summary) > SUMMARY_CHARS:
        summary = summary[: SUMMARY_CHARS - 1].rstrip() + "…"
    return Doc(path.stem, path, title, summary)


def list_docs(config: "ChatConfig") -> list[Doc]:
    """Every document, by name. README is the human index, not a topic."""
    root = docs_dir(config)
    try:
        paths = sorted(p for p in root.glob("*.md") if p.is_file())
    except OSError:
        return []
    return [_parse(path) for path in paths if path.stem.lower() != "readme"]


def docs_index(config: "ChatConfig") -> str:
    """The system-message block listing topics (``""`` when there are none)."""
    docs = list_docs(config)
    if not docs:
        return ""
    lines = [DOCS_INTRO, "", "Topics:"]
    lines.extend(doc.line() for doc in docs)
    return "\n".join(lines)


def find_doc(config: "ChatConfig", name: str) -> Doc | None:
    """Resolve a topic: exact name, then unique prefix, then title match."""
    wanted = name.strip().lower()
    if wanted.endswith(".md"):
        wanted = wanted[: -len(".md")]
    if not wanted:
        return None
    docs = list_docs(config)
    for doc in docs:
        if doc.name.lower() == wanted:
            return doc
    matches = [doc for doc in docs if doc.name.lower().startswith(wanted)]
    if len(matches) == 1:
        return matches[0]
    by_title = [doc for doc in docs if doc.title.lower() == wanted]
    if len(by_title) == 1:
        return by_title[0]
    if matches:
        return matches[0]
    return None


def read_doc(config: "ChatConfig", name: str) -> str | None:
    """The document's markdown, or ``None`` when no topic matches."""
    doc = find_doc(config, name)
    if doc is None:
        return None
    try:
        return doc.path.read_text(encoding="utf-8")
    except OSError:
        return None
