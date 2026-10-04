# Themes

A theme repaints the whole frame the chat lives in: menu bar, top and bottom
bars, borders, panels, and the syntax colours used for fenced code the model
prints. Two layers make that up — a Textual theme for the chrome, and a paired
pygments style for the code — so a theme cannot leave the transcript looking
like it came from somewhere else.

## Switching

```
/theme                 # list them, and show the current one
/theme amber           # switch (unambiguous prefixes work: /theme comm)
```

The choice is remembered in `~/.k0b0l-chat-ui.json` and restored on the next
start. The Textual app's **View → Options…** pane does the same thing with the
arrow keys, if you would rather pick from a list.

## What ships

| Name | Look |
| --- | --- |
| `textual-dark` | The default |
| `textual-light` | — |
| `nord` | — |
| `gruvbox` | — |
| `catppuccin-mocha` | — |
| `monokai` | — |
| `solarized-dark` | — |
| `tokyo-night` | — |
| `hotdog-3x` | Hot Dog Stand: black on yellow, the 3.x desktop's loudest theme |
| `beos` | BeOS: steel-blue panels, the yellow tab accent |
| `commodore-64` | The 64's blue-on-blue BASIC screen |
| `edit-com` | The MS-DOS editor: grey on blue, cyan menu bar |
| `amber` | Monochrome amber phosphor |
| `matrix` | Monochrome green phosphor |

The last six are for working in a room where nobody will mistake the terminal
for a chat app.

## Adding one

`rt_harness/themes.py` holds them. A retro theme is a `textual.theme.Theme`
registered under a name, plus an entry in `RETRO_SYNTAX` giving the pygments
style its code blocks use:

```python
RETRO_THEMES: dict[str, Theme] = {
    "amber": Theme(name="amber", primary=..., background=..., ...),
}
```

Registration happens in `ChatApp.__init__`, *before* anything can set
`self.theme`: Textual silently falls back to `textual-dark` for a name it does
not know, so a theme registered late looks like a theme that does not exist.
Adding a name to `RETRO_THEME_NAMES` (and to `THEMES` in `textual_chat.py`) is
what puts it in `/theme` and in the menu.

## Semantic colour

A theme carries a second table: `SEMANTIC_ROLES[<name>]`, which says what
colour each kind of *data* gets in the transcript — paths, commands, flags,
model tags, verdicts, outcomes, counts. It is keyed by the same names as
`RETRO_THEMES`, so a new theme needs entries there too, and every colour in it
should come from that theme's own palette (`test_semantic.py` checks). The
categories and the rules that recognise them live in `semantic.py`; the choice
of colours lives here, with the rest of the palette. See `docs/semantic.md`.
