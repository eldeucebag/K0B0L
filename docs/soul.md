# SOUL.md

`SOUL.md` is the assistant's standing text: who it is and how it works, as
opposed to what this session wants. It is loaded automatically, and it is the
first thing in the system message — before the operator's prompt, the working
directory, the skills, and the docs index.

## What it is not

| Not | Because |
| --- | --- |
| The operator's system prompt | That is `CHAT_SYSTEM_FILE` / `SYSTEM_PROMPT_FILE`, and it goes in after the soul |
| A skill | Skills are opt-in per session (`/skills`), the soul is on unless you turn it off |
| Config | Nothing about models, endpoints, or paths belongs in it |

## Where it is looked for

First hit wins:

1. `CHAT_SOUL_FILE`, or `--soul-file PATH` — explicit
2. `SOUL.md` (or `soul.md`) in the chat's root — `CHAT_ROOT`, so a project can
   carry its own
3. `SOUL.md` next to the package — the shipped default

A file that exists but is empty counts as no soul. A missing file is not an
error: the session simply runs without one, and says so.

## Turning it off

```
--no-soul                 # one session
CHAT_SOUL=0               # this environment
```

Both are reported by `/soul`, so you can always tell which soul is in force:

```
/soul                     # the path, the size, and the text
```

## Writing one

Plain markdown prose. Short and specific beats long and generic — the same
advice as for any system prompt, with one difference: this text is present in
*every* session, so anything that would only apply to redteam work belongs in a
skill instead. Say what the assistant should sound like and what it should
refuse to do quietly, not what it should know.
