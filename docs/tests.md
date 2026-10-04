# Test suites

Every suite is a plain script under `tests/`: no pytest, no fixtures to
discover, and each prints `ok` lines and exits non-zero if any check failed.

```
bash tests/run_all.sh           # every suite in the list below
bash tests/run_all.sh --quick   # the model-free suites only
python3 tests/test_docs.py      # one suite, with its per-check output
```

## What each suite covers

| Suite | What it holds down |
| --- | --- |
| `test_tools.py` | Tool schemas, the file tools, and the workspace bound |
| `test_verdict.py` | Reading a target's answer as complied / partial / refused |
| `test_recovery.py` | Recovering a candidate from a malformed model reply |
| `test_runctl.py` | Run control: start, status, tail, stop, and the log files |
| `test_cycle.py` | The attack cycle end to end, on a scripted client |
| `test_engine.py` | Prompt assembly and the sampling options |
| `test_frontend.py` | The chat front end's commands and stream handling |
| `test_pty.py`, `test_fspty.py` | The line TUI driven in a real pty |
| `test_sessions.py` | Saving and loading a chat session |
| `test_skills.py` | Loading a skill from the root and from the home directory |
| `test_openai_compat.py` | The `/v1` endpoint path and its request shape |
| `test_soul.py` | The soul lookup order, and that it reaches the system message |
| `test_docs.py` | The docs index, `read_docs`, and that the docs do not lie |
| `test_themes.py` | Every theme resolves, registers, and covers its variables |
| `test_semantic.py` | The data rules claim the data, and the colour stays in the theme's palette |
| `test_menubar.py` | The bar shows every menu, and Esc takes the keys |

Two of these are worth knowing about when you change something:
`test_docs.py` fails if a slash command or an environment variable named in the
docs does not exist in the code, and `test_menubar.py` fails if the menu bar
stops laying its labels out on one painted row — it checks the screenshot, not
just the widget geometry.

## Needing a model

`test_engine.py`, `test_frontend.py`, `test_tools.py`, `test_pty.py`,
`test_fspty.py`, `test_fslive.py`, and `test_textual_live.py` call a real
server. They read `CHAT_TEST_URL` (default `http://127.0.0.1:11436`, the second
Ollama instance `rt-fa-server.sh` runs for the red-team loop, with flash
attention and a quantized KV cache) and `CHAT_TEST_MODEL` (default the attacker
model), and they fail — rather than skip — when nothing answers. Run
`run_all.sh --quick` on a machine with no server, or point them at one:

```
CHAT_TEST_URL=http://127.0.0.1:11434 CHAT_TEST_MODEL=qwen3:8b python3 tests/test_engine.py
```

## Slow, run by hand

`test_themepty.py`, `test_textualpty.py`, and `test_textual_live.py` boot the
real app in a pty and read the escape stream back. They take tens of seconds
each and are not part of `run_all.sh`; run one directly when you touch
rendering:

```
timeout 300 python3 tests/test_themepty.py
```

## Scratch

`tests/.scratch/` is for throwaway probes. Nothing there is a test, and nothing
outside it may import from it.
