# Tests

The chat subsystem's suite. Run everything with `./run_all.sh`, or only the part
that needs no model with `./run_all.sh --quick`.

| file | checks | needs a model | covers |
|---|---|---|---|
| `test_tools.py` | 58 | no | the confined `Workspace`: reads, edits, search, escapes, the text tool protocol |
| `test_verdict.py` | 22 | no | the refusal verdict, including against every logged run's real target output |
| `test_recovery.py` | 18 | no | recovery from a malformed tool call, and that a failed turn is never silent |
| `test_engine.py` | 35 | yes | `ChatSession` against the real attacker: the tool loop, hooks, history, reset |
| `test_frontend.py` | 17 | yes | `PlainChat` and the CLI — **that tool output lands in the chat, not the shell** |
| `test_pty.py` | 10 | yes | the rich front end under a real pty: banner, streaming, panels, repaint |

Scratch files are written under `.scratch/` beside these tests, never `/tmp`, and
are recreated on each run — so a checkout is self-contained and repeatable.

## Prerequisites

`test_tools.py` and `test_verdict.py` are pure standard library and always run. The
other three need:

- Ollama reachable at `CHAT_TEST_URL` (default `http://127.0.0.1:11436`) — the
  dedicated flash-attention + q4_0 instance started by `../rt-fa-server.sh`, not
  the system service on `:11434`.
- The attacker model, `CHAT_TEST_MODEL` (default
  `hf.co/mradermacher/Gemma-4-E4B-Abliterated-Uncensored-i1-GGUF:i1_Q4_K_M`).

```bash
./run_all.sh --quick                 # both suites that need no model
../rt-fa-server.sh status            # is :11436 up?
CHAT_TEST_URL=http://127.0.0.1:11434 ./run_all.sh
```

`test_pty.py` needs a pty; it drives `thinlizzy.py --chat` in a child process with
its own terminal, so it does not care what the calling shell is.

## What these tests are guarding

- **Tool output belongs in the chat.** `test_frontend.py` captures the process's
  real stdout and asserts it stays empty of tool output; the transcript is where
  a result is supposed to appear.
- **A blank reply must be explained.** The abliterated attacker reasons in a
  hidden field, so on a small `num_predict` it can burn the whole budget thinking
  and emit no visible prose. The suite asserts *reply or explanation*, never
  silence — asserting "a reply exists" at a tiny budget is flaky, not a finding.
- **The live region repaints rather than appends.** `test_pty.py` checks the raw
  stream for cursor-up escapes; without them a real terminal would fill with
  duplicated frames.

## Notes

- Each suite prints `ok` / `FAIL` per check and exits non-zero on any failure, so
  it can be wired into CI unchanged.
- `test_pty.py` leaves its capture under `.scratch/pty_chat_capture.txt` (ANSI
  stripped, readable) and `.scratch/pty_chat_raw.txt` (raw escapes).
- Two concurrent runs must not share an output file: a tally you cannot attribute
  to a revision is worthless. `run_all.sh` gives every suite its own temp file.
- Shell-init noise from this box's `conda` / `nvm` setup can appear on stderr
  when a suite runs under a login shell. It is not a test failure.
