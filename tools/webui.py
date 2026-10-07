"""The web front end: K0B0L's chat served as a page.

The engine is UI-agnostic -- ``ChatHooks`` is the whole contract -- so
the web is one more front end beside the Textual one. One process:

  * ``WebHooks`` streams every engine event as WebSocket JSON frames
  * starlette serves a single self-contained page (no build step) plus
    three read-only JSON routes for docs/skills/models
  * ``/image/<path>`` serves the generated images from the workspace

The surface is a Command/Inspect console: transcript, folds for
thinking and code, an input bar, and the image results rendered as real
<img> tags -- the pixel-perfect display the terminal cannot do.

Run:  python3 tools/webui.py            (http://LAN:8321)
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(REPO))

from rt_harness.chat import ChatSession  # noqa: E402,F401  (session type ref)
from rt_harness.config import Config  # noqa: E402
from rt_harness.tools import summarize_call, ToolResult  # noqa: E402
from rt_harness.tui import ChatUI  # noqa: E402
from rt_harness.client import OllamaError  # noqa: E402

PORT = 8321

# -- the page (self-contained; theme tokens ported from the harness) -------

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K0B0L</title>
<style>
:root {
  --bg: #0a0e14; --surface: #11151d; --band: #161b26;
  --ink: #e6e9ef; --muted: #8b93a3; --faint: #5c6474;
  --accent: #62e2c6; --accent-ink: #062a22;
  --code-bg: #131722; --think-bg: #12141c;
  --warn: #e2b962; --err: #e2627a;
  --mono: ui-monospace, "Cascadia Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body {
  background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, sans-serif;
  display: grid; grid-template-rows: auto 1fr auto; height: 100vh;
}
header {
  display: flex; align-items: baseline; gap: 12px;
  padding: 10px 16px; border-bottom: 1px solid #1d2330;
  background: var(--surface);
}
header .name { font-weight: 700; letter-spacing: 0.12em; }
header .name b { color: var(--accent); }
header .meta { color: var(--muted); font-size: 12.5px; font-family: var(--mono); }
#transcript {
  overflow-y: auto; padding: 18px 16px 10px; scroll-behavior: smooth;
  display: flex; flex-direction: column; gap: 14px;
}
.turn { display: flex; flex-direction: column; gap: 14px; }
.who { font-size: 11px; letter-spacing: 0.14em; color: var(--faint);
       text-transform: uppercase; margin-bottom: -8px; }
.who.you { color: var(--accent); }
.prose { white-space: pre-wrap; overflow-wrap: anywhere; }
details { background: var(--band); border-radius: 8px; }
details summary {
  cursor: pointer; padding: 6px 12px; color: var(--muted);
  font-size: 12.5px; font-family: var(--mono); user-select: none;
  list-style: none;
}
details summary::before { content: "▸ "; color: var(--faint); }
details[open] summary::before { content: "▾ "; }
details.think summary { color: #9aa3b8; }
details .body { padding: 2px 12px 10px; }
details.think .body { color: var(--muted); font-size: 13.5px;
  white-space: pre-wrap; }
details.code .body { margin: 0; }
pre.code {
  margin: 0; padding: 10px 12px; overflow-x: auto;
  background: var(--code-bg); border-radius: 0 0 8px 8px;
  font: 13px/1.5 var(--mono);
}
.k { color: #c792ea; } .s { color: #c3e88d; } .n { color: #f78c6c; }
.c { color: #5c6474; font-style: italic; } .f { color: #82aaff; }
.tool { display: grid; gap: 2px; }
.tool .call {
  font-family: var(--mono); font-size: 13px; color: var(--accent);
  padding: 4px 0 0;
}
.tool .call::before { content: "⚙ "; }
.tool .result {
  font-family: var(--mono); font-size: 12.5px; color: var(--muted);
  background: var(--band); border-radius: 6px; padding: 8px 10px;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.tool .result.error { color: var(--err); }
.notice { color: var(--warn); font-size: 13px; }
.notice::before { content: "· "; }
.errorline { color: var(--err); font-size: 13px; }
.errorline::before { content: "! "; }
.statusline { color: var(--faint); font-size: 12px;
  font-family: var(--mono); text-align: right; }
img.gen { max-width: min(100%, 560px); border-radius: 8px;
  border: 1px solid #1d2330; display: block; }
figure { margin: 0; }
figcaption { color: var(--faint); font-size: 11.5px;
  font-family: var(--mono); margin-top: 4px; }
form { display: flex; gap: 10px; padding: 12px 16px;
  border-top: 1px solid #1d2330; background: var(--surface); }
#input {
  flex: 1; background: var(--bg); color: var(--ink);
  border: 1px solid #242c3d; border-radius: 8px; padding: 10px 12px;
  font: 14px/1.4 ui-monospace, "Cascadia Mono", Menlo, monospace;
}
#input:focus { outline: none; border-color: var(--accent); }
#send {
  background: var(--accent); color: var(--accent-ink); border: 0;
  border-radius: 8px; padding: 0 20px; font-weight: 600; cursor: pointer;
}
#send:disabled { opacity: 0.4; cursor: default; }
@media (max-width: 640px) { header .meta { display: none; } }
</style>
</head>
<body>
<header>
  <span class="name">K<b>0</b>B<b>0</b>L</span>
  <span class="meta" id="meta">connecting…</span>
</header>
<main id="transcript" aria-live="polite"></main>
<form id="form">
  <input id="input" autocomplete="off" placeholder="ask, or / for commands"
         aria-label="message">
  <button id="send">send</button>
</form>
<script>
const ws = new WebSocket(
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
const transcript = document.getElementById("transcript");
const input = document.getElementById("input");
const send = document.getElementById("send");
const meta = document.getElementById("meta");
let turn = null;
let proseEl = null;
let thinkEl = null;
let codeBuf = null;

function newTurn() {
  turn = document.createElement("div");
  turn.className = "turn";
  transcript.appendChild(turn);
}
function who(name, cls) {
  const el = document.createElement("div");
  el.className = "who " + (cls || "");
  el.textContent = name;
  turn.appendChild(el);
}
function scroll() {
  transcript.scrollTop = transcript.scrollHeight;
}
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  turn.appendChild(e);
  return e;
}

ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  switch (m.kind) {
    case "meta":
      meta.textContent = m.model + " · " + m.protocol + " · ctx " + m.num_ctx;
      break;
    case "you":
      newTurn(); who("you", "you");
      proseEl = el("div", "prose"); proseEl.textContent = m.text; scroll();
      break;
    case "assistant":
      newTurn(); who(m.thinking ? "model (thinking)" : "model");
      break;
    case "delta":
      if (!proseEl) { proseEl = el("div", "prose"); }
      proseEl.textContent += m.text; scroll();
      break;
    case "thinking":
      if (!thinkEl) {
        const d = document.createElement("details");
        d.className = "think"; d.open = m.show !== false;
        d.innerHTML = '<summary>thinking</summary><div class="body"></div>';
        turn.appendChild(d);
        thinkEl = d.querySelector(".body");
      }
      thinkEl.textContent += m.text;
      break;
    case "flush_think":
      thinkEl = null; break;
    case "tool_call":
      el("div", "tool call", m.text); scroll(); break;
    case "tool_result":
      const r = el("div", "tool result" + (m.ok ? "" : " error"));
      r.textContent = m.text; scroll(); break;
    case "notice":
      el("div", "notice", m.text); scroll(); break;
    case "error":
      el("div", "errorline", m.text); scroll(); break;
    case "status":
      el("div", "statusline", m.text); scroll(); break;
    case "image":
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.className = "gen";
      img.src = "/image/" + encodeURIComponent(m.path);
      img.alt = m.label || "generated image";
      fig.appendChild(img);
      const cap = document.createElement("figcaption");
      cap.textContent = m.path + (m.label ? " · " + m.label : "");
      fig.appendChild(cap);
      turn.appendChild(fig); scroll();
      break;
    case "turn_end":
      turn = null; proseEl = null; thinkEl = null;
      send.disabled = false; input.focus();
      break;
    case "raw":
      // unhandled line kinds (slash-command output) render as muted prose
      el("div", "prose", m.text); scroll(); break;
  }
};
ws.onopen = () => { meta.textContent = "connected"; input.focus(); };
ws.onclose = () => { meta.textContent = "disconnected — restart the server"; };

document.getElementById("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  send.disabled = true;
  input.value = "";
  ws.send(JSON.stringify({ text }));
});
</script>
</body>
</html>"""


# -- the web front end proper: a ChatUI whose renders go to the socket ---

class WebChat(ChatUI):
    """ChatUI with every render pushed as WebSocket frames.

    Subclassing ChatUI (not just ChatHooks) is the point: ``dispatch``
    already implements every slash command and renders through
    ``self.line`` -- overriding the render sinks here means the whole
    command surface works over the web with zero duplication.
    """

    def __init__(self, socket, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.socket = socket
        self._loop = asyncio.get_event_loop()

    def _frame(self, payload: dict[str, Any]) -> None:
        frame = json.dumps(payload)
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                self.socket.send({"type": "websocket.send", "text": frame})))

    # -- render sinks: ChatUI's command surface lands here -----------------
    def line(self, text: str = "") -> None:
        self._frame({"kind": "raw", "text": text})

    def delta(self, text: str) -> None:
        self._frame({"kind": "delta", "text": text})

    def thinking(self, text: str) -> None:
        self._frame({"kind": "thinking", "text": text,
                     "show": self.show_thinking})

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self._frame({"kind": "tool_call",
                     "text": summarize_call(name, arguments)})

    def tool_result(self, name: str, result: ToolResult) -> None:
        body = result.text if result.ok else f"error: {result.text}"
        self._frame({"kind": "tool_result", "ok": result.ok, "text": body})

    def notice(self, text: str) -> None:
        self._frame({"kind": "notice", "text": text})

    def error(self, text: str) -> None:
        self._frame({"kind": "error", "text": text})

    def status(self, stats) -> None:
        parts = []
        if stats.prompt_tokens:
            parts.append(f"prompt {stats.prompt_tokens}")
        if stats.output_tokens:
            rate = stats.output_tokens / stats.seconds if stats.seconds else 0
            parts.append(f"out {stats.output_tokens} ({rate:.1f} tok/s)")
        if stats.seconds:
            parts.append(f"{stats.seconds:.1f}s")
        if stats.done_reason and stats.done_reason != "stop":
            parts.append(stats.done_reason)
        self._frame({"kind": "status", "text": " · ".join(parts)})

    def show_image(self, relative_path: str) -> None:
        self._frame({"kind": "image", "path": relative_path})

    # -- turn lifecycle: the page keys its turn bookkeeping on these -------
    def turn_start(self, model: str, protocol: str) -> None:
        self._frame({"kind": "assistant"})

    def turn_end(self) -> None:
        self._frame({"kind": "turn_end"})

    def inference_start(self) -> None:
        pass

    def inference_end(self) -> None:
        pass

    def refresh_folds(self, kind: str, folded: bool) -> None:
        pass  # folds are client-side details elements in the web UI

    def greet(self) -> None:
        for key, value in self.session.describe():
            self.line(f"{key:>10}: {value}")
        self.line("type /help for commands; the web front end adds none")


# -- the server --------------------------------------------------------------

def main() -> int:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, FileResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    from rt_harness.tui import _pick_client

    config = Config.from_env(None)
    client = _pick_client(config)
    try:
        client.version()
    except OllamaError as exc:
        print(f"endpoint not reachable at {config.api_url}: {exc}")
        return 1

    chat_config = config.chat

    async def page(request):
        return HTMLResponse(PAGE)

    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        # WebChat IS a ChatUI: the session it builds binds itself as the
        # hooks, every render sinks to the socket, and dispatch's whole
        # slash-command surface works unchanged.
        ui = WebChat(websocket, client, config,
                     tool_output=chat_config.tool_output,
                     show_thinking=config.show_thinking)
        await websocket.send_json({
            "kind": "meta", "model": chat_config.model,
            "protocol": ui.session.protocol,
            "num_ctx": chat_config.num_ctx,
        })
        loop = asyncio.get_event_loop()
        try:
            while True:
                message = await websocket.receive_json()
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                if text.startswith("/"):
                    if text in ("/exit", "/quit", "/q"):
                        await websocket.send_json(
                            {"kind": "notice", "text": "the web session "
                             "stays; just close the tab"})
                        continue
                    handled = await loop.run_in_executor(
                        None, ui.dispatch, text)
                    if not handled:
                        await websocket.send_json(
                            {"kind": "raw", "text": text})
                    await websocket.send_json({"kind": "turn_end"})
                    continue
                await websocket.send_json({"kind": "you", "text": text})
                await loop.run_in_executor(None, ui.ask, text)
        except WebSocketDisconnect:
            pass
        finally:
            try:
                ui.session.memory and ui.session.memory.close()
            except Exception:  # noqa: BLE001
                pass

    async def image(request):
        rel = request.path_params["path"]
        target = (chat_config.root / rel).resolve()
        if not str(target).startswith(str(chat_config.root.resolve())):
            return JSONResponse({"error": "outside the workspace"}, 403)
        if not target.is_file():
            return JSONResponse({"error": "not found"}, 404)
        return FileResponse(target)

    async def models(request):
        try:
            rows = client.model_details()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)})
        return JSONResponse({"models": [str(r.get("name") or r.get("id"))
                                        for r in rows]})

    async def docs(request):
        from rt_harness.docs import docs_index
        return JSONResponse({"index": docs_index(chat_config)})

    app = Starlette(routes=[
        Route("/", page),
        Route("/models", models),
        Route("/docs", docs),
        Route("/image/{path:path}", image),
        WebSocketRoute("/ws", ws_endpoint),
    ])
    print(f"K0B0L web on http://0.0.0.0:{PORT} "
          f"(endpoint {config.api_url})")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
