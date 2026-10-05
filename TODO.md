# TODO — frontier-grade research harness, entirely local

Notes toward making K0B0L a frontier-grade *research* harness on this exact
machine. Every feasibility claim below is grounded in a probe run on the
host (2026-10-05), not assumed.

## The machine we are designing for (measured)

- GTX 1070 8GB Pascal, driver 580.178.04. ~3.4 GB already in use.
- 24.5 GB RAM, ~19.6 GB available; 6.1 GB swap.
- 4 CPU cores. Disk: **9.4 GB free** (98% full — anything that grows must
  grow into RAM or the network, not disk).
- Router: PrismML llama.cpp fork on :11434, `--models-max 1`, 4 models
  (Gemma-4-E4B, Qwen3-8B-Hivemind, DeepSeek-R1-Distill-14B, Bonsai-27B-TQ1_0).
- Server facts probed today: `/v1/embeddings` → **501 Not Implemented**;
  `POST /slots/0/save` → **404** (`--slot-save-path` not enabled);
  `/v1/models`, `/props` fine. A second llama-server instance (Gemma,
  16384 ctx, q4_0 KV) already runs on :57885.
- Python: fastapi, uvicorn, starlette, aiohttp, websockets, flask,
  jinja2, markdown, pygments, httpx all import. Node present. LAN IP
  192.168.99.38 — the front end can be phone/tablet/laptop reachable.

## Challenges to "unfeasible" (deductions to keep in the plan)

**"You can't run parallel sessions on one 1070."**
False as stated. Parallel *inference* is serialized (models-max 1), but
parallel *sessions* are just Python objects: `ChatSession` holds history
and is cheap. The server queues; the sessions wait. Frontier-grade means
the *operator experience* is concurrent even when the GPU is not — like a
single barista serving a queue well. Session A generating does not block
typing in session B, `/model` switches, or reading session C's history.

**"No embeddings endpoint → no semantic memory."**
Wrong twice over. (1) BM25/FTS5 is already the tier-1 recall and it is
strong for a 4-core box; (2) tier-2 does not need the server at all:
hashed bag-of-tokens + character n-gram vectors in pure Python give
cosine similarity over the same rows — no model, no endpoint, ~1 ms.
Semantic-ish recall without embeddings is a solved trick; the endpoint
would only upgrade it.

**"KV cache can't hold 100k ctx on 8GB VRAM."**
Already solved — the fork runs `--no-kv-offload`: KV lives in host RAM
(18+ GB free), streamed over PCIe. q4_0 KV at 54–81 KiB/tok means a
14B/102400-ctx session needs ~5.5–8 GB KV in *system* RAM. The cost is
bandwidth, not feasibility; the alternative (overflow to slower RAM) is
explicitly the chosen trade.

**"A web front end means Electron/npm/webpack."**
No. The engine is already UI-agnostic: `ChatHooks` is the entire
front-end contract, and `ChatSession` never touches a terminal. A web UI
is one more `ChatHooks` subclass (delta → WebSocket frame) plus a ~400
line stdlib-optional HTTP layer (uvicorn + starlette are installed).
Zero build step: server-rendered HTML + vanilla JS + htmx if wanted.
Served on the LAN — the chat becomes reachable from any device in the
house, which a TUI can never be.

**"Slot-save for instant model resume is blocked (404)."**
Only blocked by a flag: the binary supports `--slot-save-path` (in
`--help`), the running router just wasn't started with it. One line in
the launch command + a save/restore hook pair. Not research, ops.

## Gap analysis — what "frontier-grade" means here, ranked

### 1. Multiple sessions & session management (highest leverage)

Today: one live session; `/session save|load|list` writes bare JSON by
hand. Missing: named live sessions, switching without losing state,
branching (fork a session at a message), auto-persistence, metadata
(created, model, last turn, title), and per-session memory domain tags.

- `SessionRegistry`: holds N live `ChatSession`s keyed by id; active
  session pointer; the UI talks to the registry, not a session.
  Sessions are dirt-cheap (list[dict] + one HTTP client handle).
- Auto-save on every turn (debounced) into `~/.k0b0l-sessions/<id>/`:
  `messages.json` + `meta.json`. Crash-proof by construction.
- Branch: copy history up to message k → new session with a `branched_from`
  field. Enables "what if" research without endangering the mainline —
  this is what frontier labs actually use trees for.
- Title auto-generation: first assistant reply's first line, or a cheap
  summarizer pass (we already have `compress`); no new machinery.
- Sessions search: reuse FTS5 — index titles + last summary into the same
  memory store with `domain="session-index"`.
- Feasibility: trivially local. The only design risk is `ChatSession`
  assuming one workspace; fix is passing the workspace per session.

### 2. Web front end (biggest visible jump)

One process, two front ends sharing the engine:

- `rt_harness/webui.py`: starlette/uvicorn app; routes: `GET /` (single
  page), `GET /api/sessions`, `POST /api/sessions` (new/switch/branch),
  `WS /api/chat/<id>` (stream deltas, thinking, tool panels, notices),
  `GET /api/docs`, `/api/skills`. All read-only except chat POSTs.
- `WebHooks(ChatHooks)`: `delta`/`thinking` → WS frames; `tool_call` /
  `tool_result` → structured frames; `notice` → toast. ~150 lines.
- The page: hand-written HTML + a little JS; jinja2 for chrome,
  `markdown` + `pygments` server-side for assistant prose (same
  rendering rules as the TUI: thinking folded, code highlighted, tool
  calls as panels). Theme via CSS custom properties — port 2–3 themes.
- Auth: LAN only, bind 192.168.99.38, optional token in the URL since
  there's no HTTPS locally. Not exposed to the internet, full stop.
- Feasibility: everything installed; engine decoupled; ~800 lines total.
  The only real risk is Textual already owning the front-end niceties
  (folds, pickers) — the web gets semantic HTML details/summary for
  folds, a <form> for `ask_user_choice`. Different tools, same contract.
- Bonus nobody expects on this box: `ask_user_choice` over WS means the
  phone in your pocket can answer the model's branching question.

### 3. Context memory (deepen what just landed)

`/compact` + `compress_context` landed (f651048). Gaps to close:

- **Auto-recall per turn**: `recall_block(query=turn text, domain=…)`
  exists but is only wired into compression. Wire a cheap "preamble"
  recall into `send()`: top-3 FTS hits for the user's turn, marked as
  *recalled earlier notes* — the model stops re-deriving known ground.
  Guard: only when the store has rows and the turn is > N words, so
  chit-chat doesn't pay the query cost.
- **Session domains**: every session auto-tags its remembers with a
  stable domain (`session:<uuid>` or a research topic the operator
  names). Cross-session recall then falls out of FTS `domain` filters.
- **Importance decay**: `importance` is static; a note unrecalled for
  30 days should rank below one used yesterday. Add `last_recalled` +
  a small decay in the ranking SQL — one column, one ORDER BY tweak.
- **Tier-2 recall without embeddings** (see challenge above): hashed
  token+n-gram vectors, cosine, only when FTS returns 0 rows. ~120 lines,
  pure stdlib, no new deps, no server support needed.
- **Recall transparency**: every auto-recalled line already carries
  provenance; surface "recalled 3 earlier notes" as a UI notice.

### 4. Skill management (polish, not rearchitecture)

Landed: SKILL.md format, triggers, requires-chain, progressive
disclosure, in-chat editor. Gaps:

- **Skill import**: `skills install <git-url|path>` → clone/copy into
  `~/.k0b0l-skills/`, frontmatter validated on arrival; a URL pasted in
  chat becomes a one-command install. The agentskills.io convention means
  the ecosystem imports without adaptation.
- **Skill search** across both roots with FTS over name+description+
  body; `load_skill` and the editor both gain a query mode.
- **Skill versioning**: frontmatter `version:` is parsed but unused;
  pin it into the index line and diff on upgrade (string compare, no
  git dependency).
- **Conflict report**: two skills claiming the same name/keywords
  across workspace/profile roots should say so at scan time.
- **Skill usage memory**: when a skill body is loaded, `remember` the
  pairing (skill + task phrase, importance 0.4, domain `skills`) — the
  harness learns which skills fire together, and recall can suggest.

### 5. Serving-quality-of-life (small, high comfort)

- `--slot-save-path` on the router launch + save-on-idle hook (the 404
  is a flag away; dedupe slot resume with session save).
- Streamed tool results over WS already come free with #2.
- `/models` + per-session model override in the web UI (trivial once
  sessions are first-class).

## What we deliberately do NOT chase (and why, so future-us doesn't relitigate)

- **Vector DBs / FAISS / dedicated embedding models** — disk is 98% full,
  a second model would contend for the one GPU, and tier-2 hashed
  recall covers the gap. Revisit only if recall demonstrably fails.
- **Parallel GPU inference** — physically one 1070; the queue is honest
  engineering, not a limitation to hack around.
- **Cloud/remote models as fallback** — "entirely local" is the point;
  the harness must stay useful with the network cable out.
- **Microservices** — one process, one SQLite file, one port. The
  frontier labs' quality comes from tight feedback loops, not topology.

## Phasing (dependency order, each independently shippable)

1. **SessionRegistry + auto-save + branch** (#1) — unblocks everything.
2. **Tier-2 recall + auto-recall preamble + decay** (#3) — engine-only,
   testable without any UI.
3. **Web front end MVP** (#2): read-only transcript + WS chat + session
   switcher. Pick up `ask_user_choice` over WS after basic chat works.
4. **Skill import/search/usage-memory** (#4).
5. **Slot-save ops** (#5) when the router next gets restarted anyway.

Each phase lands with its own test suite (pattern is established:
scripted client + real `ChatSession`, no model needed).

## Probe provenance (all measured this session)

- RAM/disk/CPU/python modules/LAN IP: `/tmp/probe_frontier.py` output.
- Router endpoints: `/tmp/probe_router.py` (`/props` keys, embeddings 501,
  slot-save 404). Slot-save capability confirmed present in binary
  `--help` earlier this arc.
- q4_0 KV sizes: established earlier (54–81 KiB/tok by model size).
- Second server instance on :57885 observed via ps.
