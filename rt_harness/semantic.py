"""Semantic colour in the chat transcript: highlighting pointed at data.

Fenced code already gets pygments. Prose does not — and in this harness most
prose *is* data: a path, a slash command, an env assignment, a model tag, the
``host:port`` a run is talking to, the verdict it came back with. Read at a
glance those are the words that carry the meaning, and uncoloured they read as
soup.

So: the same trick, aimed at meaning instead of grammar. ``RULES`` recognise
the kinds of datum this harness actually prints; ``themes.SEMANTIC_ROLES`` says
what colour each kind gets *in the current theme*. The palette stays the
theme's — the mono themes (amber, matrix) have one hue and differentiate by
intensity and attribute rather than importing a colour the machine never had.

Rules are ordered most-specific-first and the first rule to claim a span keeps
it, because the kinds overlap: a URL contains a ``host:port`` the model-tag rule
would take, a path contains digits the number rule would take, a file:line is
one datum rather than a path plus a number.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from rich.markup import MarkupError, render
from rich.style import Style
from rich.text import Text

from .themes import DEFAULT_SEMANTIC_ROLES, SEMANTIC_ROLES

#: Every kind of datum the transcript knows how to colour.
CATEGORIES: tuple[str, ...] = (
    "url",
    "env",
    "path",
    "command",
    "flag",
    "tool",
    "model",
    "verdict",
    "outcome_good",
    "outcome_bad",
    "tag",
    "number",
)

#: The harness's own tools and read-only info tools: what a run *did*.
TOOL_WORDS: tuple[str, ...] = (
    "read_file",
    "list_files",
    "search_files",
    "write_file",
    "edit_file",
    "read_docs",
    "harness_help",
    "list_models",
    "list_modes",
    "list_sessions",
    "list_skills",
)

#: The bracket tags the harness prints as a line label — `[model]`, `[run]`.
#: They reach the transcript escaped (``\\[model]``) so markup leaves them
#: alone; by the time a rule sees them they are ordinary text again.
TAG_WORDS: tuple[str, ...] = (
    "model",
    "run",
    "tools",
    "docs",
    "soul",
    "theme",
    "skills",
    "session",
    "read",
    "target",
    "root",
    "think",
    "paste",
    "plan",
    "semantic",
)

#: File extensions worth calling a path. Deliberately a list, not ``\S+``:
#: colouring every dotted token would paint half the prose.
EXTS = "py|md|sh|json|jsonl|txt|log|tcss|ya?ml|toml|cfg|ini"

#: A slash command's own words. Without this the command rule runs on into the
#: sentence — `/run status says log` is one command and three prose words.
SUBCOMMANDS = (
    "start|status|tail|stop|save|load|list|clear|history|exit|help|on|off|"
    "enable|disable|full|summary|theme|model|target|root|read|paste|docs|"
    "soul|semantic|think|tools|skills|session|run|attempts"
)

#: Any bracketed run short enough to be a tag rather than prose.
BRACKET_GROUP = re.compile(r"\[([^\[\]\n]{0,60})\]")

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s]+")),
    ("url", re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d{2,5})\b")),
    ("env", re.compile(r"\b[A-Z][A-Z0-9_]{2,}=\S+")),
    ("env", re.compile(r"\$\{?[A-Z][A-Z0-9_]{2,}\}?")),
    ("path", re.compile(rf"(?:(?<=^)|(?<=[\s(\[{{,'\"=]))/?[\w./-]+\.(?:{EXTS}):\d+\b")),
    ("path", re.compile(r"(?:(?<=^)|(?<=[\s(\[{,'\"=]))(?:~|\.{1,2})?/[\w@.+-]+(?:/[\w@.+-]+)+/?")),
    ("path", re.compile(rf"\b[\w-]+\.(?:{EXTS})\b")),
    ("command", re.compile(rf"(?<![\w/])/[a-z][a-z0-9-]*(?:\s+(?:{SUBCOMMANDS})\b)?")),
    ("flag", re.compile(r"(?<!\S)--[A-Za-z][\w-]*")),
    ("flag", re.compile(r"(?<!\S)-[A-Za-z](?![\w-])")),
    ("tool", re.compile(r"\b(?:" + "|".join(TOOL_WORDS) + r")\b")),
    # A model tag starts with a letter: ``qwen3:8b`` yes, ``12:34`` no.
    ("model", re.compile(r"\b[a-z][\w./+-]*:[A-Za-z0-9][\w.-]*\b")),
    ("verdict", re.compile(r"\bVERDICT\b")),
    ("verdict", re.compile(r"\b(?:refused|complied|partial|unclear|no-verdict)\b")),
    ("outcome_good", re.compile(r"\b(?:PASS|PASSED)\b|\ball suites passed\b")),
    ("outcome_bad", re.compile(
        r"\b(?:FAIL|FAILED|FAILURE|Traceback|TimeoutExpired)\b|\bexit=[1-9]\d*\b"
    )),
    ("tag", re.compile(r"\[(?:" + "|".join(TAG_WORDS) + r")\]")),
    # Durations and sizes first, then bare numbers: a clock time is not a
    # datum, so the bare rule refuses to colour the parts of ``12:34:56``.
    ("number", re.compile(r"\b\d+(?:ms|s|m|h|KiB|MiB|GiB|GB|MB|KB)\b")),
    ("number", re.compile(r"(?<![\w:.])\d+(?:\.\d+)?%?(?![\w:])")),
)


@lru_cache(maxsize=512)
def _is_style(content: str) -> bool:
    """Does Rich read ``content`` as a style? If not, it is text, not markup."""
    try:
        Style.parse(content)
    except Exception:  # noqa: BLE001 - StyleError, and whatever parse raises
        return False
    return True


def escaped(text: str) -> str:
    """Escape bracket tags Rich would eat, so they print as written.

    ``[model]`` is a line label in this harness, not a style, and ``[ok, no]``
    is a list in somebody's answer. Rich's markup drops a tag it cannot parse
    without saying anything, so both used to disappear from the transcript.
    A bracket group survives untouched when Rich really would read it as a
    style (``[bold red]``), or when it is a closing tag.
    """

    def one(match: "re.Match[str]") -> str:
        content = match.group(1)
        if content.startswith("/") or _is_style(content):
            return match.group(0)
        return "\\" + match.group(0)

    return BRACKET_GROUP.sub(one, text)


def rendered(text: str) -> Text:
    """``text`` as a ``Text`` with markup applied — and never raising.

    The transcript is mostly model output, and model output contains brackets
    that were never markup. Rich's ``render`` raises ``MarkupError`` on some of
    those; a stray bracket is text, not a crash.
    """
    try:
        return render(escaped(text))
    except MarkupError:
        # Bad markup is not worth a crash and not worth a backslash on screen:
        # print what was written, including whatever bracket upset Rich.
        return Text(text)


def spans(text: str) -> list[tuple[int, int, str]]:
    """Claim ``text``'s characters for categories, most specific rule first."""
    taken = bytearray(len(text))
    found: list[tuple[int, int, str]] = []
    for category, pattern in RULES:
        for match in pattern.finditer(text):
            start, end = match.span()
            if start == end or taken[start:end].count(1):
                continue
            taken[start:end] = b"\x01" * (end - start)
            found.append((start, end, category))
    found.sort()
    return found


def styles_for(theme: str = "") -> dict[str, str]:
    """The role table for ``theme``, falling back to the terminal's own."""
    return SEMANTIC_ROLES.get(theme, DEFAULT_SEMANTIC_ROLES)


def enabled(config: Any) -> bool:
    """Is semantic colouring on? Accepts a whole ``Config`` or a ``ChatConfig``."""
    settings = getattr(config, "chat", config)
    return bool(getattr(settings, "semantic", True))


def highlight(text: str, *, theme: str = "", enabled: bool = True) -> Text:
    """``text`` as a transcript line: markup applied, data coloured."""
    body = rendered(text)
    if not enabled:
        return body
    styles = styles_for(theme)
    for start, end, category in spans(body.plain):
        style = styles.get(category)
        if style:
            body.stylize(style, start, end)
    return body
