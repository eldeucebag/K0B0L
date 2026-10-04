# Semantic colour

The transcript colours *data* — paths, commands, flags, env vars, model tags,
endpoints, verdicts, outcomes, counts — using the active theme's own palette.

Fenced code blocks have always gone through pygments. Prose did not, and most
prose in a run is data. `rt_harness/semantic.py` recognises the kinds of datum
and colours each one, so a line reads at a glance instead of as soup:

```
+ read_file /home/jeff/redtram/docs/semantic.md -> ok in 0.4s
                                ^ path          ^ outcome  ^ number
```

## What gets coloured

| Kind | Looks like | Why it is worth a colour |
| --- | --- | --- |
| `path` | `/home/jeff/redtram/rt_harness/semantic.py`, `runctl.py:155` | the file a line is about |
| `command` | `/run start`, `/docs runs` | what was asked of the harness |
| `flag` | `--no-semantic`, `-x` | how it was asked |
| `env` | `CHAT_SEMANTIC=0`, `$HOME` | the environment in play |
| `url` | `http://127.0.0.1:11436`, `localhost:11434` | which endpoint answered |
| `model` | `qwen3:8b`, `hf.co/…:Q4_K_M` | who answered |
| `tool` | `read_file`, `edit_file`, `read_docs` | what the model reached for |
| `verdict` | `refused`, `complied`, `partial` | what a run came back with |
| `outcome_good` | `PASS`, `all suites passed` | what worked |
| `outcome_bad` | `FAIL`, `Traceback`, `exit=1` | what broke |
| `tag` | `[model]`, `[run]` | the harness's own line labels |
| `number` | `425`, `12s`, `4.5 KiB` | counts, durations, sizes |

Rules are ordered most-specific-first and each span is claimed once: a URL's
`host:port` is a URL and not a model tag, `run-1.log:44` is one path and not a
path plus a number. Clock times (`12:34:56`) are deliberately left alone — a
time is not a datum anyone scans for.

## The colour is the theme's

Each theme carries its own role table (`themes.SEMANTIC_ROLES`), so switching
theme recolours meaning, not just the frame:

| Theme | How it separates one kind of datum from another |
| --- | --- |
| `hotdog-3x` | yellow ⇄ orange ⇄ gold, error red as a chip |
| `beos` | blue verbs, teal paths, brown literals, green outcomes |
| `commodore-64` | lime literals, lavender verbs, pale blue locations |
| `edit-com` | cyan locations, yellow literals, green/red outcomes |
| `amber` | one hue: bold for verbs, underline for locations, dim for labels |
| `matrix` | one hue, same scheme, red reserved for failures |

The mono themes have a single hue, so they differentiate by intensity and
attribute rather than importing a colour the machine never had. No theme's
table uses a colour outside that theme's palette, and `tests/test_semantic.py`
asserts exactly that.

Themes other than the six above (the stock Textual ones) fall back to
`themes.DEFAULT_SEMANTIC_ROLES`, which uses named ANSI colours.

Lines already in the transcript keep the theme they arrived under — the same
rule as code blocks. Scroll back after a theme switch and you are reading a
history of your theming, not a bug.

## Turning it off

| Where | How |
| --- | --- |
| In the session | `/semantic off` and `/semantic on` |
| Menu bar | View → Data colouring |
| A single session | `--no-semantic` |
| Every session | `CHAT_SEMANTIC=0` |

`/semantic` on its own reports the state, the theme in force, and how many
kinds of datum that theme currently colours.

## Adding a kind of datum

Add a `(category, pattern)` pair to `semantic.RULES` in matched order, put the
category in `semantic.CATEGORIES`, and give it a style in each theme's table in
`themes.SEMANTIC_ROLES` (plus `DEFAULT_SEMANTIC_ROLES`). `tests/test_semantic.py`
will tell you if a table is missing a category or has wandered off its palette.
