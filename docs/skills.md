# Skills

Skills are on-demand procedures: a directory with a `SKILL.md`, portable
across any framework that reads the format (the agentskills.io convention).
The chat loads them from two places, workspace before profile:

- `<root>/skills/` — this project's skills
- `~/.k0b0l-skills/` — your global skills

A workspace skill shadows a profile skill of the same name, first match
wins: a repo can carry its own version of a global skill without editing
the global file.

## The format

```
skills/
└── tdd/
    ├── SKILL.md          # required
    └── pytest-cheatsheet.md   # optional resources, read_file-able
```

The `SKILL.md` is YAML frontmatter plus a Markdown body:

```markdown
---
name: tdd
description: Test-driven development workflow.
trigger:
  keywords: ["tdd", "write tests"]
---

# Test-Driven Development

1. Write a failing test that captures the requirement.
2. Run it. Confirm it fails for the right reason.
...
```

`name` and `description` are the required frontmatter; `trigger` is
optional. A bare `.md` file (the old format) still works — heading title,
no frontmatter, no trigger.

## Progressive disclosure

The system message carries **one line per skill** — name and description
only. The body loads on demand:

- the model calls `load_skill(name)` before working in a skill's area (its
  resources are listed in the result, to `read_file` as needed), or
- the operator runs `/skills enable <name>` to ride the body every turn, or
- a keyword trigger fires (below).

Twenty skills cost twenty lines until one is wanted.

## Keyword triggers

Frontmatter may declare `trigger: keywords: [...]`. When a turn's text
mentions one, the skill's body is injected for that turn and the transcript
shows `skill triggered: <name>` — the operator's window into a doctrine
firing. Nothing is triggered without declared keywords; a plain skill never
activates itself.

## Operator commands

`/skills list` shows what is installed, `/skills enable|disable <name>`
pins or unpins a body into every turn. The View → Skills menu is the same
list.
