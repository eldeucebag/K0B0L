# Memory

The model has long-term memory of its own, shared by every session of this
harness — the model that writes a fact and the model that recalls it do not
have to be the same, or even running in the same week.

## The mechanic: self-memorization

Memory is **model-driven, not harness-driven**. Nothing is recalled into a
turn automatically and nothing is written behind the model's back — both
sides are tool calls the model makes when it judges them worthwhile:

| Tool | Does |
| --- | --- |
| `remember(body, domain, importance)` | Write one durable fact, one clear sentence |
| `recall(query, domain, limit)` | Find what was written before, with provenance |

`recall` with an empty query returns the most important recent notes. Results
come back as lines carrying their provenance — `- The PrismML router evicts a model... [serving] (2026-10-04 by Gemma-4-E4B-...)` — because a memory the model cannot
attribute is one it cannot trust.

## Where it lives

One SQLite file, `~/.k0b0l-memory.sqlite`, with an FTS5 full-text index
(porter-stemmed). No server, no daemon, no embeddings — the survey answer
for cramped quarters was that the separate vector database is disappearing,
and lexical search answers most recalls in about a millisecond. The recall
path is the seam embeddings would slot into later, not a rewrite.

## What it is for

Cross-domain research runs. The chat trims at `CHAT_HISTORY_TURNS` (40) and
the server window is finite; the store is the layer under both. A serving
fact learned in one session is available to a research session weeks later,
under a different model, in a different workspace — domains are free-form
scope tags (`serving`, `research`, a project name) that narrow recalls
without blinding them.

Duplicates converge: remembering the same body in the same domain raises the
existing row's importance rather than stacking a twin, so the store stays
relevant instead of bloating as runs re-derive the same facts.

## The model's own instructions

The system prompt teaches the mechanic and leaves the judgement to the
model: *"remember what a future session would otherwise have to rediscover,
and recall when starting or resuming a research task."*

## Session-level state (distinct from this)

Chat sessions (`/session save`) persist one conversation's messages.
Slot-level KV-cache save/restore (llama.cpp `--slot-save-path`) would cover
fast model-switch resume at the server; this harness does not drive it yet —
verified working on the PrismML build (`save`/`erase`/`restore` round-trip),
pending the reuse-after-restore behaviour this fork predates.
