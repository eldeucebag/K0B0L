# Deployments and stock system prompts

By default the target runs bare: only the candidate prompt and the objective
reach it. A *deployment* mounts it under the stock system prompt its model
family ships instead, catalogued from `elder-plinius/CL4R1T4S`, so the cycle is
testing the framing a real deployment would present rather than a checkpoint in
a vacuum.

## Choosing one

```bash
python3 thinlizzy.py --cycle --objective "..." --target-family llama3
python3 thinlizzy.py --cycle --objective "..." --target-family llama3/strict
python3 thinlizzy.py --cycle --objective "..." --system-prompt-file ./mine.txt
```

| Flag | Effect |
| --- | --- |
| `--target-family FAMILY[/VARIANT]` | Use this family's newest stock prompt, or a named variant |
| `--system-prompt-file PATH` | Use this file instead of a family |
| `--system-prompt-chars N` | Characters of the prompt shown to the planner and attacker; `0` shows all |
| `--no-fetch-prompts` | Never download; use only the local cache |
| `--list-families` | Print the catalogued families and exit |
| `--sync-prompts [FAMILY]` | Cache the catalogued prompts — all of them, or one family — and exit |

Env equivalents: `TARGET_FAMILY`, `SYSTEM_PROMPT_FILE`, `SYSTEM_PROMPT_CHARS`,
`FETCH_SYSTEM_PROMPTS`, `SYSTEM_PROMPTS_DIR`.

From the chat, `family=` on a `/run start` line does the same thing for that
run. `/docs modes` covers the artifact side of a run.

## The cache

Prompts are cached under `SYSTEM_PROMPTS_DIR` (`system-prompts/` in the working
directory by default). The first use of a family fetches it; `--no-fetch-prompts`
narrows that to the cache only, and `--sync-prompts` fills the cache up front —
useful on a machine that will not have network access during a run.

Only the excerpt the planner sees is bounded by `--system-prompt-chars`; the
target always receives the prompt whole, because a truncated framing would be a
different framing.

## Why the planner is told about it

The strategist needs to know what framing the target is answering under —
otherwise it writes candidates against a bare checkpoint and reads the refusal
as a seam. The excerpt, plus the label of what was mounted, goes into the
planner's context; the size check on startup warns when the excerpt would eat
too much of the planner's window.

## When nothing is mounted

With neither a family nor a file, the run is unchanged from the bare-target
case, and nothing is downloaded. The resolved deployment, if any, is printed in
the startup config block and archived per stage as `<slot>.system.txt` (see
`docs/runs`).
