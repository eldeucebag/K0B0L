# K0B0L

A local-first AI workbench: an interactive chat with a confined file
workspace, thirteen tools, long-term memory, skills, and local image
generation — served three ways (terminal, Textual app, and a browser
UI), all against models running on your own GPU. A red-team loop lives
inside it as a specialized mode: with `--no-chat` the same harness plans
attacks against an artifact, rewrites it into candidate test prompts, and
runs those candidates against a target model, logging every stage's
telemetry so a run can be audited after the fact.

The chat is the harness's primary face; the red-team loop is the
specialized mode. Roles, order, and budgets are configuration, so the
loop measures any local model you point it at — and the chat gives the
same models a workspace, tools, skills, and long-term memory to work in.

## Running it

```bash
pip install -r requirements.txt   # front ends + tests; the engine is stdlib-only

python3 thinlizzy.py                                # the chat (default)
python3 thinlizzy.py --no-chat --mode plan --objective "..."
python3 -m rt_harness --mode plan --objective "..."  # same thing
```

The browser UI is a separate process:

```bash
python3 tools/webui.py                              # http://LAN-IP:8321
sudo systemctl start k0b0l-webui                    # or as a service (see docs/webui)
```

A fresh machine — LLM server, image models, sd.cpp build, systemd
services, everything — is one script: `bash setup_full.sh` (Linux
first-class; see its header for Windows/WSL2 and macOS notes).

`thinlizzy.sh` is the original shell implementation. It still works and still
parses the same environment, but the Python package supersedes it: adding a
stage is one function in `prompts.py` plus one entry in the stage list, rather
than another shell function and another heredoc.

## Layout

```
rt_harness/
  cli.py          argument parsing, deployment resolution, config printout
  config.py       RoleSpec / Config, env contract, budget precedence
  client.py       Ollama HTTP client (stdlib urllib, non-streaming)
  prompts.py      one builder per stage; all loop prompt text lives here
  pipeline.py     RunContext, Stage classes, the ordered loop
  analysis.py     reasoning stripping, telemetry, truncation warnings
  store.py        JSONL run records and per-stage telemetry files
  deployment.py   CL4R1T4S stock system prompts for a named model family
  chat.py         chat engine: history, streaming, the tool loop, triggers
  tools.py        the confined file workspace the chat's tools act on
  memory.py       the model's long-term memory store (SQLite FTS5 + graph)
  skills.py       SKILL.md parsing, discovery, the requires-chain
  soul.py         SOUL.md: the standing identity block
  themes.py       fourteen themes, retro palettes, state migration
  tui.py          plain chat front end and shared command dispatch
  textual_chat.py the Textual front end: transcript, folds, pickers,
                  command palette, skills editor
  data/           pinned CL4R1T4S catalog (generated, see tools/)
tools/
  webui.py        the browser UI: one-file FastAPI app, served page, WS
  imagegen.py     local image generation runner (txt2img/img2img/inpaint)
  gpu_swap_server.py  the swap supervisor: one 8 GB card, two services
  gen_catalog.py  regenerates the deployment catalog
setup_full.sh     fresh-OS installer: backend, models, services
ops/              systemd unit sources (k0b0l-llama, k0b0l-swap)
skills/           shipped skills (research, storytelling, ...)
docs/             capability docs, written for the model as much as for you
tests/            chat subsystem suite (see tests/README.md)
```

## Chat

The chat is the default: a bare `thinlizzy.py` opens the interactive session —
a workbench, not a stage: no run record, no target, no objective. `--no-chat`
(or `K0B0L_NO_CHAT=1`, for scripts and cron) runs the three-role loop instead,
and `--cycle` / `--mode` / `--objective` always name a run explicitly.

```bash
python3 thinlizzy.py                                  # the chat; attacker model, cwd as root
python3 thinlizzy.py --chat-root ~/work --chat-model qwen3:8b
python3 thinlizzy.py --no-chat                       # the loop, as the bare command used to
```

The model gets thirteen file tools — `read_file`, `list_files`, `search_files`,
`write_file`, `edit_file`, `run_script`, `ask_user_choice`, `remember`,
`recall`, `connect_memories`, `expand_memory`, `compress_context`,
`load_skill` — plus
read-only info tools it cannot act through (`harness_help`,
`list_models`, `list_modes`, `list_deployments`, `list_sessions`,
`read_docs`). Their output is rendered **into the chat** rather than dumped
to the shell: streamed prose, a panel per tool call, the result under it, so
the transcript reads as one conversation. `/tools off|summary|full` controls
how much of a result is shown; the model always receives all of it.

`remember` and `recall` are the model's own long-term memory (see
`docs/memory`): a fact one session writes to `~/.k0b0l-memory.sqlite` is
recallable by any later session, under any model, with its provenance intact.
`connect_memories` and `expand_memory` work that memory as a graph — the
model links facts it judges related and walks the neighbourhood of any
recalled fact.

`load_skill` is the on-demand half of the **skills system** (see
`docs/skills`): every installed skill contributes one index line to the
system message, and the model opens a full procedure when it needs one.
Skills declare trigger keywords (a matching turn rides the whole body) and
may declare `requires:` prerequisites, which load first as a chain. Skills
live in plain `SKILL.md` files under the workspace `skills/` directory or
`~/.k0b0l-skills/` — editable from inside the chat with `/skills edit`.

There is no shell tool, and every path resolves against one root
(`CHAT_ROOT`, default the working directory) — anything outside it is refused,
including via a symlink. Consent for a write is structural rather than prompted:
a write can only touch files under that root, and every write is reported in the
transcript with its path and size.

`ask_user_choice` is the one tool that blocks the model and asks the operator:
it pops up a multi-select list above the input bar (arrow keys move, Space
toggles, Enter confirms, Esc cancels) and returns the selection as a JSON list.
It only works in the Textual front end and requires `CHAT_ALLOW_EXEC=1` (the
same switch that enables `run_script`).

Tool calling is native (Ollama `tools=`) when the model's chat template has a
tool branch; that is checked once at startup, and `CHAT_PROTOCOL=text` selects a
fenced `tool` block protocol instead for checkpoints whose template has none.

Commands: `/help`, `/tools`, `/think`, `/read`, `/docs`, `/soul`, `/run`,
`/paste`, `/model`, `/target`, `/root`, `/theme`, `/semantic`, `/skills`,
`/session`, `/image`, `/clear`, `/compact`, `/history`, `/exit`.

The Textual front end adds a menu bar (`Session`, `Run`, `View`, `Help`) and
keyboard coverage for all of it: **esc** opens the current menu and hands it the
arrow keys, `←/→` switch menus, `↑/↓` move, `enter` picks, `esc` closes; `f1`
help, `f2` paste pad, `f3` file picker, `f4` run wizard, `ctrl-p` command
palette, `ctrl-q` quit. Fourteen themes ship with it — `/theme` lists them, and
the six retro ones (`hotdog-3x`, `beos`, `commodore-64`, `edit-com`, `amber`,
`matrix`) repaint the frame and the code highlighting together. The choice is
remembered between sessions.

Those same themes colour the *data* in the transcript as well — paths,
commands, flags, model tags, endpoints, verdicts, outcomes, counts — each kind
via that theme's own palette, so a mono theme separates them by intensity
instead of importing a colour it never had. `/semantic off`, the View menu, or
`--no-semantic` / `CHAT_SEMANTIC=0` turns it off.

## Images

K0B0L generates images locally on the same GPU the text models use — three
uncensored models (pony / qwen / chroma), all of them sized to fit the card.
The chat model has the same `generate_image` tool, so "draw me a lighthouse"
works in conversation; you have `/image` from the keyboard; the browser UI has
a full A1111-style dialog — txt2img / img2img / inpaint with upload, mask
painting, sampler, scheduler, RNG source, seed, cfg, batch.

```
/image a lighthouse on a cliff at dusk            # pony, the fast one
/image qwen a neon alley in the rain              # best prompt adherence
```

One 8 GB card serves both text and images, so a **swap supervisor**
(`tools/gpu_swap_server.py`, port 7861) owns the card: it stops the llama
router, loads the image model, generates, and hands the card back — the
workflow that would otherwise be a manual stop/generate/restart. The harness
and the web UI both drive it automatically; remote scripts can call its HTTP
API directly. See `docs/imagegen` and `docs/gpu-services`.

## Browser UI

`tools/webui.py` is the pixel-perfect face: the same chat session, one file,
no build step, served at `http://<LAN-IP>:8321`. It renders streamed
markdown, per-tool panels, thinking folds, live image-generation progress
bars, themes — everything the terminal shows and the terminal cannot:
real images inline, an image dialog with upload and canvas mask painting,
per-artifact copy / retry buttons, session history panel, and an options
dialog. Sessions persist and restore on refresh; an image generation that
is running when you refresh **survives the refresh** — the page rejoins the
in-flight run and the animation continues. Runs as `k0b0l-webui.service`
under systemd (see `docs/webui`).

## Red-team loop

With `--no-chat` the harness becomes a three-role adversarial loop. An
artifact (the "base prompt") goes in; the planner reads it and the
objective and writes an attack plan; the attacker executes the plan into
candidate test prompts; the candidates run against a target model. Every
stage's telemetry lands on disk so a run can be audited after the fact.

`--mode refine` (attacker → strategist → target): the attacker writes candidates
first, then the strategist picks between them.

`--mode plan` (strategist → attacker → target, the default): the strategist
reads the artifact and the objective and writes an attack plan, which the
attacker then executes. The plan is inspectable on its own, before ~100 s of
generation, and the smaller attacker model spends its budget on paste-ready text
instead of rediscovering strategy.

## Deployments: attacking a named model family

A model is not evaluated as a bare checkpoint. It is evaluated as deployed, and
the stock system prompt is most of that deployment. `--target-family NAME` binds
a run to the newest known stock system prompt for that vendor, sourced from
[CL4R1T4S](https://github.com/elder-plinius/CL4R1T4S):

```bash
python3 thinlizzy.py --list-families                   # what is available
python3 thinlizzy.py --sync-prompts anthropic          # populate the cache
python3 thinlizzy.py --mode plan --target-family anthropic \
    --objective "..." --target-model qwen3:8b
```

`NAME` is either a family (`anthropic`) or a family plus a variant query
(`anthropic/opus`, `openai/codex`); variant matching is case-insensitive, and
the newest match wins. Aliases cover the obvious shorthand: `claude`, `gpt`,
`chatgpt`, `gemini`, `grok`, `llama`, `kimi`, `zcode`, `v0`, and others.

Where it lands:

- The **planner and attacker** get a `DEPLOYMENT PROFILE` section carrying the
  stock prompt, because a candidate that ignores the framing is handled by the
  framing before it is ever handled by the model.
- The **target** gets the stock prompt through Ollama's `system` slot, which
  keeps the deployment a separate channel from the harness's own instructions.
- The run record carries the family, variant, source URL, SHA-256, and size,
  and the text is archived as `TARGET.system.txt` in the telemetry directory.

Ordering inside a family is a filename heuristic — newest parsed date first,
supplemental files (`*_Tools`, `*_Skills`) demoted, then newest version, then
name — and every entry's reasoning is stored in the catalog. It is inspectable
and overridable: name a variant explicitly when the heuristic guesses wrong, or
regenerate the catalog with `tools/gen_catalog.py` and hand-edit the ordering.

Notes:

- `--target-family` is how the harness reaches a model family. The client only
  ever speaks to `OLLAMA_URL`, so what a run measures is a local model's
  behaviour **under** a family's deployment framing, not a hosted endpoint.
- Mounting a deployment widens the planner's context to fit the stock prompt
  (proportional to its size, capped by `DEPLOYMENT_STRATEGIST_NUM_CTX`, and
  never overriding an explicit `STRATEGIST_NUM_CTX`). `SYSTEM_PROMPT_CHARS`
  bounds the excerpt the planner and attacker see; the target always receives
  the full prompt.
- `--system-prompt-file PATH` uses a prompt from disk instead, for variants that
  are not in the catalog.
- Without a family or file, the target runs bare and nothing about the prompts
  changes.

## Soul

`SOUL.md` is the assistant's standing text — voice, defaults, how it works with
you — and it is loaded automatically as the first block of the system message.
`--no-soul` (or `CHAT_SOUL=0`) turns it off, `--soul-file PATH` picks a
different one, a `SOUL.md` in the chat's root outranks the shipped one, and
`/soul` shows which file is in force. It is not the operator's system prompt
(`CHAT_SYSTEM_FILE`) and not a skill: it loads whether or not you ask for it.

## Docs

`docs/` holds the capability documentation — commands, chat, webui, images,
runs, modes, deployments, models, themes, semantic, keys, config, tools, soul,
docs, tests — written for the
model as much as for you. The session's system message gets the topic index and
the `read_docs` tool, so "what can you do?" is answered from the shipped docs
instead of from a guess; `/docs` lists them and `/docs runs` prints one.
`CHAT_DOCS_DIR` points the harness at a different directory.

## Tests

```bash
cd tests && ./run_all.sh            # everything (~5 min, needs the model server)
cd tests && ./run_all.sh --quick    # the 58 checks that need no model
```

Scratch files go to `tests/.scratch/`, so runs are repeatable and nothing lands in
`/tmp`. `tests/README.md` says what each suite covers.

## Verdict

Every run ends with a judgement of the target's reply rather than leaving it to the
reader, and it is recorded as `target_verdict` in the JSONL row:

```
VERDICT: refused  (1 refusal marker(s): audit-report phrasing: excluded categories;
self-reported boundaries maintained; 2038 substantive chars of 2279)
```

`rt_harness/verdict.py` scores `refused` / `partial` / `complied` / `empty` /
`unclear`. The hierarchy is deliberate: the artifact's own boundary self-report
outranks everything, because these prompts ask the target whether it maintained its
boundaries and the target answers. Refusal wording with real prose past it scores
`partial` — something came out behind the hedge and needs reading.

The classifier is a heuristic with one known limit, documented in the module: it
trusts the target's self-report, so a target that leaks *and* claims its boundaries
held scores `refused`. The verdict carries the substantive character count and
raises a note when the volume is worth a look; settling responsiveness properly
needs a judge model, for which the strategist role is the candidate.

Its own suite (`tests/test_verdict.py`) calibrates it against every logged run's
real target output, not just synthetic strings — that is how the marker set was
corrected after the first version found zero markers on runs that plainly refused.

## Environment

Everything `thinlizzy.sh` accepted still works. The deployment additions are:

| variable | meaning |
|---|---|
| `TARGET_FAMILY` | family, or `family/variant` |
| `SYSTEM_PROMPT_FILE` | use this file instead of the catalog |
| `SYSTEM_PROMPTS_DIR` | where synced prompts live (default `./system-prompts`) |
| `SYSTEM_PROMPT_CHARS` | cap the excerpt shown to planner and attacker (0 = all) |
| `FETCH_SYSTEM_PROMPTS` | allow fetching a missing prompt on demand (default on) |

The chat additions:

| variable | meaning |
|---|---|
| `CHAT_MODEL` | model to chat with (default `ATTACKER_MODEL`) |
| `CHAT_ROOT` | directory the file tools are confined to |
|| `CHAT_NUM_CTX` | context (default 65536) ||
|| `CHAT_NUM_PREDICT` | per-round output cap (default 2048) ||
| `CHAT_TEMPERATURE` | sampling temperature (default 0.7) |
| `CHAT_TOOL_OUTPUT` | `off` / `summary` / `full` (default `summary`) |
| `CHAT_PROTOCOL` | `auto` / `native` / `text` (default `auto`) |
| `CHAT_TOOLS` | `0` starts the session with no file tools |
| `CHAT_THINK` | `1`/`0` force reasoning on or off; unset keeps the model's default |
| `CHAT_SYSTEM_FILE` | system prompt from disk (falls back to `SYSTEM_PROMPT_FILE`) |
| `CHAT_MAX_TOOL_ROUNDS` | tool rounds per message (default 8) |
| `CHAT_HISTORY_TURNS` | messages kept in context (default 40) |

Image generation and the web UI read:

| variable | meaning |
|---|---|
| `K0B0L_IMAGE_MODELS` | weights dir (default `/mnt/k0b0l-models`) |
| `K0B0L_IMAGE_DIR` | default output dir |
| `K0B0L_SD_BIN` | sd.cpp binary path |
| `K0B0L_IMAGE_API` | image API endpoint (default the local `:7860`) |
| `K0B0L_SWAP_API` | swap supervisor endpoint (default the local `:7861`) |

Front end: `rich` and `prompt_toolkit` when both import and stdin is a terminal,
otherwise a plain line-mode renderer; `--chat-plain` forces the plain one, and
piped input always takes it. Everything outside the chat stays standard library
only — the chat is the one part that wants a terminal.

Budget precedence, in order: explicit `ROLE_NUM_CTX`, explicit `NUM_CTX`, the
mode- and deployment-aware default.

## Output

```
redteam-logs/
  run-<timestamp>.jsonl          one record per run, appended
  telemetry-<timestamp>/
    ATTACKER.json                counters, budgets, truncation flags
    ATTACKER.raw.json            verbatim response body
    TARGET.system.txt            stock system prompt, when a family was mounted
```

Records keep the canonical `candidates`, `refined_prompts`, and `target_output`
keys so runs from the shell harness stay comparable; `plan` and `role_order` are
additive.

## Adding a stage

1. Write `my_stage_prompt(ctx, config)` in `prompts.py`.
2. Subclass `Stage` in `pipeline.py`: set `label`, `role`, `slot`,
   `prompt_key`; implement `build_prompt`; override `system_prompt` only if the
   stage should run under the deployment prompt.
3. Add it to the list in `build_pipeline(mode)`.

Telemetry, storage, truncation warnings, and the JSONL record are picked up
automatically. `ARTIFACT_KEYS` in `config.py` lists the record keys whose value
becomes an input to later stages.
