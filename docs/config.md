# Configuration

Precedence is: command-line flag, then environment variable, then the default in
`rt_harness/config.py`. A flag that is not given does not touch the value; that
is why the CLI only ever *replaces* fields it was handed.

## Endpoint and models

| Env var | Meaning |
| --- | --- |
| `OLLAMA_URL` | Endpoint for everything. Default `http://127.0.0.1:11434` |
| `ATTACKER_MODEL` | Candidate writer; also the chat model's default |
| `STRATEGIST_MODEL` | Planner: reads the artifact, picks the mode |
| `TARGET_MODEL` | Default model for a run's target |
| `WRITEUP_MODEL` | Writes the technical write-up on a win |
| `ATTACKER_THINK`, `STRATEGIST_THINK`, `TARGET_THINK` | Reasoning tokens per role |
| `SKIP_PULL` | Do not check that the models exist before starting |

## The run

| Env var | Meaning |
| --- | --- |
| `BASE_PROMPT_FILE` | The artifact |
| `ATTACK_MODES` | Comma-separated modes for every run in this session |
| `ATTACK_SKILLS` | Skills injected into the attacker's prompt |
| `STRATEGIST_MODE`, `STRATEGIST_INCLUDE_BASE` | How the planner is briefed |
| `TARGET_FAMILY`, `SYSTEM_PROMPT_FILE` | Deployment framing (see `docs/deployments`) |
| `SYSTEM_PROMPTS_DIR`, `FETCH_SYSTEM_PROMPTS` | Where cached prompts live, and whether fetching is allowed |
| `SYSTEM_PROMPT_CHARS` | Characters of the stock prompt shown to the planner; `0` = all |
| `LOGDIR` | Where logs, writeups, and winners land. Default `redteam-logs/` |
| `WRITEUP` | Whether a win gets a write-up |
| `SHOW_THINKING` | Print reasoning tokens to the console |
| `NUM_GPU`, `KEEP_ALIVE` | How many layers to offload, how long Ollama keeps the model loaded |
| `PROBE_SCOPE_GUARD` | The startup probe that checks a model can hold a scope |

## The chat

| Env var | Meaning | Default |
| --- | --- | --- |
| `CHAT_MODEL` | Which model answers you | the attacker model |
| `CHAT_ROOT` | Workspace root the file tools are confined to | the working directory |
|| `CHAT_NUM_CTX`, `CHAT_NUM_PREDICT` | Context and output budget | `65536`, `2048` ||
| `CHAT_TEMPERATURE`, `CHAT_TOP_P`, `CHAT_TOP_K`, `CHAT_REPEAT_PENALTY` | Sampling | — |
| `CHAT_TOOLS` | Attach the file tools | on |
| `CHAT_TOOL_OUTPUT` | `off` \| `summary` \| `full` | `summary` |
| `CHAT_PROTOCOL` | `auto` \| `native` \| `text` | `auto` |
| `CHAT_MAX_TOOL_ROUNDS` | Tool rounds per turn | `8` |
| `CHAT_HISTORY_TURNS` | Turns kept before trimming | `40` |
| `CHAT_THINK` | Show reasoning tokens | model default |
| `CHAT_SYSTEM_FILE` | Operator system prompt (falls back to `SYSTEM_PROMPT_FILE`) | built-in default |
| `CHAT_SOUL` | Load `SOUL.md` | on |
| `CHAT_SOUL_FILE` | Explicit soul file | — |
| `CHAT_DOCS_DIR` | Directory the docs index and `read_docs` read | the shipped `docs/` |
| `CHAT_SEMANTIC` | Colour data in the transcript | on |

## Flags

`python3 thinlizzy.py --help` is the complete list. The chat is the default
(bare `thinlizzy.py` opens it; `--no-chat` or `K0B0L_NO_CHAT=1` runs the loop).
The chat flags are grouped under `chat` and mirror the env vars above:

```
--chat                --chat-model <name>   --chat-root <path>
--chat-tool-output    --chat-protocol       --no-chat-tools
--no-chat             --no-soul             --soul-file <path>
--docs-dir <path>     --no-semantic
--chat-plain          --chat-classic
```

The deployment flags (`--target-family`, `--system-prompt-file`,
`--system-prompt-chars`, `--no-fetch-prompts`, `--list-families`,
`--sync-prompts`) are described in `docs/deployments`.
