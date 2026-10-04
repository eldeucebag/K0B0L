# Chat front ends

The interactive session: a transcript, a prompt, and whichever front end your
terminal can carry. Start it with `thinlizzy.py --chat`.

## Front ends

| Front end | When you get it | Force it |
| --- | --- | --- |
| Textual app | `textual` and `rich` are installed | — |
| Rich line mode | `rich` is installed, `textual` is not | `--chat-plain` |
| Plain line mode | neither is installed | — |

All three are the same session underneath: one `ChatSession`, one transcript, one
set of commands. The Textual app adds the menu bar, the modal panes (help, paste,
file picker, run wizard), the command palette (ctrl-p), and themes.

## A turn

Your line goes to the chat model with the current system message and history.
If the model calls a tool, the call runs under the workspace root, the result is
appended, and the model continues — up to `CHAT_MAX_TOOL_ROUNDS` (8) rounds.
Long turns are normal: the timeout is an hour, and the status line shows tokens
and elapsed time as they arrive.

## Tools

- File tools (`read_file`, `list_files`, `search_files`, `write_file`,
  `edit_file`) are confined to the workspace root. A path that escapes it is
  refused, so a hallucinated path cannot reach the rest of the disk.
- Info tools answer questions about the harness itself. See `docs/tools`.
- `--no-chat-tools` starts with no tools at all; `/tools` controls how much of a
  result you see.

Two protocols are supported. `native` sends the tool schemas to the server and
relies on the chat template's tool branch; `text` spells the protocol out in the
system message instead. The default is `auto`: ask the server which it can do
and fall back to `text`. Set it with `--chat-protocol` or `CHAT_PROTOCOL`.

## What is in the system message

In order:

1. the soul file (`docs/soul`),
2. your system text — `--system-file` / `CHAT_SYSTEM_FILE` if set, otherwise the
   built-in coding-assistant default,
3. the working directory,
4. any enabled skills,
5. the docs index (topics only — the model is told to fetch a document before
   answering a capability question),
6. the tool clause, describing the tools it actually has.

## Sessions

`/session save <name>` writes the conversation to `~/.k0b0l-sessions/<name>.json`
and `/session load <name>` reads it back; `/session list` shows what is saved.
Saving does not snapshot the workspace, only the messages. Skills live in
`<root>/skills` and `~/.k0b0l-skills`; `/skills` turns them on and off.

History is trimmed at `CHAT_HISTORY_TURNS` (40) turns so a long session does not
grow past the context it was given; `/history` shows where you are, and `/clear`
starts over.
