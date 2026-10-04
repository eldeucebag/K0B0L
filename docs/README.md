# K0B0L docs

Capability documentation for the harness, written to be read by the model as
much as by you. The chat session gets a topic index in its system message and
can fetch any of these with the `read_docs` tool; you can read one with
`/docs <topic>`.

| Topic | What it covers |
| --- | --- |
| `commands` | Every slash command, and the CLI flags that mirror them |
| `chat` | The three front ends, tools, sessions, and how a turn works |
| `runs` | The attack cycle: starting, watching, stopping, and what lands on disk |
| `modes` | The seven attack modes and the idea behind each |
| `deployments` | Mounting a target under a model family's stock system prompt |
| `models` | Roles, which model answers the chat, and pointing at an endpoint |
| `themes` | The UI themes, switching them, and adding one |
| `semantic` | Colouring data in the transcript: what gets coloured, and in whose palette |
| `keys` | Keyboard and menu bar |
| `config` | Environment variables and CLI flags, in one place |
| `soul` | `SOUL.md`: the standing identity file |
| `docs` | This directory: how the model reads it, how to add a topic |
| `tools` | The file and info tools the model may call |
| `tests` | Running the suites |

Adding a topic: drop a markdown file in this directory whose first line is an
`# H1` title and whose first paragraph is a one-line summary. That pair becomes
its index line; nothing else needs registering.
