# Keyboard reference

The Textual app is keyboard-first: nothing needs the mouse, including the menu
bar.

## Opening the menu

Press **esc**. The menu bar takes focus and opens its first menu, so the arrow
keys work immediately — no click required. Escape again closes it and puts the
cursor back in the prompt.

You can also click a menu label to open that menu directly.

| Key | Does |
| --- | --- |
| `esc` | Open the menu bar, or close it if it is already open |
| `←` / `→` | Move between menus on the bar |
| `↑` / `↓` | Move through the open menu's items, including submenus |
| `enter` | Pick the highlighted item |
| `esc` | Close the open menu (and any modal) |

The bar prints its own legend on the right, so what it offers is visible without
opening anything: `←/→ menus · ↑/↓ items · Enter pick · Esc close`.

## Everywhere else

| Key | Does |
| --- | --- |
| `enter` | Send the line in the prompt |
| `↑` / `↓` | Walk the input history (in the prompt) |
| `f1` | Help panel — commands and keys in one screen |
| `f2` | Paste pad, for multi-line input; F2 sends it, Esc cancels |
| `f3` | File picker, for choosing an artifact |
| `f4` | Run wizard — the fields of a `/run start`, one at a time |
| `ctrl-p` | Command palette: every command and menu action, filterable |
| `ctrl-q` | Quit (closes any open menu first) |

Typing `/` opens the command suggestor; an unambiguous prefix is accepted
(`/th` is `/think`).

## Menu bar contents

| Menu | Items |
| --- | --- |
| **Session** | Model, Target, Clear conversation, History, Options… |
| **Run** | Start run (wizard), Status, Tail log, Stop |
| **View** | Tool output (off/summary/full), Skills, Theme, Toggle reasoning |
| **Help** | Help panel, Product docs, Soul file, Quit |

## Line modes

The rich and plain front ends have no menu bar; they read the same commands at
the prompt. `ctrl-d` exits either of them.
