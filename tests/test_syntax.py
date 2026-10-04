"""Which lexer a fenced code block ends up highlighted with.

The interesting cases are the ones where the tag cannot be trusted: a fence
with no tag at all, one with options after the language, one whose tag pygments
has never heard of, and the inline-looking fence that is a tag and nothing else.
"""
from __future__ import annotations

import io
import json as jsonlib

import pytest
from rich.console import Console

from rt_harness import themes as retro
from rt_harness.textual_chat import (
    TextualChatUI,
    Transcript,
    resolve_language,
    split_fence,
)

textual = pytest.importorskip("textual")

from textual.app import App, ComposeResult  # noqa: E402

PY = "def f(x):\n    return x + 1\n"
JS = '''
import React, { useState } from "react";

export default function Counter({ start = 0 }) {
  const [n, setN] = useState(start);
  const inc = () => setN((v) => v + 1);
  return <button onClick={inc}>clicked {n} times</button>;
}
'''
SHEBANG_PY = "#!/usr/bin/env python3\nimport os\nprint(os.getcwd())\n"
SHEBANG_SH = "#!/bin/bash\nset -euo pipefail\nls -la\n"
DOC = jsonlib.dumps({"name": "redtram", "scripts": {"test": "pytest -q"}}, indent=2)
PROSE = "The model weighed two options here.\nIt chose the quieter one.\n"


class StubUI:
    semantic = True
    show_thinking = False


class Harness(App):
    """A bare app carrying one transcript."""

    def on_mount(self) -> None:
        retro.register(self)
        self.theme = "matrix"

    def compose(self) -> ComposeResult:
        yield Transcript(StubUI(), id="transcript")


# -- the fence itself --------------------------------------------------------

@pytest.mark.parametrize(
    "chunk, expected",
    [
        ("python\ndef f(): pass\n", ("python", "def f(): pass\n")),
        ("no tag at all", ("", "no tag at all")),
        ("python {linenos=table}\nx = 1\n", ("python {linenos=table}", "x = 1\n")),
        ("\nx = 1\n", ("", "x = 1\n")),
        ("text\n", ("text", "")),
        ("ls -la\ntotal 4\n", ("ls -la", "total 4\n")),
        ("python", ("python", "")),           # a tag with no body, not code
        ("sh", ("sh", "")),
        ("x = 1", ("", "x = 1")),              # one line, but not a language
        ("", ("", "")),
    ],
)
def test_split_fence(chunk: str, expected: tuple[str, str]) -> None:
    assert split_fence(chunk) == expected


# -- naming the language -----------------------------------------------------

@pytest.mark.parametrize(
    "tag, expected",
    [
        ("python", "python"),
        ("py", "py"),
        ("python3", "python3"),
        ("c++", "c++"),
        ("Python", "Python"),                  # pygments is case-insensitive
        ("python {linenos=table}", "python"),  # options after the language
        ("json {hl_lines=1}", "json"),
        ("bash", "bash"),
    ],
)
def test_a_known_tag_is_honoured(tag: str, expected: str) -> None:
    assert resolve_language(tag, PY) == expected


@pytest.mark.parametrize("tag", ["notalang", "pythn", "language", "not-a-language"])
def test_an_unknown_tag_stays_plain(tag: str) -> None:
    """The author made a choice we cannot honour -- not grounds to re-guess."""
    assert resolve_language(tag, DOC) == ""


@pytest.mark.parametrize(
    "code, expected",
    [
        (SHEBANG_PY, "python"),
        (SHEBANG_SH, "bash"),
        ("#!/usr/bin/env sh\nls\n", "bash"),
        (DOC, "json"),
        ('["a", "b"]', "json"),
        ('<?xml version="1.0"?>\n<r/>\n', "xml"),
        ("<!DOCTYPE html>\n<p>hi</p>\n", "html"),
        ("<html><body>x</body></html>\n", "html"),
        ("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n", "diff"),
    ],
)
def test_an_untagged_block_is_named_on_proof(code: str, expected: str) -> None:
    assert resolve_language("", code) == expected


@pytest.mark.parametrize(
    "code",
    [
        PY,
        JS,
        PROSE,
        "SELECT a FROM t WHERE a > 1;",
        '{"a": 1',            # looks like JSON, does not parse as it
        "[not, valid, json,",
        "    indented continuation of a sentence\n",
        "",
        "\n",
    ],
)
def test_an_untagged_block_is_left_alone_on_opinion(code: str) -> None:
    """No wrong lexer: an unnameable block is plain rather than mis-coloured."""
    assert resolve_language("", code) == ""


def test_the_classifier_is_not_consulted() -> None:
    """Guard the guard: pygments' own guess is what we refuse to trust.

    Both of these are answered confidently and wrongly by ``guess_lexer``
    ("Python" for the JavaScript, "scdoc" for the shell), so neither may name a
    block here.
    """
    from pygments.lexers import guess_lexer

    assert guess_lexer(JS).name == "Python"
    assert resolve_language("", JS) == ""
    assert guess_lexer("ls -la\ntotal 4\n").aliases[0] in {"scdoc", "scd", "text"}


# -- what the transcript does with it ----------------------------------------

@pytest.mark.asyncio
async def test_the_header_names_the_language_it_used() -> None:
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)

        transcript.write_code("python {linenos=table}", PY)
        await pilot.pause()
        assert transcript.blocks[-1].language == "python"
        assert "python" in transcript.lines[0].text

        transcript.write_code("", SHEBANG_PY)
        await pilot.pause()
        assert transcript.blocks[-1].language == "python", "sniffed from the shebang"

        transcript.write_code("notalang", PY)
        await pilot.pause()
        assert transcript.blocks[-1].language == ""
        header = transcript.lines[transcript.spans[2][0]].text
        assert " code " in header, "an unnamed block is just 'code'"
        assert "python" not in header


class _Stream:
    """A front end stand-in: records what ``_write`` dispatches."""

    app = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def _dispatch(self, method: str, *args) -> None:
        self.calls.append((method, args))

    @property
    def code(self) -> list[tuple]:
        return [args for method, args in self.calls if method == "render_code"]


def test_streaming_splits_prose_from_fences() -> None:
    stream = _Stream()
    TextualChatUI._write(
        stream,
        "here you go\n```python\nx = 1\n```\nand\n```\n#!/bin/sh\nls\n```\n"
        "a tag with no body:\n```python```\ndone\n",
    )

    assert stream.code == [("python", "x = 1"), ("", "#!/bin/sh\nls")]
    prose = " ".join(args[0] for method, args in stream.calls if method == "render_line")
    assert "here you go" in prose and "and" in prose and "done" in prose
    assert "python" not in prose, "the bare tag never reaches the transcript"


def test_streaming_ignores_an_empty_fence() -> None:
    stream = _Stream()
    TextualChatUI._write(stream, "before\n```\n```\nafter\n")
    assert stream.code == []
    assert [m for m, _ in stream.calls] == ["render_line", "render_line"]


# -- reaching rich -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_block_hands_rich_a_real_lexer_and_theme() -> None:
    """The last link: the alias and the theme make it to the highlighter.

    Everything above settles which *name* a block carries and whether it is
    drawn on the band. This is the other half -- that the name becomes a lexer
    rich can use and that rich paints more than one colour with it, which is
    what separates highlighting from a monochrome slab of text.
    """
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_code("python", PY)
        await pilot.pause()

        block = transcript.blocks[-1]
        syntax = transcript._code(block, retro.block_background("matrix", "code"), 78)

        assert syntax.lexer is not None, "a tag pygments knows must resolve"
        assert "python" in syntax.lexer.name.lower(), syntax.lexer.name

        console = Console(
            width=78, file=io.StringIO(), force_terminal=True, color_system="truecolor"
        )
        colours = {
            segment.style.color
            for segment in console.render(syntax)
            if segment.style is not None and segment.style.color is not None
        }
        assert len(colours) >= 2, f"one colour is not highlighting: {colours}"
