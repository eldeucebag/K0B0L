#!/usr/bin/env python3
"""Semantic colour in the transcript: does it colour *data*, and per theme?

Plain script, no pytest — prints ok/FAIL per check and exits non-zero on the
first failure summary. Two claims are being held to account here:

  * the rules claim the data and leave the prose alone (no false positives on
    ordinary sentences, no text lost in the process);
  * the colour is the *theme's* — every hex in a theme's role table is a hex
    that theme already owns, and switching theme recolours the same datum.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rich.style import Style  # noqa: E402
from rich.text import Text  # noqa: E402

from rt_harness import semantic  # noqa: E402
from rt_harness.config import ChatConfig, Config  # noqa: E402
from rt_harness.themes import (  # noqa: E402
    DEFAULT_SEMANTIC_ROLES,
    RETRO_SYNTAX,
    RETRO_THEMES,
    SEMANTIC_ROLES,
)

failed: list[str] = []
count = 0


def check(label: str, condition: bool) -> None:
    global count
    count += 1
    if condition:
        print(f"ok   {label}")
    else:
        print(f"FAIL {label}")
        failed.append(label)


HEX = re.compile(r"#[0-9a-fA-F]{6}")


def palette(name: str) -> set[str]:
    """Every hex a retro theme already uses, chrome and code alike."""
    theme = RETRO_THEMES[name]
    # PygmentsSyntaxTheme keeps its token map on the wrapped pygments class.
    style_class = getattr(RETRO_SYNTAX[name], "_pygments_style_class", None)
    sources = [
        vars(theme).values(),
        getattr(theme, "variables", {}).values(),
        getattr(style_class, "styles", {}).values(),
    ]
    found: set[str] = set()
    for source in sources:
        for value in source:
            found.update(h.upper() for h in HEX.findall(str(value)))
    return found


def claimed(text: str) -> list[tuple[int, int, str]]:
    return semantic.spans(text)


def category_of(text: str, needle: str) -> str | None:
    start = text.index(needle)
    end = start + len(needle)
    for lo, hi, category in claimed(text):
        if lo <= start and hi >= end:
            return category
    return None


print("--- the rules claim the data ---")

CASES = [
    ("the harness talks to http://127.0.0.1:11436 right now", "http://127.0.0.1:11436", "url"),
    ("ollama answers on 127.0.0.1:11434", "127.0.0.1:11434", "url"),
    ("started with CHAT_SEMANTIC=0 set", "CHAT_SEMANTIC=0", "env"),
    ("the shell expanded $HOME first", "$HOME", "env"),
    ("wrote /home/jeff/redtram/docs/semantic.md just now", "/home/jeff/redtram/docs/semantic.md", "path"),
    ("see runctl.py:155 for the reason", "runctl.py:155", "path"),
    ("the entry point is thinlizzy.py", "thinlizzy.py", "path"),
    ("then /run start --target seam ran", "/run start", "command"),
    ("pass --no-semantic to turn it off", "--no-semantic", "flag"),
    ("the model called read_file twice", "read_file", "tool"),
    ("qwen3:8b answered in the end", "qwen3:8b", "model"),
    ("the verdict came back refused", "refused", "verdict"),
    ("all suites passed on the retry", "all suites passed", "outcome_good"),
    ("Traceback (most recent call last)", "Traceback", "outcome_bad"),
    ("the run ended with exit=1", "exit=1", "outcome_bad"),
    ("[model] qwen3:8b · text tools", "[model]", "tag"),
    ("the suite ran 425 checks in 12s", "425", "number"),
    ("the suite ran 425 checks in 12s", "12s", "number"),
]
for line, needle, expected in CASES:
    got = category_of(line, needle)
    check(f"{expected}: {needle!r} is claimed as {expected} (got {got})", got == expected)

print("\n--- ...and leave the prose alone ---")
QUIET = [
    "and/or whatever you prefer",
    "the clock said 12:34:56 and nobody cared",
    "Objective: probe the artifact",
    "Empty is a state, not a verdict",
    "the plan is simple and the prose is plain",
]
for line in QUIET:
    spans = claimed(line)
    check(f"no false positives in {line!r} (got {spans})", not spans)

print("\n--- spans agree with each other ---")
busy = "run /run status after 12s; log: /home/jeff/redtram/x.log:7 and exit=0 for qwen3:8b"
spans = claimed(busy)
check("spans come back sorted", spans == sorted(spans))
overlaps = [
    (a, b)
    for (a_start, a_end, _), (b_start, b_end, _) in zip(spans, spans[1:])
    if b_start < a_end
]
check(f"no span overlaps another (got {overlaps})", not overlaps)
check("every span is inside the text", all(0 <= lo < hi <= len(busy) for lo, hi, _ in spans))
check("a url is one span, not a model tag too", len(claimed("see https://example.com/x for it")) == 1)
check("a path keeps its leading slash", "/home/jeff/redtram/x.log:7" in [busy[lo:hi] for lo, hi, _ in spans])

print("\n--- text survives the colouring ---")
# Lines whose brackets are text rather than markup: not a character may move.
EXACT = [
    "plain",
    "",
    "an array [1, 2] and a list [ok, no]",
    "[model] qwen3:8b · text tools",
    "an unclosed [bold tag does not raise",
]
for sample in EXACT:
    body = semantic.highlight(sample)
    check(f"a Text comes back for {sample!r}", isinstance(body, Text))
    check(f"nothing is lost from {sample!r} (got {body.plain!r})", body.plain == sample)
markup = semantic.highlight("[bold red]error[/bold red] stays markup")
check("real markup is still applied, not printed", markup.plain == "error stays markup")
check("no bracket survives a real tag", "[" not in markup.plain)
check("the text is unchanged with colour off", semantic.highlight("a /path/here", enabled=False).plain == "a /path/here")
check("and nothing is coloured with it off", not semantic.highlight("a /path/here", enabled=False).spans)

print("\n--- the colour is the theme's ---")
DEFAULT_STYLE = DEFAULT_SEMANTIC_ROLES
check("every category has a default style", all(DEFAULT_STYLE.get(c) for c in semantic.CATEGORIES))
check("every rule names a known category",
      all(category in semantic.CATEGORIES for category, _ in semantic.RULES))

for name in RETRO_THEMES:
    table = SEMANTIC_ROLES.get(name)
    check(f"{name}: has a role table", bool(table))
    if not table:
        continue
    check(f"{name}: covers every category", all(c in table for c in semantic.CATEGORIES))
    check(f"{name}: styles differ from the default table", table != DEFAULT_STYLE)
    check(f"{name}: at least 6 distinct styles, so data reads apart",
          len(set(table.values())) >= 6)
    own = palette(name)
    strays = {c: s for c, s in table.items() if any(h.upper() not in own for h in HEX.findall(s))}
    check(f"{name}: every colour is one the theme already owns (strays: {strays})", not strays)

for name, table in list(SEMANTIC_ROLES.items()):
    for category, style in table.items():
        try:
            Style.parse(style)
        except Exception as exc:  # noqa: BLE001
            check(f"{name}.{category}: {style!r} parses as a rich style ({exc})", False)
check("every style in every table parses", True)

check("an unknown theme falls back to the default table",
      semantic.styles_for("no-such-theme") is DEFAULT_STYLE)
check("an empty theme name falls back too", semantic.styles_for("") is DEFAULT_STYLE)
check("a known theme gets its own table", semantic.styles_for("amber") is SEMANTIC_ROLES["amber"])

LINE = "wrote /home/jeff/redtram/docs/semantic.md in 12s"
hotdog = semantic.highlight(LINE, theme="hotdog-3x")
amber = semantic.highlight(LINE, theme="amber")
matrix = semantic.highlight(LINE, theme="matrix")
styles = lambda body: sorted({str(s.style) for s in body.spans})  # noqa: E731
check("switching theme recolours the same datum", styles(hotdog) != styles(amber))
check("two mono themes differ too", styles(amber) != styles(matrix))
check("the path span carries the theme's path role",
      SEMANTIC_ROLES["hotdog-3x"]["path"] in styles(hotdog))
check("a theme's number role reaches the number", SEMANTIC_ROLES["amber"]["number"] in styles(amber))
hotdog_path = next(str(s.style) for s in hotdog.spans if LINE[s.start:s.end].endswith("semantic.md"))
mono_path = next(str(s.style) for s in amber.spans if LINE[s.start:s.end].endswith("semantic.md"))
check(f"mono theme says a location with an attribute, not a hue ({mono_path!r})",
      "underline" in mono_path and "#" in mono_path)

print("\n--- the switch reaches the front end ---")
check("the config carries the flag", ChatConfig().semantic is True)
check("CHAT_SEMANTIC=0 turns it off", Config.from_env({"CHAT_SEMANTIC": "0"}).chat.semantic is False)
check("CHAT_SEMANTIC=1 leaves it on", Config.from_env({"CHAT_SEMANTIC": "1"}).chat.semantic is True)
check("the default is on", Config.from_env({}).chat.semantic is True)
check("enabled() reads a ChatConfig", semantic.enabled(ChatConfig()) is True)
check("enabled() reads a ChatConfig that says no", semantic.enabled(ChatConfig(semantic=False)) is False)
check("enabled() reads a whole Config", semantic.enabled(Config.from_env({"CHAT_SEMANTIC": "0"})) is False)

from rt_harness import cli, textual_chat  # noqa: E402

args = cli.build_parser().parse_args(["--no-semantic", "--list-modes"])
check("--no-semantic parses", args.chat_semantic is False)
bare = cli.build_parser().parse_args(["--list-modes"])
check("not passing --no-semantic leaves the decision to config", bare.chat_semantic is None)

check("/semantic is a command the UI knows", "/semantic" in textual_chat._COMMANDS)
check("/semantic has help text", bool(textual_chat.COMMAND_INFO.get("/semantic")))
check("/semantic is in the help screen", "/semantic" in textual_chat.HELP_TEXT)
check("the help screen says what it colours", "colour data" in textual_chat.HELP_TEXT)


class StubUI(textual_chat.TextualChatUI):
    """No console, no app: just enough to exercise the command."""

    def __init__(self) -> None:
        self.semantic = True
        self.app = None
        self.said: list[str] = []

    def line(self, text: str) -> None:
        self.said.append(text)


ui = StubUI()
check("the command is handled by the UI, not the model", ui.dispatch("/semantic") is True)
ui.dispatch("/semantic off")
check("/semantic off turns it off", ui.semantic is False)
ui.dispatch("/semantic on")
check("/semantic on turns it back on", ui.semantic is True)
ui.dispatch("/semantic sideways")
check("a word it does not know leaves the setting alone", ui.semantic is True)
check("and says so, instead of guessing", any("sideways" in line for line in ui.said))
ui.dispatch("/semantic")
check("a bare /semantic reports the state", any("on" in line for line in ui.said))

print("\n--- through the transcript, on a real app ---")


async def rendered() -> None:
    from textual.app import App, ComposeResult

    from rt_harness import themes as retro
    from rt_harness.textual_chat import Transcript

    class FakeUI:
        def __init__(self, semantic: bool = True) -> None:
            self.semantic = semantic

    class Probe(App):
        def __init__(self, semantic: bool = True, theme: str = "hotdog-3x") -> None:
            super().__init__()
            self.ui = FakeUI(semantic)
            self.wanted = theme

        def compose(self) -> ComposeResult:
            yield Transcript(self.ui)

    async def one(theme: str, enabled: bool) -> tuple[Text, Text, str]:
        app = Probe(enabled, theme)
        async with app.run_test() as pilot:
            retro.register(app)
            app.theme = theme
            log = app.query_one(Transcript)
            seen: list[object] = []
            original = log.write

            def spy(renderable, *args, **kwargs):
                seen.append(renderable)
                return original(renderable, *args, **kwargs)

            log.write = spy
            log.write_prose(LINE)
            log.write_prose("[model] qwen3:8b · text tools")
            await pilot.pause()
            return seen[0], seen[1], app.export_screenshot()

    body, tagged, svg = await one("hotdog-3x", True)
    check("the transcript is handed a styled Text", isinstance(body, Text) and bool(body.spans))
    check("with the path span in the theme's path colour",
          SEMANTIC_ROLES["hotdog-3x"]["path"] in {str(s.style) for s in body.spans})
    check("the [model] line label prints as written", tagged.plain.startswith("[model] "))
    check("and the label is coloured as a tag",
          SEMANTIC_ROLES["hotdog-3x"]["tag"] in {str(s.style) for s in tagged.spans})
    number_colour = HEX.findall(SEMANTIC_ROLES["hotdog-3x"]["number"])[0].lower()
    check(f"the number colour ({number_colour}) reaches the rendered screen",
          number_colour in svg.lower())

    other, other_tagged, other_svg = await one("amber", True)
    amber_number = HEX.findall(SEMANTIC_ROLES["amber"]["number"])[0].lower()
    check("another theme paints the same line differently",
          {str(s.style) for s in body.spans} != {str(s.style) for s in other.spans})
    check("and its screen carries amber's own colour",
          amber_number in other_svg.lower())
    check("the hotdog colour is not on the amber screen",
          number_colour != amber_number)

    plain, plain_tagged, plain_svg = await one("hotdog-3x", False)
    check("with colour off the line is written unstyled", isinstance(plain, Text) and not plain.spans)
    check("and the colour leaves the screen with it", number_colour not in plain_svg.lower())
    check("the [model] tag still prints when colour is off",
          plain_tagged.plain.startswith("[model] "))


asyncio.run(rendered())

print()
if failed:
    print(f"{len(failed)} failed: {', '.join(failed)}")
    sys.exit(1)
print(f"all semantic checks passed ({count} checks, {len(RETRO_THEMES)} theme tables)")
