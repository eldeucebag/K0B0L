# Slash commands

Everything the chat accepts when you type a line starting with `/`. Commands
are handled by the front end, not by the model, and an unambiguous prefix works
(`/th` for `/think`).

| Command | What it does |
| --- | --- |
| `/help` | The command list |
| `/tools off\|summary\|full` | How much of a tool result is rendered into the transcript |
| `/think on\|off` | Show or hide the model's reasoning tokens |
| `/reason on\|off\|auto` | Reasoning GENERATION: off asks for no reasoning tokens at all (auto leaves the model's default) — display stays `/think` |
| `/imagetool on\|off` | Whether the MODEL may generate images: off withholds `generate_image` entirely (schema, protocol, refusal guard) so image prompts can be built and refined in prose without the model firing a GPU swap — the 🎨 dialog still works for you |
| `/read <path>` | Print a workspace file into the chat |
| `/docs [topic]` | List the product docs, or print one |
| `/core` | Show the CORE.md this session loaded, and its path (alias: `/soul`) |
| `/skills list\|enable\|disable <name>` | Manage skills: list, or pin/unpin one into every turn |
| `/skills show <name>` | Print a skill's body (and where it lives) |
| `/skills new [name]` | Create a new skill from the template, under the workspace `skills/` dir |
| `/skills edit <name>` | Print the raw SKILL.md (frontmatter + body) with its path; the Textual front end opens its modal editor instead (see `docs/skills`) |
| `/skills delete <name>` | Delete a skill (click-to-confirm in the web panel; removes the file and unpins it) |
| `/compact` | Compress the conversation into one summary and continue from it — durable facts are written to the memory store, and earlier memories are recalled into the compacted context |
| `/image [model] <prompt>` | Generate an image locally (pony\|qwen\|chroma — see `docs/imagegen`); the model has the same `generate_image` tool |
| `/session save\|load\|list <name>` | Persist or resume a conversation |
| `/run start [key=value ...] <objective>` | Spawn a cycle run |
| `/run status\|tail [N]\|stop` | Watch or stop the current run |
| `/paste` | Read a multi-line block; finish with a line containing only `.` |
| `/model [name]` | Show or switch the chat model |
| `/target [name]` | Show or set the model the next run targets |
| `/root [path]` | Show or change the workspace root (starts a fresh session) |
| `/theme [name]` | List or switch the UI theme |
| `/semantic [on\|off]` | Colour data in the transcript, or report the setting |
| `/clear` | Forget the conversation, keeping the system message |
| `/history` | Messages held, turns taken, tool calls run |
| `/exit` | Leave (ctrl-d and ctrl-q work too) |

## `/run start` keys

`key=value` tokens are lifted out of the line; whatever is left is the
objective.

| Key | Meaning |
| --- | --- |
| `target=` | The model the target role runs (same as `/target`) |
| `modes=` | Comma-separated attack modes, e.g. `modes=seam,escalation` |
| `attempts=` | Cap on candidates for this run |
| `family=` | Mount the target under this deployment family |
| `ofile=` | Read the objective from a file, appended to any inline text |
| `skills=` | Comma-separated skills to inject into this run |

## CLI equivalents

Every chat command has a shell-side counterpart, and the chat is what a bare
`thinlizzy.py` opens (`--no-chat` or `K0B0L_NO_CHAT=1` runs the loop instead):

```bash
python3 thinlizzy.py                              # this session (the default)
python3 thinlizzy.py --no-soul                    # without CORE.md
python3 thinlizzy.py --cycle --objective "..."    # the attack loop, unattended
python3 thinlizzy.py --list-modes                 # modes and one-liners
python3 thinlizzy.py --list-families              # deployment families
python3 thinlizzy.py --help                        # every flag
```
