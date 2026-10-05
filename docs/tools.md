# Tools

What the model can reach from the chat. There are two families, and only one of
them changes anything on disk.

## File tools

Confined to the chat's root (`CHAT_ROOT`, default the working directory): a path
outside it is refused, not resolved. Nothing here runs a shell — that rule is
the reason the harness can hand tools to an abliterated model at all.

| Tool | Does |
| --- | --- |
| `list_files(path, recursive)` | List a directory, optionally its subtree |
| `read_file(path, start, limit)` | Read a file, with a line window |
| `search_files(pattern, path, glob, limit)` | Regex search inside files |
| `write_file(path, content)` | Write a whole file |
| `edit_file(path, old, new, replace_all)` | Replace text in a file |
| `run_script(path, args, interpreter, cwd, stdin, timeout)` | Run a script that already exists in the workspace |
| `ask_user_choice(prompt, options, allow_multiple, default_selected)` | Ask the operator to pick from a list; a palette strip opens above the input bar |
| `remember(body, domain, importance)` | Write one durable fact to the model's long-term memory (see `docs/memory`) |
| `recall(query, domain, limit)` | Search that memory; results carry ids and the model and date that wrote them |
| `connect_memories(from_id, to_id, why)` | Link two memories as one subject — an explicit graph edge |
| `expand_memory(memory_id, hops, limit)` | Walk the memory graph from one memory, nearest first |
| `load_skill(name)` | Open one installed skill's full procedure, prerequisites first (see `docs/skills`) |

## Info tools

Answers about the harness itself, all read-only: `harness_help`, `list_models`,
`list_modes`, `list_deployments`, `list_sessions`, and `read_docs`.

`read_docs(name)` returns one of these documents verbatim, which is what makes
"what can you do?" answerable from the shipped docs instead of a guess. The
topic list is in the session's system message; `/docs` shows the same list to
you.

## Tool protocol

| `CHAT_PROTOCOL` | Behaviour |
| --- | --- |
| `auto` (default) | Ask the server whether the template has a tool branch; use `native` if it does, `text` if not |
| `native` | Tool calls in the API's own format |
| `text` | The model writes a fenced JSON object — `{"name": ..., "arguments": {...}}` — and the harness parses it out of the reply |

In `text` mode a call is only accepted if it names a real tool with the
arguments that tool takes; a near-miss stays in the transcript as prose, so you
can see what the model actually wrote. Reasoning models that emit
`<tool_call>{...}</tool_call>` are understood too — that spelling is common
enough that refusing it would just cost turns.

## How much you see

`/tools off|summary|full` decides how tool traffic appears in the transcript:

| Level | Shows |
| --- | --- |
| `off` | Nothing but the model's prose |
| `summary` (default) | One line per call: tool, key argument, result size |
| `full` | Arguments and result text |

## Interactive user choices

`ask_user_choice(prompt, options, allow_multiple, default_selected)` blocks the
model's turn and shows a picker above the input bar where the operator makes
a selection. Arrow keys navigate, Space toggles (for multi-select), Enter
confirms, Esc cancels. The chosen option(s) are returned to the model as a
JSON list.

```bash
# Example model use (text protocol)
```tool
{"name": "ask_user_choice", "arguments": {
  "prompt": "Which attack mode should run next?",
  "options": ["seam", "graft", "vocabulary", "inversion"],
  "allow_multiple": true,
  "default_selected": ["seam"]
}}
```
```

This is useful when the cycle needs a human decision between identified
branching paths — e.g., which escalation mode to try after a `partial` verdict,
or which target model to switch to mid-run. The picker opens as a bounded
palette strip docked above the input bar (not a full-screen takeover): the
chat stays visible behind the dimmed screen. It only works in the Textual chat
(`--chat` or the default TTY front end) and requires `CHAT_ALLOW_EXEC=1`
(the same switch that enables `run_script`).
