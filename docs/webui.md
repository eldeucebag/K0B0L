# Web UI (the browser front end)

The browser face of the same chat session: one file, no build step, real
pixels. Everything the terminal front ends render — streamed prose, tool
panels, thinking folds, themes, sessions — plus what a terminal cannot do:
images inline, an A1111-style image dialog, and clickable artifact actions.

## Running it

```bash
python3 tools/webui.py                # http://0.0.0.0:8321 → http://LAN-IP:8321

sudo systemctl enable --now k0b0l-webui   # as a service: survives reboots,
                                          # restarts on death (Restart=always)
sudo systemctl restart k0b0l-webui        # after editing tools/webui.py
```

The unit source ships in `tools/webui.service` (installed to
`/etc/systemd/system/k0b0l-webui.service`).

## What it renders

- **Streamed markdown** in chat prose: headings, emphasis, lists, tables,
  blockquotes, inline code, http(s) links. The renderer is escape-first —
  model output is untrusted in this harness, so no live tags from hostile
  prose ever reach the DOM. Fenced code still opens its own panel.
- **Tool panels**: one per tool call, with the result under it, matching
  the terminal's `summary` level.
- **Images inline**, each in its own alternating turn (assistant row →
  image → close), with per-image actions: **copy** (the prompt),
  **retry** (re-send the full request), **copy request** (the whole
  parameter set as JSON), and ✕ delete (click twice to confirm).
- **Live generation progress**: a bar that is indeterminate through the
  swap + weight load ("preparing"), steps during sampling, "finalizing"
  through VAE decode, and a seconds clock from the moment you send.
- **Cancel in place of send**: while any generation runs (text or
  image), the send button becomes **cancel** — same spot, still
  clickable, danger-tinted. A click stops the run: a text turn stops
  at the next token (the partial reply stays on the page and in the
  conversation), an image run is stopped at the sd-server (the GPU
  is handed straight back to the llm service). When nothing runs, a
  cancel click answers "nothing is running". Turns run as server-side
  tasks, so the cancel is heard even from the tab that started the
  generation.
- **Sessions**: autosaved after every turn; a refresh restores the last
  conversation; the ≡ panel lists, loads, saves, and deletes named
  sessions. Each session maintains its own graph: loading a named
  session makes that document the autosave target (its turns and
  branch edits grow ITS file), a new chat (`/clear`) pivots back to
  the scratch slot so it can never overwrite a named document, and a
  restart restores the conversation you were in.
- **Skill authoring** (🧠): click a skill to open its raw SKILL.md in
  the editor — create from a template, save (frontmatter-validated),
  delete (click twice). New skills are created in the workspace
  `skills/` dir, never the profile. The same operations work by chat:
  `/skills new|show|edit|delete`.
- **Input bar**: three lines tall, growing with the message (Enter
  sends, Shift+Enter is a newline). **+** attaches a file — its text
  is appended to the prompt on send (the model sees it as part of the
  message); the button stays flagged until the send consumes it.
- **Copy on every message**: each turn's header row carries a ⧉ that
  copies that message whole (the prompt, or the model's full answer).
- **Scrolling is yours**: new content follows only while you are at
  the bottom — scroll up to read the backlog while an answer streams
  and it stays put; your own send (or a fresh page load) jumps to
  the latest once.
- **Themes** (⚙): the same seven palettes as the terminal front ends,
  persisted to `~/.k0b0l-chat-ui.json` — the web and the TUI agree.
- **Options** (⚙): model, show-thinking, reasoning generation
  (`off` asks the model for no reasoning tokens at all — distinct from
  show-thinking, which only hides them), **model image tool**
  (unchecked, the model cannot generate images at all — build and
  refine image prompts in prose; your 🎨 dialog is unaffected),
  tool-output level, clear.

## Generations survive a refresh

Image generation is **server-side state, not socket state**. Every frame
of a run is recorded in a run log and broadcast to every open tab, so:

- Refresh mid-generation → the page rejoins the in-flight run on
  connect: same bar, same animation, because the run was never tied to
  your socket.
- A second tab sees the same run live.
- A run that finished within the last five minutes replays for a fresh
  page, image and buttons intact.
- The requester dying mid-run (closed tab, dead network) cannot strand
  the GPU or wedge the registry: the run finishes, retires itself, and
  the card returns to the LLM automatically.

All three ways to generate go through this: the 🎨 dialog, a typed
`/image`, and the model's own `generate_image` tool-call.

## The image dialog

🎨 (or a bare `/image`) opens it: tabs **txt2img / img2img / inpaint**,
model select (pony / qwen / chroma), prompt + negative prompt,
size/steps/seed, and the full sampler surface — sampler, scheduler, RNG
source, cfg, eta, batch. img2img and inpaint take an upload (click or
drag); inpaint paints its mask on a canvas (brush size, clear, invert —
white regenerates, black keeps). Behind the wheel is sd.cpp's A1111-shaped
sdapi route with an OpenAI-route fallback.

## Endpoints

| route | what |
| --- | --- |
| `/` | the page (self-contained: no CDN, no external assets) |
| /ws | the frame stream (chat, tools, progress, sessions, themes) |
| /image/{path} | serves generated images from the workspace |
| /image-api/{path} | same-origin proxy for the image server |

Frame kinds over the socket: `meta`, `you`, `assistant`, `delta`,
`thinking`, `code`, `tool_call`, `tool_result`, `image`, `gen_progress`,
`busy`, `raw`, `notice`, `error`, `turn_end`, `clear`,
`restore_begin`/`restore_end`, `session_list`.

Client messages: `{"text": "..."}` (a turn or slash command),
`{"image": {...}}` (dialog/typed generation op), `{"cancel": true}`
(stop whatever is running), plus the session/theme/tree/skills verbs.
A turn is a server-side task — further messages (including cancel)
are processed while it runs.

## Notes

- The page is a Python string inside `tools/webui.py` — probe the served
  VALUE, not the source text (a `\n` in the source is consumed by Python
  at page-build time and has broken the whole script before).
- `tests/.scratch/check_served_page.py` guards it: `node --check` on the
  served script and every scripted id asserted present.
- Theme changes made in the TUI apply on the web session's next connect.
