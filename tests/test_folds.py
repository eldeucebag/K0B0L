"""Reasoning and code blocks: their own background, and a fold.

The transcript keeps blocks as source so a fold can redraw what follows it,
so these check both halves: the band each kind paints, and that folding one
block really does change what the rows below it say.
"""
from __future__ import annotations

import pytest

from rt_harness import themes as retro
from rt_harness.textual_chat import Transcript

textual = pytest.importorskip("textual")

from textual.app import App, ComposeResult  # noqa: E402
from textual import events  # noqa: E402

REASONING = "the model weighed\ntwo options here"
CODE = "def band():\n    return 'slab'\n"


class StubUI:
    """The few attributes the transcript reads off a front end."""

    semantic = True
    show_thinking = False


class Harness(App):
    """A bare app carrying one transcript, so the blocks can be driven.

    The border is the real app's (``ChatApp.CSS``) and is load-bearing: a mouse
    press is handed to the widget with its border counted, so a harness without
    one hides every off-by-one in the click path.

    ``watch_theme`` mirrors ``ChatApp``'s wiring (the real one needs a whole
    front end to build); the live test drives the real app.
    """

    CSS = "#transcript { border: solid $surface-lighten-1; }"

    def on_mount(self) -> None:
        retro.register(self)
        self.theme = "matrix"

    def watch_theme(self, theme: str) -> None:
        for transcript in self.query(Transcript):
            transcript.retag(theme)

    def compose(self) -> ComposeResult:
        yield Transcript(StubUI(), id="transcript")


def bands(transcript: Transcript) -> list[str]:
    """The background painted behind each rendered row, in order."""
    out = []
    for strip in transcript.lines:
        colour = ""
        for segment in strip:
            style = getattr(segment, "style", None)
            if style is not None and style.bgcolor is not None:
                colour = style.bgcolor.get_truecolor().hex.lower()
                break
        out.append(colour)
    return out


@pytest.mark.asyncio
async def test_reasoning_band_and_default_fold() -> None:
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_reasoning(REASONING)
        await pilot.pause()

        block = transcript.blocks[-1]
        assert block.kind == "reasoning"
        assert block.folded, "reasoning arrives folded"
        assert block.theme == "matrix", "a block remembers the theme it arrived under"

        # Folded: one row, the header, on the reasoning band.
        assert len(transcript.lines) == 1
        assert bands(transcript)[0] == retro.block_background("matrix", "reasoning").lower()
        assert "thinking" in transcript.lines[0].text

        transcript.toggle(block)
        await pilot.pause()
        assert not block.folded
        # Unfolded: the header plus the two lines of thought.
        assert len(transcript.lines) == 3
        band = retro.block_background("matrix", "reasoning").lower()
        assert all(colour == band for colour in bands(transcript))
        assert "two options here" in transcript.lines[2].text


@pytest.mark.asyncio
async def test_code_band_and_open_by_default() -> None:
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_code("python", CODE)
        await pilot.pause()

        block = transcript.blocks[-1]
        assert not block.folded, "code is readable without a click"
        assert len(transcript.lines) == 4, "header plus three code lines"
        code_band = retro.block_background("matrix", "code").lower()
        assert bands(transcript)[0] == code_band
        assert bands(transcript)[1] == code_band
        assert "band" in transcript.lines[1].text


@pytest.mark.asyncio
async def test_prose_is_never_a_block() -> None:
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_prose("just an answer")
        await pilot.pause()

        assert transcript.blocks[-1].kind == "prose"
        assert len(transcript.lines) == 1
        assert bands(transcript) == [""], "body text keeps the transcript background"


@pytest.mark.asyncio
async def test_folding_shifts_what_follows() -> None:
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_reasoning(REASONING)
        transcript.write_prose("the answer")
        await pilot.pause()

        assert [b.text for b in transcript.lines][-1].endswith("the answer")
        before = len(transcript.lines)
        reasoning = transcript.blocks[0]
        transcript.toggle(reasoning)          # unfolded
        await pilot.pause()
        assert len(transcript.lines) == before + 2
        transcript.toggle(reasoning)          # folded again
        await pilot.pause()
        assert len(transcript.lines) == before, "folding gives the rows back"
        assert transcript.lines[-1].text.startswith("the answer")


@pytest.mark.asyncio
async def test_set_folded_counts_and_reports() -> None:
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        for _ in range(2):
            transcript.write_reasoning(REASONING)
        transcript.write_code("python", CODE)
        await pilot.pause()

        assert transcript.set_folded("reasoning", True) == 0, "already folded"
        assert transcript.set_folded("code", True) == 1
        await pilot.pause()
        assert transcript.fold_report() == [("reasoning", 2, 2), ("code", 1, 1)]
        assert transcript.set_folded("code", False) == 1
        assert transcript.fold_report() == [("reasoning", 2, 2), ("code", 0, 1)]


def header_cell(transcript: Transcript) -> tuple[int, int]:
    """The screen cell of the last block's header row."""
    inner = transcript.content_region
    row = transcript.spans[-1][0] - int(transcript.scroll_offset.y)
    return inner.x + 2, inner.y + row


def press(app: App, x: int, y: int) -> None:
    """Post a press the way a terminal reports one: in *screen* coordinates.

    ``App.on_event`` hit-tests the widget with x/y and only then re-targets the
    event, so this is the only way to exercise the border and the scroll -- a
    handler called directly, or a widget-local coordinate, hides both.
    """
    app.post_message(events.MouseDown(None, x, y, 0, 0, 1, False, False, False,
                                      screen_x=x, screen_y=y))


@pytest.mark.asyncio
async def test_a_press_on_a_header_folds_it() -> None:
    """The press alone folds: no Click, and no release, is needed.

    Textual only makes a Click when the same widget is under the pointer at
    press and release, so a real click in a scrolled transcript often produced
    none at all -- which is why clicking a header used to do nothing.
    """
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_prose("\n".join(f"line {n}" for n in range(40)))
        transcript.write_reasoning(REASONING)
        await pilot.pause()

        header = transcript.spans[-1][0]
        assert transcript.header_at(header) is transcript.blocks[-1]
        assert transcript.header_at(header + 1) is None, "a body row is not a header"

        block = transcript.blocks[-1]
        assert block.folded, "reasoning arrives folded"
        x, y = header_cell(transcript)
        assert y > 4, "the header is scrolled down, clear of the border"
        press(app, x, y)
        await pilot.pause()
        assert not block.folded, "a press on the visible header row unfolds it"

        press(app, x, y)
        await pilot.pause()
        assert block.folded, "and the next one folds it again"


@pytest.mark.asyncio
async def test_a_press_one_row_below_the_header_folds_nothing() -> None:
    """The mapping must be exact, border included.

    The row under the press is the row it means: the widget is handed a ``y``
    that counts its border, so ignoring the border put every press one row high
    -- here, a press on the first line of thought would have folded the header
    above it.
    """
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_reasoning(REASONING)
        transcript.toggle(transcript.blocks[-1])       # open, so the row below
        await pilot.pause()                            # is its own first line

        block = transcript.blocks[-1]
        assert not block.folded
        x, y = header_cell(transcript)
        press(app, x, y + 1)
        await pilot.pause()
        assert not block.folded, "the line under the header is not the header"


@pytest.mark.asyncio
async def test_a_release_elsewhere_still_counts_as_that_press() -> None:
    """A press folds even if the hand wobbles and the release lands elsewhere.

    Pairing press with release looked forgiving and was not: one row of drift,
    or a release that ends on the bar below, dropped the fold silently.
    """
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_reasoning(REASONING)
        await pilot.pause()

        block = transcript.blocks[-1]
        assert block.folded
        x, y = header_cell(transcript)
        press(app, x, y)
        # The release is not a fold trigger at all, let alone a veto.
        await pilot.pause()
        assert not block.folded, "the press folded it; a stray release is ignored"


@pytest.mark.asyncio
async def test_a_real_click_through_the_app_folds_the_block() -> None:
    """The handler must be a name Textual dispatches, not merely a method.

    ``on_click`` was the original bug: Textual only makes a Click when the same
    widget is under the pointer at press and release. This drives a whole click
    -- Pilot posts MouseDown, MouseUp and Click -- so it fails if the fold stops
    being driven by the press.
    """
    app = Harness()
    async with app.run_test(size=(100, 20)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_reasoning(REASONING)
        await pilot.pause()

        block = transcript.blocks[-1]
        assert block.folded, "reasoning arrives folded"
        inner, outer = transcript.content_region, transcript.region
        offset = (inner.x - outer.x + 2, inner.y - outer.y + transcript.spans[0][0])
        await pilot.click("#transcript", offset=offset)
        await pilot.pause()
        assert not block.folded, "a real click unfolds the block"

        await pilot.click("#transcript", offset=offset)
        await pilot.pause()
        assert block.folded, "and the next one folds it again"


@pytest.mark.asyncio
async def test_a_theme_change_repaints_the_blocks_already_on_screen() -> None:
    """Switching theme repaints every band, not only the blocks that follow.

    Bands are resolved as a block is drawn, so without a retag the chat keeps
    the palette it was written under -- which is how a commodore-64 blue band
    survives a move to matrix.
    """
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_reasoning(REASONING)
        transcript.write_code("python", CODE)
        await pilot.pause()

        old = bands(transcript)
        assert old[0] == retro.block_background("matrix", "reasoning").lower()
        assert [block.theme for block in transcript.blocks] == ["matrix", "matrix"]

        app.theme = "amber"
        await pilot.pause()

        assert [block.theme for block in transcript.blocks] == ["amber", "amber"]
        want_reasoning = retro.block_background("amber", "reasoning").lower()
        want_code = retro.block_background("amber", "code").lower()
        assert want_reasoning != old[0], "the two themes really do differ"
        assert bands(transcript)[0] == want_reasoning, "the band follows the theme"
        assert all(colour == want_code for colour in bands(transcript)[1:]), (
            "including every row of the code block"
        )


@pytest.mark.asyncio
async def test_a_fold_redraw_lays_every_block_down_where_the_spans_say() -> None:
    """A fold redraws the log from the blocks, so nothing may be lost or shifted.

    ``rerender`` empties the log and draws every block again, and the row ranges
    in ``spans`` are all that connects a click to a block. If a redraw dropped a
    block, or recorded a row the block does not occupy, clicking would fold the
    wrong thing -- so the check is that the row each span names still carries
    that block's header.
    """
    app = Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        transcript = app.query_one("#transcript", Transcript)
        transcript.write_prose("a short answer")
        transcript.write_reasoning(REASONING)
        transcript.write_code("python", CODE)
        await pilot.pause()

        before = len(transcript.lines)
        assert transcript.spans[0][0] == 0, "the first block starts at the top"
        assert transcript.spans[-1][1] == before, "spans describe rows that exist"

        transcript.toggle(transcript.blocks[1])      # unfold the reasoning
        await pilot.pause()
        spans = transcript.spans
        assert len(spans) == len(transcript.blocks), "the redraw lost or invented a block"
        assert len(transcript.lines) == before + 2, "unfolding gives the rows back"

        for index, (start, end) in enumerate(spans):
            block = transcript.blocks[index]
            assert end > start, "a drawn block occupies at least its header row"
            if block.kind not in retro.BLOCK_KINDS:
                assert transcript.header_at(start) is None, "prose has no header to click"
                continue
            assert transcript.header_at(start) is block, (
                f"block {index} ({block.kind}) has no header on row {start}"
            )

        for (_start, end), (next_start, _next_end) in zip(spans, spans[1:]):
            assert next_start >= end, "a redraw overlapped two blocks"


def test_builtin_themes_degrade_to_plain_text() -> None:
    """A theme with no block variables is not an error, just no band."""
    assert retro.block_background("textual-dark", "reasoning") == ""
    assert retro.block_background("matrix", "prose") == ""
    assert retro.block_emphasis("textual-dark") == "bold"
    for name in retro.RETRO_THEME_NAMES:
        for kind in retro.BLOCK_KINDS:
            assert retro.block_background(name, kind), f"{name} has no {kind} band"
        assert retro.block_emphasis(name) != "bold"
