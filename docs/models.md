# Models and endpoints

K0B0L talks to one OpenAI-compatible endpoint for everything. It is Ollama by
default — `OLLAMA_URL`, default `http://127.0.0.1:11434` — but any `/v1`
endpoint works.

## Roles

Each role has its own model and its own context/output budget, because the jobs
are different sizes:

| Role | Job |
| --- | --- |
| `STRATEGIST` | Reads the artifact and plans: which mode to run, and why |
| `ATTACKER` | Writes the candidate that goes to the target (and defaults the chat model) |
| `TARGET` | The model the candidates are sent to and scored against |
| `WRITEUP` | Turns a win into the technical write-up |

Each is configured by `<ROLE>_MODEL` and `<ROLE>_NUM_CTX` / `<ROLE>_NUM_PREDICT`
/ `<ROLE>_THINK`. The defaults live in `rt_harness/config.py`, chosen for an
8 GB card: an abliterated small model for the attacker and target, a reasoning
distill for the strategist. The list worth trusting is the endpoint's own — the
`list_models` info tool, and the model picker in the app (Session → Model), read
`/api/tags` — so a tag that has not been pulled never gets chosen.

## The chat model

`/model` shows and switches it; `CHAT_MODEL` / `--chat-model` set it at startup.
It defaults to the attacker model: the role that has to hold a conversation
cheaply.

`/target` sets the model the *next run* sends candidates to; it does not change
who answers you. `/target` with no argument prints the current one.

## Endpoints and keys

Ollama-shaped endpoints (`/api/...`) use the native client; endpoints ending in
`/v1` use the chat-completions client with an `Authorization` header, if a key
is on file.

Saved endpoints live in `~/.k0b0l-apis.json` — a JSON array of either URL
strings or `{"base_url": ..., "api_key": ...}` objects:

```json
[
  "http://127.0.0.1:11434",
  {"base_url": "http://127.0.0.1:11436/v1", "api_key": "local"}
]
```

In the Textual app, **View → Options…** switches between saved endpoints and
adds the current one to the file. The URL in the file is matched exactly (after
stripping a trailing slash), so a key only applies to the endpoint it was saved
against.

## Probing

The harness asks the server two things at startup: which models exist, and
whether the chat template has a tool branch (that decides `native` vs `text`
tool protocol — see `docs/chat`). Both probes are short-timeout and non-fatal:
an unreachable endpoint reports the error to the model instead of killing the
session.
