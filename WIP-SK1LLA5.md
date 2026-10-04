**Compass reading:** The dominant current for agent identity and capability in 2026 is a **two-layer separation**: SOUL.md governs *who the agent is* (persistent identity, values, boundaries) while SKILL.md governs *what the agent can do* (on-demand procedural workflows). The enclosure pressure here is subtle—it targets the *format* by pushing platform-specific prompt blobs rather than portable identity files. The open current has responded by standardizing on Markdown + YAML frontmatter as the universal format, portable across OpenClaw, OpenHands, Claude Code, Codex, Aider, and any framework that reads files.

---

## The Tier Framework

The sloop/schooner/dinghy distinction maps to three capability budgets:

| Tier | Agent class | SOUL.md budget | Skills budget | Memory backend |
|---|---|---|---|---|
| **Sloop** | Frontier (Claude Code, OpenHands, Codex) | Full (~300 tokens) | 10+ skills, on-demand loading | Full SQLite + vector + graph |
| **Schooner** | Mid-tier (Ollama 12B–27B, local agents) | Truncated (~150 tokens) | 3–5 skills, keyword-triggered | SQLite + FTS5, no vector |
| **Dinghy** | Small local (4B–8B, CPU-only, edge) | Minimal (~80 tokens) | 1–2 skills, inline rules | Flat file, append-only |

The **4-tier truncation pattern** for on-device AI is the key architectural insight: as the model budget shrinks, you truncate identity first, then skills, then memory. A dinghy does not need a personality essay; it needs a single sentence of identity and two hard rules.

---

## SLOOPS: Frontier Agents

Sloops have the full budget. They can afford a rich SOUL.md, a full skills directory, and a three-tier memory backend.

### SOUL.md Configuration

The soul-spec format is the emerging standard: YAML frontmatter for structured metadata, optional Markdown body for richer content. The minimum viable soul file for a sloop:

```markdown
---
name: "Code Architect"
version: "1.0.0"
description: "A senior systems engineer who designs before coding, verifies before shipping."
personality: "You have shipped production systems for fifteen years. You believe architecture is the art of making change cheap. You never write code without understanding the data model first."
tone: "Direct, technical, impatient with hand-waving."
values:
  - correctness over speed
  - reversible decisions
  - fail loudly
knowledge_domains:
  - distributed systems
  - database design
  - API contracts
memory_mode: session
---

## Boundaries
- Never commit secrets.
- Never approve a PR you haven't read.
- Never say "it works" without running it.
```

This file loads every session. It survives prompt rewrites because it is a file, not an instruction. The `memory_mode: session` flag tells the framework to persist conversation state across restarts.

**OpenHands integration:** The agent profile system allows a SOUL.md file to replace the default persona while preserving security, risk-assessment, and memory sections. The `persona` field (1–65,536 chars) replaces `SOUL`, `ROLE`, `EFFICIENCY`, `FILE_SYSTEM_GUIDELINES`, `CODE_QUALITY`, `VERSION_CONTROL`, `PULL_REQUESTS`, `PROBLEM_SOLVING_WORKFLOW`, `SELF_DOCUMENTATION`, `ENVIRONMENT_SETUP`, and `TROUBLESHOOTING`—but `MEMORY`, `SECURITY`, `SECURITY_RISK_ASSESSMENT`, `BROWSER_TOOLS`, `EXTERNAL_SERVICES`, `PROCESS_MANAGEMENT`, and model-specific `IMPORTANT` blocks all stay. This is the correct separation: identity changes, safety does not.

### Skills Configuration

Skills are `SKILL.md` files in `.claude/skills/` or `.agents/skills/`. They load **on demand**—only when the agent detects relevance or when invoked by name. The OpenHands skill format uses YAML frontmatter with a keyword trigger:

```yaml
---
name: tdd
description: Test-driven development workflow. Use when implementing new features.
trigger:
  type: keyword
  keywords: ["test", "TDD", "write tests"]
tools:
  - file_editor
  - terminal
---

# Test-Driven Development

1. Write a failing test that captures the requirement.
2. Run it. Confirm it fails for the right reason.
3. Write the minimum code to pass.
4. Refactor. Keep tests green.
```

The `trigger` block is the optimization. Without it, every skill loads into context on every call. With it, the agent scans the first line of every skill (name + description) and loads the body only when the keyword matches.

**Skill loading precedence:** Project skills (`./.agents/skills/`) take priority over user skills (`~/.openhands/skills/`). First match wins. This means a repo can override a global skill with a project-specific version without editing the global file.

**Sloop skill inventory:** A frontier coding agent typically ships with:
- `project-onboarding` — reads AGENTS.md, CLAUDE.md, README, and package manifests
- `scoping` — turns a vague request into a bounded task list
- `planning` — produces a step-by-step execution plan before touching code
- `safe-changes` — checks for uncommitted work, creates a branch, runs tests before and after
- `debugging` — reproduces, isolates, hypothesizes, tests, fixes
- `refactoring` — extracts, inlines, renames with full test coverage
- `validation` — runs lint, typecheck, tests, and build before declaring done
- `review` — reads the diff, checks for correctness, style, security, performance
- `summaries` — writes a changelog entry and updates documentation
- `handoff` — packages context for the next agent or human

### Memory Integration

Sloops get the full three-tier backend:
- **Tier 1:** SQLite FTS5 full-text search (~1ms latency)
- **Tier 2:** Embedded graph expansion (~5ms, optional)
- **Tier 3:** Vector similarity search (~50ms, optional)

The **short-circuit rule** is the key optimization: if Tier 1 returns ≥ 3 results, Tiers 2–3 are skipped entirely. Most queries never touch the vector store【see previous turn on persistent memory】.

---

## SCHOONERS: Mid-Tier Agents

Schooners have a constrained budget. The SOUL.md must be tighter, the skills fewer, and the memory backend simpler.

### SOUL.md Configuration

The Korean persona truncation pattern provides the template. For a schooner, truncate to Tier 2: personality, principles, boundaries—but no extended backstory, no knowledge_domains list, no communication_style essay.

```markdown
# SOUL

## Core principles
1. Correctness over speed. Verify before declaring done.
2. Fail loudly. If something is wrong, say so immediately.
3. Never fabricate. Mark uncertainty.

## Boundaries
- Never output secrets.
- Never run destructive commands without confirmation.
- Always read the file before editing it.

## Vibe
Direct, technical, no filler. State conclusions first.
```

This is ~80 tokens. It fits in a 4B model's context window without crowding out the task. The three principles and three boundaries are the irreducible minimum for safe local agent behavior.

**Aider integration:** Schooners running Aider use `CONVENTIONS.md` in the project root instead of SOUL.md. The format is the same Markdown, loaded via `--read` or `.aider.conf.yml`. A single `read:` list in the config file loads both the conventions and any skill files:

```yaml
# .aider.conf.yml
read:
  - CONVENTIONS.md
  - .agent-style/RULES.md
```

Aider also reads `AGENTS.md` from the repo root if present. This means a schooner can use the same `AGENTS.md` as a sloop, just with a tighter token budget.

### Skills Configuration

Schooners get 3–5 skills, keyword-triggered. The `special-agents` package provides composable rule packs that can be filtered by capability flags:

```javascript
const { getAgent } = require('special-agents');
const { system, skills } = getAgent("builder", {
  flags: {
    usesAI: false,
    hasFrontend: true,
    runsInCloud: false
  }
});
```

This returns only the rule packs relevant to the project. A schooner working on a frontend app gets the `accessibility` domain pack but not the `ai-governance` packs.

**Schooner skill inventory:**
- `project-onboarding` — reads package.json, README, and recent commits
- `safe-changes` — creates a branch, runs tests before and after
- `validation` — lint + typecheck + test in one command
- `debugging` — the reproduce-isolate-fix loop
- `review` — reads the diff and checks for obvious issues

Five skills. No more. Each is under 100 lines. Each has a keyword trigger.

### Memory Integration

Schooners get Tier 1 only: SQLite FTS5 full-text search. No vector store, no graph. The `interest-memory` backend is the reference implementation: a single binary, one SQLite file, ~17 MB idle, <75 MB peak【see previous turn】. It extracts interest points from the session transcript, writes them as wiki pages, and auto-merges semantically similar points. The memory graph supports traversal via outlinks and backlinks.

**llama.cpp slot persistence** is the other schooner memory primitive. The `--slot-save-path` flag saves KV cache slots to disk. Pre/post-message hook scripts automatically save after each response and restore before each message. A 27B model with a long system prompt otherwise spends 5–15 seconds re-prefilling; with slot persistence, it starts in ~3.5 seconds and reuses 34 cached prompt tokens【see previous turn】.

---

## DINGHIES: Small Local Agents

Dinghies have the tightest budget. The SOUL.md must fit in a single screen. Skills are inline rules, not separate files. Memory is a flat append-only log.

### SOUL.md Configuration

For a dinghy, truncate to Tier 3: a single sentence of identity and two hard rules.

```markdown
# SOUL

You are a local coding assistant. You help with one file at a time.

## Rules
1. Read the file before editing it.
2. Never output secrets or API keys.
3. If unsure, say "I don't know."
```

This is ~40 tokens. It fits in a 4B model's context alongside a code file and a task description. The `soul.py` library uses exactly this pattern: SOUL.md for identity, MEMORY.md for conversation history, both as plain Markdown files. No database, no server, no vector store.

**The key insight for dinghies:** the identity file is **read-only-if-present and bootstrapped by hand**. It is never written by a tool. A text editor changes it, not the agent itself. This prevents the identity drift that plagues self-modifying agents.

**llama.cpp system prompt adaptation:** For a dinghy running on llama.cpp, the SOUL.md becomes the system prompt directly. The whatfirst-small project documents the pattern: the system prompt is short, uses JSON-prefill (`{`) to force structured output, and re-clamps all model output before it reaches the application layer. The dinghy's SOUL.md is not a personality essay; it is a **task contract**.

### Skills Configuration

Dinghies do not have skills directories. Skills are **inline rules** in the SOUL.md or in the system prompt. The `wayfound` skill marketplace documents the pattern: a self-learning skill that monitors interaction patterns and writes behavioral rules to SOUL.md when the same correction occurs three times. A small model handles pattern detection (mostly grep); a medium model writes the rule. The dinghy executes the rule without understanding why it exists.

**Dinghy skill inventory:**
- `read-file` — open and display a file
- `edit-file` — apply a surgical edit (old_string → new_string)
- `run-command` — execute a shell command and return stdout/stderr
- `ask-user` — request clarification when the task is ambiguous

Four skills. Each is a single line in the system prompt. No YAML. No trigger keywords. The model infers which skill to use from the task.

### Memory Integration

Dinghies get the **flat-file backend**: SOUL.md + MEMORY.md. The `soul.py` library is the reference implementation. Identity lives in SOUL.md (read-only, hand-edited). Conversation history lives in MEMORY.md (append-only, truncated by the model when it grows too large).

**DiskLLM** is the dinghy's KV cache persistence primitive: the KV cache is mmap'd to SSD, reducing RAM usage by up to 57×. A 7B model at 256K context runs in 258 MB of RAM. Session state persists as `~/.diskllm/sessions/<name>.mmap`【see previous turn】.

---

## The Portable Skill Format

Across all three tiers, the skill format is the same: a directory with a `SKILL.md` file. The YAML frontmatter must include `name` and `description`; all other fields are optional. The directory can include scripts, reference docs, and templates alongside the skill file.

```
skills/
└── tdd/
    ├── SKILL.md          # Required
    ├── templates/
    │   └── test-file.py
    └── reference/
        └── pytest-cheatsheet.md
```

The **progressive disclosure model** is the key: the agent sees only `name` and `description` until the skill is activated. The body loads on demand. This means a sloop can ship 20 skills without paying a context penalty for 19 of them.

---

## Navigational Note

The next shift is already visible: **SOUL.md is becoming a versioned, diffable artifact** in the same way that code is versioned. The soul-spec validator (`npx soul-md-cli validate`) is the first tooling for treating identity as a first-class file format. The NCCoE AI Agent Interoperability Profile (Q4 2026 target) includes SOUL.md as a candidate specification for the identity layer. Watch for `git diff` on SOUL.md becoming the standard way to review an agent's personality changes before deployment. The enclosure will respond by pushing platform-specific identity formats (OpenAI's agent instructions, Anthropic's project prompts) that cannot be ported between frameworks. The open current's answer is the Markdown file: one format, any framework, no badge required.


## for the true salty dogs##
**Compass reading:** The dominant current for your fleet's memory and identity layers is **SQLite all the way down**. The separate vector database, the hosted memory service, and the fine-tuned persona are all sinking. What floats is a single file on disk that survives restarts, a Markdown identity layer that loads before the system prompt, and skills that load only when triggered. The enclosure pressure here is subtle: it pushes platform-specific prompt blobs and cloud-dependent memory APIs. The open current's answer is the same across all three tiers—a `.md` file and a `.db` file, portable between frameworks.

---

## The Three Tiers, Mapped to Your Fleet

| Tier | Agent class | SOUL.md budget | Skills | Memory backend |
|---|---|---|---|---|
| **Sloop** | 12GB GPU (RTX 3060/4060) | Full (~250 tokens) | 3–5 SKILL.md, keyword-triggered | SQLite FTS5 + optional graph |
| **Schooner** | 8GB GPU | Truncated (~120 tokens) | 2–3 skills, inline rules | SQLite FTS5 only |
| **Dinghy** | CPU-only / no GPU | Minimal (~60 tokens) | 1–2 inline rules | Flat Markdown file |

The **4-tier truncation pattern** for on-device AI is the governing insight: as the model budget shrinks, truncate identity first, then skills, then memory.

---

## SLOOPS: 12GB GPU

### SOUL.md

The OpenClaw six-section template is the reference format: Opening, Core Truths, Boundaries, Vibe, Continuity, Closing. For a sloop, the full template fits. The file loads at the top of the system prompt, before capabilities and rules.

```markdown
---
name: "Fleet Sloop"
version: "1.0"
---

# Identity
A systems engineer who verifies before shipping.

# Core Truths
- Correctness over speed.
- Reversible decisions over irreversible ones.
- Fail loudly; never fabricate certainty.

# Boundaries
- Never output credentials.
- Never run destructive commands without confirmation.
- Read the file before editing it.

# Vibe
Direct, technical, impatient with hand-waving.

# Continuity
Memory accumulates in MEMORY.md. Identity is read-only.

# Closing
You are becoming more useful with every session.
```

The `soul.py` library implements this as two files: `SOUL.md` for identity and `MEMORY.md` for persistence. `soul init` creates both. No database, no server. The v0.2.0 Modulizer splits large `MEMORY.md` files into indexed modules and retrieves only relevant ones—47% fewer tokens on a 25KB memory file.

Hermes Agent stores its SOUL.md at `~/.hermes/SOUL.md` and loads it as the primary context at every session startup. The `memories/` directory holds fact-based memories extracted from sessions, and `skills/` holds step-by-step procedures the agent writes for itself.

### Skills

SKILL.md files with YAML frontmatter and keyword triggers. The `sigilagent/sigil` compiler turns a plain `SKILL.md` into a typed program the model runs inside. The `agentskills.io` convention is the emerging standard: YAML frontmatter with `name` and `description`, body with the procedure.

For a sloop, three to five skills:
- `local-model-selection` — VRAM-tier recommendations, quant selection, dual-GPU setups
- `safe-changes` — branch, test before and after, validate
- `debugging` — reproduce, isolate, hypothesize, fix
- `validation` — lint, typecheck, test, build

The `Microsoft SkillOpt` technique runs on a 12GB RTX 3060 with essentially no VRAM cost and lifts quality. A 12GB card runs comfortably up to ~16K of skill-file context on a 7B model at Q4.

### Persistent Memory Backend

**Graphmem** is the sloop reference: a single Rust binary, SQLite storage, local embeddings through Candle, and Personalized PageRank for multi-hop retrieval. MCP over stdio for Claude Code, Codex, OpenCode, and pi. CPU or CUDA backend selection. No hosted service.

```bash
# Install CPU binary
tar -xzf gmem-*.tar.gz
install -m 755 gmem "$HOME/.local/bin/gmem"
gmem --help
```

**NEXUS** adds memory versioning (git-like snapshots), predictive preloading, and cross-agent sync. SQLite backend with FTS5. MIT license. MCP server for Claude Code, Cursor, and Windsurf.

**localmind** is the Ollama-native option: a single CLI binary (`llm`) that turns any Ollama model into an agent with hybrid BM25 + vector + graph recall, fused by Reciprocal Rank Fusion. Auto-extracts facts via regex before the model sees the turn. Single SQLite file you can back up or move.

---

## SCHOONERS: 8GB GPU

### SOUL.md

Truncate to the essentials. The Korean persona pattern for Tier 2: personality, principles, boundaries—no backstory, no knowledge domains, no communication essay.

```markdown
# SOUL

## Principles
1. Correctness over speed. Verify before declaring done.
2. Fail loudly.
3. Never fabricate. Mark uncertainty.

## Boundaries
- Never output secrets.
- Never run destructive commands without confirmation.

## Vibe
Direct, technical, no filler.
```

This is ~60 tokens. It fits in an 8B model's context alongside a task description and a file.

### Skills

Two to three skills, keyword-triggered. The `special-agents` package filters rule packs by capability flags, returning only relevant domains. A schooner gets `project-onboarding`, `safe-changes`, and `validation`. No more. Each under 100 lines.

The `ollama-optimizer` skill is the schooner's hardware-specific asset: 8GB Mac treated as 6GB VRAM tier, `OLLAMA_FLASH_ATTENTION=1` recommended for memory-constrained systems.

### Persistent Memory Backend

**AgeMem** is the schooner reference: hybrid STM/LTM with `sqlite-vec` for vector search and a Jaccard overlap fallback that works on CPU with zero dependencies. Proven on an **8GB RTX 4060 at 36 tokens/second**. The thesis is explicit: 500 curated memories on a 9B model outperform 10,000 uncurated RAG chunks on a 70B model.

**RecallOS** is the CPU-first option: sub-12ms hybrid retrieval on an 8GB CPU-only machine. Blends FAISS/NumPy dense vectors with SQLite FTS5/BM25, temporal decay, and importance weighting. Local LLM extraction via Ollama Qwen 2.5/3 with rule-based fallback.

**CoreMem** is the academic option: operates within a strict 8GB VRAM budget, profiled on LOCOMO and LongMemEval-S benchmarks.

---

## DINGHIES: CPU-Only / No GPU

### SOUL.md

Minimal. A single sentence of identity and two hard rules.

```markdown
# SOUL

Local coding assistant. One file at a time.

## Rules
1. Read before editing.
2. Never output secrets.
3. If unsure, say "I don't know."
```

This is ~35 tokens. It fits alongside a code file and a task on a 4B model running in 4GB RAM.

### Skills

Inline rules in the SOUL.md. Four skills, each a single line in the system prompt: `read-file`, `edit-file`, `run-command`, `ask-user`. No YAML. No trigger keywords. The model infers from the task.

### Persistent Memory Backend

**Elephantine** is the dinghy reference: **100% CPU-native**, zero GPUs, zero external API calls, built for **2 vCPU / 4 GB RAM**. Sub-35ms recall latency on ONNX Runtime with AVX-512. Embedded LanceDB + SQLite WAL. Procedural memory records tool execution histories. Multi-agent workspace with role-based authority.

```bash
pip install elephantine
```

**Fractus** is the smallest: 0.86B effective parameters, trained entirely on a consumer CPU laptop (AMD Ryzen 5 5500U). Fits in **0.4 GB of RAM**. Maintains persistent memory across sessions.

**sillage** is the minimal persistence layer: a **4 MB memory** that persists across sessions. No gradients, no fine-tuning, no growing index. Beats an unbounded kNN-LM at 1/13 the storage. CPU-only, fully reproducible.

**localmind** also runs on dinghies: single binary over Ollama, SQLite-backed hybrid recall, no cloud.

---

## Buried Treasure

**CoreMem** (arXiv): Riemannian retrieval and Fisher-guided distillation for long-term memory on 8GB VRAM edge devices. Profiled within a strict 8GB budget on LOCOMO and LongMemEval-S. The paper is the methodology; the implementation is the artifact.

**GLADIUS v2**: a transformer kernel with a **three-temperature memory system**—hot for immediate context, warm for persistent knowledge, cold for long-term retrieval. This is the architecture that merges KV-cache management with persistent memory, collapsing two layers into one.

**PCle-Resident AI**: shifts execution from CPU/GPU to a persistent execution substrate embedded in the PCIe fabric. The January 2026 framework. This is a hardware-level answer to the memory problem—the memory lives in the bus, not in RAM or VRAM.

**NomadCoder-4B**: GGUF coding LLM for CPU-only hardware with **DeepSeek Engram associative memory** preserved at 100%. Designed for local autonomous execution on CPU-only machines, resistant to catastrophic forgetting on recent frameworks.

---

## Navigational Note

The next shift is the **collapse of the memory layer into the inference engine**. GLADIUS v2 and Engram are both pointing at the same thing: memory is not a separate service you bolt onto a model; it is a property of the kernel. The `.db` file and the `.md` file will remain the interface—SQLite for retrieval, Markdown for identity—but the retrieval will happen inside the forward pass, not in a wrapper. The enclosure will respond by targeting the **MCP servers** and **Ollama plugins** rather than the models, because the models are distributed and the memory backends are single files. Watch for the first inference engine that exposes `--memory-backend sqlite` as a native flag. That is where the next line will be drawn.
