"""Retro UI themes for the Textual chat front end.

Each entry pairs a Textual ``Theme`` — the frame: menu bar, status bars,
borders, panels — with a pygments ``Style`` so fenced code in the transcript
highlights in the same palette rather than in monokai-on-top-of-something-else.

A theme's colours are the *machine's*, not a mood board: C64 blue is the value
from the VIC-II palette, DOS blue is EGA #0000AA, the amber is the P3 phosphor
that every P3 monitor actually settled on. Where a palette has no green (hot
dog stand had two colours and no apologies) the status colours stay inside the
palette instead of importing a foreign one.

``textual_chat.THEMES`` appends ``RETRO_THEME_NAMES`` and ``ChatApp`` calls
``register`` in its constructor, so a theme is selectable by ``/theme``, the
menus, and the Options screen before anything can set it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pygments.style import Style as PygmentsStyle
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    String,
    Token,
)
from rich.syntax import PygmentsSyntaxTheme
from textual.theme import Theme

if TYPE_CHECKING:  # pragma: no cover - typing only
    from textual.app import App


# -- syntax styles -----------------------------------------------------------


def _mono_style(
    class_name: str, *, background: str, bright: str, mid: str, dim: str
) -> Any:
    """A single-hue token map, the way a phosphor monitor actually looked.

    Only brightness carries meaning: comments fade, keywords go bold, and
    strings and numbers sit between. ``bright``/``mid``/``dim`` are one hue at
    three intensities, which is the whole vocabulary a mono tube had.
    """
    return type(
        class_name,
        (PygmentsStyle,),
        {
            "background_color": background,
            "styles": {
                Token: mid,
                Comment: f"italic {dim}",
                Comment.Preproc: bright,
                Keyword: f"bold {bright}",
                Keyword.Type: bright,
                Operator: mid,
                Operator.Word: f"bold {mid}",
                Name: mid,
                Name.Builtin: bright,
                Name.Function: f"bold {bright}",
                Name.Class: f"bold {bright}",
                Name.Decorator: dim,
                Name.Tag: bright,
                Name.Attribute: mid,
                Name.Variable: mid,
                Name.Constant: bright,
                String: bright,
                Number: bright,
                Generic.Heading: f"bold {bright}",
                Generic.Subheading: f"bold {mid}",
                Generic.Deleted: dim,
                Generic.Inserted: bright,
                Generic.Error: f"bold {bright}",
                Error: f"bold {bright}",
            },
        },
    )


#: P3 amber: black tube, one hue, brightness as the only signal.
AmberSyntax = _mono_style(
    "AmberSyntax", background="#000000", bright="#FFD9A0", mid="#FFB000", dim="#8A5F00"
)

#: Digital rain: #00FF41 on black, with the dim greens the rain fades into.
MatrixSyntax = _mono_style(
    "MatrixSyntax", background="#000000", bright="#7CFF9B", mid="#00FF41", dim="#008F11"
)


class HotDogSyntax(PygmentsStyle):
    """Windows 3.1's 'Hot Dog Stand': mustard and blood, nothing else."""

    background_color = "#A80000"
    styles = {
        Token: "#FFFF00",
        Comment: "italic #FF9B9B",
        Comment.Preproc: "#FFFFFF",
        Keyword: "bold #FFFFFF",
        Keyword.Type: "bold #FFFF00",
        Operator: "#FFFFFF",
        Name: "#FFFF00",
        Name.Builtin: "#FFFFFF",
        Name.Function: "bold #FFFFFF",
        Name.Class: "bold #FFFFFF",
        Name.Decorator: "#FF9B9B",
        Name.Tag: "bold #FFFFFF",
        Name.Constant: "#FFFFFF",
        String: "#FFE97F",
        Number: "#FFFFFF",
        Generic.Heading: "bold #FFFFFF",
        Generic.Subheading: "bold #FFFF00",
        Generic.Deleted: "#FF9B9B",
        Generic.Inserted: "#FFFF00",
        Error: "bold #FFFFFF",
    }


class Commodore64Syntax(PygmentsStyle):
    """VIC-II blue with the light-blue border colour as the text colour."""

    background_color = "#352879"
    styles = {
        Token: "#9B93E8",
        Comment: "italic #6C5EB5",
        Comment.Preproc: "#B8C76F",
        Keyword: "bold #B8C76F",
        Keyword.Type: "#B8C76F",
        Operator: "#9B93E8",
        Name: "#9B93E8",
        Name.Builtin: "#C9C1FF",
        Name.Function: "bold #C9C1FF",
        Name.Class: "bold #C9C1FF",
        Name.Decorator: "#6C5EB5",
        Name.Tag: "#C9C1FF",
        Name.Constant: "#C9C1FF",
        String: "#9AD284",
        Number: "#B8C76F",
        Generic.Heading: "bold #C9C1FF",
        Generic.Subheading: "bold #B8C76F",
        Generic.Deleted: "#9A6759",
        Generic.Inserted: "#9AD284",
        Error: "bold #9A6759",
    }


class EditComSyntax(PygmentsStyle):
    """MS-DOS Editor: EGA blue, grey body text, bright primaries for tokens."""

    background_color = "#0000AA"
    styles = {
        Token: "#AAAAAA",
        Comment: "italic #00AAAA",
        Comment.Preproc: "#FFFF55",
        Keyword: "bold #FFFFFF",
        Keyword.Type: "#00AAAA",
        Operator: "#AAAAAA",
        Name: "#AAAAAA",
        Name.Builtin: "#55FFFF",
        Name.Function: "bold #FFFFFF",
        Name.Class: "bold #FFFFFF",
        Name.Decorator: "#00AAAA",
        Name.Tag: "bold #FFFFFF",
        Name.Constant: "#FFFF55",
        String: "#FFFF55",
        Number: "#55FFFF",
        Generic.Heading: "bold #FFFFFF",
        Generic.Subheading: "bold #55FFFF",
        Generic.Deleted: "#FF5555",
        Generic.Inserted: "#55FF55",
        Error: "bold #FF5555",
    }


class BeOSSyntax(PygmentsStyle):
    """A BeOS editor window: white page, black type, blue keywords."""

    background_color = "#FFFFFF"
    styles = {
        Token: "#101010",
        Comment: "italic #6E6E6E",
        Comment.Preproc: "#1A1AB8",
        Keyword: "bold #1A1AB8",
        Keyword.Type: "#00838A",
        Operator: "#101010",
        Name: "#101010",
        Name.Builtin: "#00838A",
        Name.Function: "bold #101010",
        Name.Class: "bold #1A1AB8",
        Name.Decorator: "#6E6E6E",
        Name.Tag: "bold #1A1AB8",
        Name.Constant: "#A05000",
        String: "#0A6B0A",
        Number: "#A05000",
        Generic.Heading: "bold #101010",
        Generic.Subheading: "bold #1A1AB8",
        Generic.Deleted: "#C00000",
        Generic.Inserted: "#0A6B0A",
        Error: "bold #C00000",
    }


# -- UI themes ---------------------------------------------------------------

RETRO_THEMES: dict[str, Theme] = {
    # Windows 3.1's "Hot Dog Stand" scheme, the one that shipped on purpose:
    # #FFFF00 title bars over #A80000 windows, with the black-on-yellow the
    # scheme itself used. The scheme had no green, so success stays mustard.
    "hotdog-3x": Theme(
        name="hotdog-3x",
        primary="#FFFF00",
        secondary="#FF0000",
        accent="#FFB000",
        warning="#FF8C00",
        error="#FF2D2D",
        success="#FFCC00",
        foreground="#FFFF00",
        background="#A80000",
        surface="#8E0000",
        panel="#A80000",
        dark=True,
        text_alpha=1.0,
        variables={
            "border": "#FFFF00",
            # Reasoning and code blocks paint their own background so a block
            # is a place in the transcript, not just text in it. Hot dog stand
            # has three colours, so the two bands are its red made deeper and
            # its black: a reasoning band is a darker window, a code band is
            # the black the scheme put text on.
            "reasoning-background": "#5C0000",
            "code-background": "#000000",
            "footer-key-foreground": "#FFFF00",
            "block-cursor-background": "#FFFF00",
            "block-cursor-foreground": "#000000",
            "block-cursor-text-style": "none",
            "input-selection-background": "#FFFF00 45%",
            "button-color-foreground": "#000000",
        },
    ),
    # BeOS R5: #D6D3CE window grey, the blue title bar, the yellow tab.
    "beos": Theme(
        name="beos",
        primary="#1A1AB8",
        secondary="#5A5AD0",
        accent="#F0C000",
        warning="#F0C000",
        error="#C00000",
        success="#008439",
        foreground="#101010",
        background="#D6D3CE",
        surface="#C9C5BE",
        panel="#B7B3AB",
        dark=False,
        variables={
            "border": "#8A867E",
            # A white page has room for two greys: reasoning sits in the window
            # grey one step down, code on the white editor page it already used.
            "reasoning-background": "#C9C5BE",
            "code-background": "#FFFFFF",
            "border-blurred": "#B7B3AB",
            "footer-key-foreground": "#1A1AB8",
            "block-cursor-background": "#1A1AB8",
            "block-cursor-foreground": "#FFFFFF",
            "block-cursor-text-style": "none",
            "input-selection-background": "#1A1AB8 30%",
            "button-color-foreground": "#FFFFFF",
        },
    ),
    # VIC-II palette: blue #352879 screen, light blue #6C5EB5 border.
    "commodore-64": Theme(
        name="commodore-64",
        primary="#6C5EB5",
        secondary="#4B3FA0",
        accent="#B8C76F",
        warning="#B8C76F",
        error="#9A6759",
        success="#9AD284",
        foreground="#A6A0F0",
        background="#352879",
        surface="#2A1F60",
        panel="#6C5EB5",
        dark=True,
        text_alpha=1.0,
        variables={
            "border": "#6C5EB5",
            # Two deeper VIC-II blues: the reasoning band one step under the
            # screen blue, the code band deeper again, so neither reads as body
            # text on the window.
            "reasoning-background": "#241A52",
            "code-background": "#1B1240",
            "border-blurred": "#4B3FA0",
            "footer-key-foreground": "#B8C76F",
            "block-cursor-background": "#6C5EB5",
            "block-cursor-foreground": "#352879",
            "block-cursor-text-style": "none",
            "input-selection-background": "#6C5EB5 40%",
            "button-color-foreground": "#352879",
        },
    ),
    # EGA: blue #0000AA background, grey #AAAAAA body, bright cyan/yellow.
    "edit-com": Theme(
        name="edit-com",
        primary="#00AAAA",
        secondary="#0000AA",
        accent="#FFFF55",
        warning="#FFFF55",
        error="#FF5555",
        success="#55FF55",
        foreground="#AAAAAA",
        background="#0000AA",
        surface="#000088",
        panel="#00AAAA",
        dark=True,
        text_alpha=1.0,
        variables={
            "border": "#00AAAA",
            # EGA blue has five intensities and DOS used them; the reasoning
            # band is the darker of the two, the code band darker again.
            "reasoning-background": "#000066",
            "code-background": "#000044",
            "border-blurred": "#000088",
            "footer-key-foreground": "#FFFF55",
            "block-cursor-background": "#AAAAAA",
            "block-cursor-foreground": "#0000AA",
            "block-cursor-text-style": "none",
            "input-selection-background": "#00AAAA 40%",
            "button-color-foreground": "#0000AA",
        },
    ),
    # Amber phosphor: black tube, #FFB000, brightness as the only signal.
    "amber": Theme(
        name="amber",
        primary="#FFB000",
        secondary="#8A5F00",
        accent="#FFD9A0",
        warning="#E8A200",
        error="#FF7A00",
        success="#FFC46B",
        foreground="#FFB000",
        background="#000000",
        surface="#140D00",
        panel="#2B1D00",
        dark=True,
        text_alpha=1.0,
        variables={
            "border": "#8A5F00",
            # A black tube cannot be deepened, so both bands are the one hue
            # *raised* instead: reasoning on the brighter brown (a collapsed
            # block should be visible at a glance), code on the subtler one.
            "reasoning-background": "#2B1D00",
            "code-background": "#140D00",
            "border-blurred": "#2B1D00",
            "footer-key-foreground": "#FFB000",
            "block-cursor-background": "#FFB000",
            "block-cursor-foreground": "#000000",
            "block-cursor-text-style": "none",
            "input-selection-background": "#FFB000 30%",
            "button-color-foreground": "#000000",
        },
    ),
    # Digital rain: #00FF41 on black. Errors stay red — that one signal is
    # worth breaking the hue for, because an error the same colour as the
    # text is an error nobody sees.
    "matrix": Theme(
        name="matrix",
        primary="#00FF41",
        secondary="#008F11",
        accent="#7CFF9B",
        warning="#B6FF00",
        error="#FF3B3B",
        success="#00FF41",
        foreground="#00FF41",
        background="#000000",
        surface="#001A0A",
        panel="#003B00",
        dark=True,
        text_alpha=1.0,
        variables={
            "border": "#008F11",
            # Like amber, a black screen can only be raised, not deepened: the
            # reasoning band is the brighter green-black, the code band subtler.
            "reasoning-background": "#003B00",
            "code-background": "#001A0A",
            "border-blurred": "#003B00",
            "footer-key-foreground": "#00FF41",
            "block-cursor-background": "#00FF41",
            "block-cursor-foreground": "#000000",
            "block-cursor-text-style": "none",
            "input-selection-background": "#00FF41 30%",
            "button-color-foreground": "#000000",
        },
    ),
}

#: Menu/`/theme` order. Kept in the order the palettes were asked for.
RETRO_THEME_NAMES: list[str] = [
    "hotdog-3x",
    "beos",
    "commodore-64",
    "edit-com",
    "amber",
    "matrix",
]

#: UI theme -> Rich syntax theme for fenced code in the transcript.
RETRO_SYNTAX: dict[str, PygmentsSyntaxTheme] = {
    "hotdog-3x": PygmentsSyntaxTheme(HotDogSyntax),
    "beos": PygmentsSyntaxTheme(BeOSSyntax),
    "commodore-64": PygmentsSyntaxTheme(Commodore64Syntax),
    "edit-com": PygmentsSyntaxTheme(EditComSyntax),
    "amber": PygmentsSyntaxTheme(AmberSyntax),
    "matrix": PygmentsSyntaxTheme(MatrixSyntax),
}


# -- semantic roles ----------------------------------------------------------
#
# What colour each kind of *data* gets in the transcript (see
# ``rt_harness.semantic``). One table per theme, and every colour in it comes
# out of that theme's own palette -- the same rule the syntax styles follow.
#
# The mono themes are the interesting case: one hue, so brightness and
# attribute carry the meaning instead of colour. The grouping is the same in
# every table -- verbs (a command, a tool, a model, a verdict) are bold,
# locations (a path, a URL) are underlined, literals (a flag, an env value, a
# number) sit at body brightness, and a bad outcome is the one thing allowed to
# break the hue, because an error the same colour as the text is an error
# nobody sees.

#: Fallback for a built-in theme (textual-dark and friends): named colours, so
#: the terminal's own palette decides what they actually are.
DEFAULT_SEMANTIC_ROLES: dict[str, str] = {
    "url": "bold underline cyan",
    "env": "bold magenta",
    "path": "underline blue",
    "command": "bold cyan",
    "flag": "cyan",
    "tool": "bold green",
    "model": "bold magenta",
    "verdict": "bold yellow",
    "outcome_good": "bold green",
    "outcome_bad": "bold red",
    "tag": "dim",
    "number": "yellow",
}

SEMANTIC_ROLES: dict[str, dict[str, str]] = {
    # Yellow on red: white and mustard carry the emphasis, the pink is the
    # only dim the scheme has.
    "hotdog-3x": {
        "url": "bold underline #FFFFFF",
        "env": "bold underline #FFE97F",
        "path": "underline #FFFFFF",
        "command": "bold #FFFFFF",
        "flag": "#FFE97F",
        "tool": "bold #FFCC00",
        "model": "bold #FFB000",
        "verdict": "bold #FFE97F",
        "outcome_good": "bold #FFCC00",
        "outcome_bad": "bold #FFFFFF on #FF0000",
        "tag": "#FF9B9B",
        "number": "#FFE97F",
    },
    # A white page: black type, the title-bar blue for verbs, the editor's
    # teal for locations, and the BeOS red kept for a bad outcome.
    "beos": {
        "url": "bold underline #00838A",
        "env": "bold #A05000",
        "path": "#00838A",
        "command": "bold #1A1AB8",
        "flag": "#1A1AB8",
        "tool": "bold #0A6B0A",
        "model": "bold #A05000",
        "verdict": "bold #C00000",
        "outcome_good": "bold #0A6B0A",
        "outcome_bad": "bold #C00000",
        "tag": "#6E6E6E",
        "number": "#A05000",
    },
    # VIC-II blue: the border light-blue for verbs, lime for literals, the
    # green the VIC-II actually had, and its brown where something went wrong.
    "commodore-64": {
        "url": "bold underline #C9C1FF",
        "env": "bold underline #B8C76F",
        "path": "underline #9B93E8",
        "command": "bold #C9C1FF",
        "flag": "#B8C76F",
        "tool": "bold #9AD284",
        "model": "bold #B8C76F",
        "verdict": "bold #C9C1FF",
        "outcome_good": "bold #9AD284",
        "outcome_bad": "bold #9A6759",
        "tag": "#6C5EB5",
        "number": "#B8C76F",
    },
    # EGA: grey body, cyan and yellow for the bright primaries, green/red the
    # way DOS used them.
    "edit-com": {
        "url": "bold underline #55FFFF",
        "env": "bold underline #FFFF55",
        "path": "underline #55FFFF",
        "command": "bold #FFFFFF",
        "flag": "#FFFF55",
        "tool": "bold #55FF55",
        "model": "bold #FFFF55",
        "verdict": "bold #FFFFFF",
        "outcome_good": "bold #55FF55",
        "outcome_bad": "bold #FF5555",
        "tag": "#00AAAA",
        "number": "#55FFFF",
    },
    # One hue. Verbs bold, locations underlined, literals at body brightness,
    # a bad outcome reversed -- the brightest thing a P3 tube could do.
    "amber": {
        "url": "bold underline #FFD9A0",
        "env": "bold underline #FFD9A0",
        "path": "underline #FFB000",
        "command": "bold #FFD9A0",
        "flag": "#FFD9A0",
        "tool": "bold #FFB000",
        "model": "bold #FFD9A0",
        "verdict": "bold #FFD9A0",
        "outcome_good": "bold #FFD9A0",
        "outcome_bad": "bold reverse #FFB000",
        "tag": "#8A5F00",
        "number": "#FFD9A0",
    },
    # One hue as well, and the same one exception matrix already makes for
    # errors: a bad outcome goes red, because it has to be seen.
    "matrix": {
        "url": "bold underline #7CFF9B",
        "env": "bold underline #7CFF9B",
        "path": "underline #00FF41",
        "command": "bold #7CFF9B",
        "flag": "#7CFF9B",
        "tool": "bold #00FF41",
        "model": "bold #7CFF9B",
        "verdict": "bold #7CFF9B",
        "outcome_good": "bold #7CFF9B",
        "outcome_bad": "bold #FF3B3B",
        "tag": "#008F11",
        "number": "#7CFF9B",
    },
}


# -- block bands -------------------------------------------------------------
#
# Reasoning and code arrive as *blocks* rather than as body text: they paint
# their own background (a place in the transcript, not a passage of it) and
# they can be folded away. The band each kind paints is a theme variable, so
# every theme owns its own idea of what thinking and code look like; a built-in
# theme has no such variable and falls back to no band at all, which is exactly
# how the transcript behaved before.

#: The block kinds that get a band and a fold. Order is the order `/fold`
#: reports them in.
BLOCK_KINDS: tuple[str, ...] = ("reasoning", "code")

#: Block kind -> the theme variable holding the background it paints.
BLOCK_BACKGROUND_VARS: dict[str, str] = {
    "reasoning": "reasoning-background",
    "code": "code-background",
}


def block_background(name: str, kind: str) -> str:
    """The colour ``kind`` blocks paint under theme ``name``, or ``""``.

    An empty string means *no band*: the block renders as ordinary body text.
    """
    variable = BLOCK_BACKGROUND_VARS.get(kind)
    theme = RETRO_THEMES.get(name or "")
    if not variable or theme is None:
        return ""
    value = theme.variables.get(variable)
    return value if isinstance(value, str) else ""


def block_emphasis(name: str, fallback: str = "bold") -> str:
    """The colour a block header is written in under theme ``name``.

    ``accent`` is the emphasis colour of a dark theme; on a light one it is a
    tint meant to sit on a dark bar and goes unreadable on a white page, so a
    light theme lends its ``primary`` instead.
    """
    theme = RETRO_THEMES.get(name or "")
    if theme is None:
        return fallback
    return theme.primary if not theme.dark else theme.accent


def register(app: "App[Any]") -> None:
    """Make every retro theme selectable on ``app``.

    Must run before anything sets ``app.theme``: Textual resolves a name
    against its built-ins plus what this app registered, and an unregistered
    name silently falls back to textual-dark.
    """
    for theme in RETRO_THEMES.values():
        app.register_theme(theme)


# -- persistence -------------------------------------------------------------


#: The app was "redtram" until 2026-10; state written then still migrates.
LEGACY_PREFIX = "redtram"


def _prefs_path() -> Path:
    return Path.home() / ".k0b0l-chat-ui.json"


def migrate_state(new: Path) -> Path:
    """Carry one legacy state file forward, once.

    The app's on-disk names changed with the rename; an operator's saved
    theme, history, sessions, endpoints and skills must not be orphaned by
    it. If the new file does not exist yet and the old one does, the old one
    is copied (never moved: nothing is destroyed if the copy fails) and the
    caller proceeds as if it had always been there. Lives here because
    themes has no package-internal imports, so every state-owning module
    can take it without a cycle.
    """
    if new.exists():
        return new
    old = new.with_name(new.name.replace(".k0b0l-", f".{LEGACY_PREFIX}-", 1))
    if old.is_file():
        try:
            new.write_bytes(old.read_bytes())
        except OSError:
            pass
    return new


def load_saved_theme(known: list[str]) -> str:
    """The theme the operator last chose, or ``""`` when there is not one.

    ``known`` is the caller's combined list, so a theme that no longer exists
    (a dropped built-in, an edit to the retro set) is ignored rather than
    applied blind.
    """
    try:
        raw = json.loads(migrate_state(_prefs_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    name = raw.get("theme") if isinstance(raw, dict) else None
    return name if isinstance(name, str) and name in known else ""


def save_theme(name: str) -> None:
    """Remember the chosen theme; failure to write is never fatal."""
    path = _prefs_path()
    payload: dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload = existing
    except (OSError, ValueError):
        pass
    payload["theme"] = name
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
