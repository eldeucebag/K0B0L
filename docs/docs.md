# The docs directory

This directory. It exists so the model can answer questions about the harness
from the shipped documentation instead of from what it can infer about the
source — and so the answer you get in the chat is the same answer that is on
disk.

## How a session reads them

1. The system message carries the **topic index**: each document's name, title,
   and summary line. Not the contents — the index.
2. `read_docs(name)` returns the document verbatim. That is the info tool behind
   every capability question; the model is told to use it rather than to answer
   from the index alone.
3. You can read the same file with `/docs <topic>`, or list every topic with
   `/docs`.

The index only appears when the model actually has tools, so a tool-less session
is not told about documents it cannot fetch.

## Adding a document

Drop a markdown file in here. The filename is the topic name — `runs.md` is the
topic `runs`, and `read_docs` accepts a unique prefix (`ru`) or the exact title
as well:

- Give it one `# H1` — that is the title.
- Make the first paragraph a single sentence — that is the summary in the index.
- Keep it about *what the harness does*, with the exact command names and env
  vars in it. Prose that has drifted from the code is worse than no prose.

A different directory can be used with `CHAT_DOCS_DIR` / `--docs-dir`, and
`read_docs` follows it, so a fork can ship its own set.

## Related

| Document | Covers |
| --- | --- |
| `docs/commands` | Every slash command and the CLI equivalents |
| `docs/keys` | Menu bar, Esc, function keys |
| `docs/config` | Env vars and flags |
| `docs/soul` | `SOUL.md`, and how to disable it |

`docs/README.md` is the index for a human reader; it is not itself a topic.
