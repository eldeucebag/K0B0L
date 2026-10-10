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
import base64
import json
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
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

# -- themes: the six retro palettes, ported from rt_harness.themes -----------
# Values are copied from RETRO_THEMES so the page needs no runtime import
# of the Textual stack; themes.py remains the source of truth -- keep the
# numbers in sync when a palette changes there.

WEB_THEMES: dict[str, dict[str, str]] = {
    "hotdog-3x": {
        "--bg": "#A80000", "--surface": "#8E0000", "--band": "#5C0000",
        "--ink": "#FFFF00", "--muted": "#FFCC00", "--faint": "#CC9900",
        "--accent": "#FFB000", "--accent-ink": "#000000",
        "--code-bg": "#000000", "--think-bg": "#5C0000",
        "--warn": "#FF8C00", "--err": "#FF2D2D", "--border": "#FFFF00",
    },
    "beos": {
        "--bg": "#D6D3CE", "--surface": "#C9C5BE", "--band": "#C9C5BE",
        "--ink": "#101010", "--muted": "#4A4640", "--faint": "#8A867E",
        "--accent": "#1A1AB8", "--accent-ink": "#FFFFFF",
        "--code-bg": "#FFFFFF", "--think-bg": "#C9C5BE",
        "--warn": "#A08000", "--err": "#C00000", "--border": "#8A867E",
    },
    "commodore-64": {
        "--bg": "#352879", "--surface": "#2A1F60", "--band": "#241A52",
        "--ink": "#A6A0F0", "--muted": "#9B93E8", "--faint": "#6C5EB5",
        "--accent": "#B8C76F", "--accent-ink": "#352879",
        "--code-bg": "#1B1240", "--think-bg": "#241A52",
        "--warn": "#B8C76F", "--err": "#9A6759", "--border": "#6C5EB5",
    },
    "edit-com": {
        "--bg": "#0000AA", "--surface": "#000088", "--band": "#000066",
        "--ink": "#AAAAAA", "--muted": "#767676", "--faint": "#545454",
        "--accent": "#FFFF55", "--accent-ink": "#0000AA",
        "--code-bg": "#000044", "--think-bg": "#000066",
        "--warn": "#FFFF55", "--err": "#FF5555", "--border": "#00AAAA",
    },
    "amber": {
        "--bg": "#000000", "--surface": "#140D00", "--band": "#2B1D00",
        "--ink": "#FFB000", "--muted": "#FFC46B", "--faint": "#8A5F00",
        "--accent": "#FFD9A0", "--accent-ink": "#000000",
        "--code-bg": "#140D00", "--think-bg": "#2B1D00",
        "--warn": "#E8A200", "--err": "#FF7A00", "--border": "#8A5F00",
    },
    "matrix": {
        "--bg": "#000000", "--surface": "#001A0A", "--band": "#003B00",
        "--ink": "#00FF41", "--muted": "#7CFF9B", "--faint": "#008F11",
        "--accent": "#7CFF9B", "--accent-ink": "#000000",
        "--code-bg": "#001A0A", "--think-bg": "#003B00",
        "--warn": "#B6FF00", "--err": "#FF3B3B", "--border": "#008F11",
    },
}

#: The default (original) palette, applied when no saved theme matches.
DEFAULT_THEME = {
    "--bg": "#0a0e14", "--surface": "#11151d", "--band": "#161b26",
    "--ink": "#e6e9ef", "--muted": "#8b93a3", "--faint": "#5c6474",
    "--accent": "#62e2c6", "--accent-ink": "#062a22",
    "--code-bg": "#131722", "--think-bg": "#12141c",
    "--warn": "#e2b962", "--err": "#e2627a", "--border": "#1d2330",
}

#: The web's own default sits in the table too, so selecting it is a real
#: choice the server accepts -- without this, switching away from the
#: default was a one-way door and the prefs file kept the last theme
#: forever. The TUI ignores it (not in its list) and uses its own default.
WEB_THEMES["k0b0l-dark"] = dict(DEFAULT_THEME)

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
html, body { margin: 0; height: 100%; overflow: hidden; }
body {
  background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, sans-serif;
  /* minmax(0, 1fr): a plain 1fr row grows to its content's min-height,
     so a long transcript made the BODY taller than the viewport and the
     whole page scrolled -- header and form scrolled out of the frame.
     The 0 floor pins the middle row to the window; the transcript owns
     the scrolling. */
  display: grid; grid-template-rows: auto minmax(0, 1fr) auto;
  height: 100vh; height: 100dvh;
}
header {
  display: flex; align-items: baseline; gap: 12px;
  padding: 10px 16px; border-bottom: 1px solid #1d2330;
  background: var(--surface);
  min-width: 0;
}
header .name { font-weight: 700; letter-spacing: 0.12em; }
header .name b { color: var(--accent); }
header .meta { color: var(--muted); font-size: 12.5px; font-family: var(--mono);
  min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
#transcript {
  overflow-y: auto; overflow-x: hidden;
  padding: 18px 16px 10px; scroll-behavior: smooth;
  overscroll-behavior: contain;
  display: flex; flex-direction: column; gap: 14px;
  min-height: 0; min-width: 0;
}
.turn { display: flex; flex-direction: column; gap: 14px; }
.who { font-size: 11px; letter-spacing: 0.14em; color: var(--faint);
       text-transform: uppercase; margin-bottom: -8px; }
.who.you { color: var(--accent); }
.who.busy { color: var(--muted); }
.typing { display: inline-block; margin-left: 6px; }
.typing .dot { opacity: 0.25; }
.typing.busy .dot { animation: bounce 1.2s infinite; }
.typing.busy .dot:nth-child(2) { animation-delay: 0.2s; }
.typing.busy .dot:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.35; }
  30% { transform: translateY(-4px); opacity: 1; }
}
.busyclock { color: var(--faint); margin-left: 8px; }
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
/* Long unbroken tokens must wrap inside the window, never push it wide */
.notice, .errorline, .statusline, .tool .call, figcaption {
  overflow-wrap: anywhere;
}
.errorline { color: var(--err); font-size: 13px; }
.errorline::before { content: "! "; }
.statusline { color: var(--faint); font-size: 12px;
  font-family: var(--mono); text-align: right; }
img.gen { max-width: min(100%, 560px); border-radius: 8px;
  border: 1px solid #1d2330; display: block; }
figure { margin: 0; position: relative; }
figcaption { color: var(--faint); font-size: 11.5px;
  font-family: var(--mono); margin-top: 4px; }
.img-del {
  position: absolute; top: 6px; right: 6px;
  background: var(--band); color: var(--muted);
  border: 1px solid var(--border); border-radius: 6px;
  width: 22px; height: 22px; line-height: 1; cursor: pointer;
  font-size: 12px; padding: 0;
}
.img-del:hover { color: var(--err); border-color: var(--err); }
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
.hbtn {
  flex-shrink: 0;
  margin-left: auto; background: none; border: 1px solid var(--border);
  color: var(--muted); border-radius: 8px; width: 30px; height: 26px;
  cursor: pointer; font-size: 14px; line-height: 1;
}
.hbtn:hover { color: var(--accent); border-color: var(--accent); }
.genbar-wrap {
  margin: 2px 0 6px; display: grid; gap: 4px;
}
.genbar-label {
  font-family: var(--mono); font-size: 12px; color: var(--muted);
}
.genbar {
  height: 10px; border-radius: 5px; background: var(--band);
  border: 1px solid var(--border); overflow: hidden;
}
.genbar > .fill {
  height: 100%; width: 0%; background: var(--accent);
  transition: width 0.5s ease; border-radius: 5px;
}
.genbar.indeterminate > .fill {
  width: 30%;
  animation: genpulse 1.2s ease-in-out infinite alternate;
}
@keyframes genpulse {
  from { margin-left: 0; width: 12%; }
  to { margin-left: 70%; width: 22%; }
}
#overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 40;
}
dialog {
  position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
  background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 12px;
  padding: 20px 24px;
  /* Scaled to the frame, never wider or taller than it: a fixed
     min-width overflowed small windows; an unclamped height put the
     dialog's top half out of reach. */
  width: min(360px, calc(100vw - 24px));
  max-height: calc(100vh - 32px); max-height: calc(100dvh - 32px);
  overflow-y: auto;
  z-index: 50; margin: 0; font: inherit;
}
dialog::backdrop { background: rgba(0,0,0,0.5); }
dialog h3 {
  margin: 0 0 14px; font-size: 13px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--muted);
}
dialog label.opt-label {
  display: block; font-size: 13px; color: var(--muted); margin: 10px 0 4px;
}
dialog .opt-row {
  display: flex; align-items: center; justify-content: space-between;
  margin: 10px 0;
}
dialog .opt-row .opt-label { margin: 0; }
dialog select {
  width: 100%; background: var(--bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 6px 8px; font: inherit;
}
dialog .opt-actions {
  display: flex; gap: 10px; margin-top: 18px; justify-content: flex-end;
}
dialog button {
  background: var(--band); color: var(--ink); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 14px; cursor: pointer; font: inherit;
}
dialog button:hover { border-color: var(--accent); color: var(--accent); }
.switch { position: relative; display: inline-block; width: 40px; height: 22px; }
.switch input { opacity: 0; width: 0; height: 0; }
.switch span {
  position: absolute; inset: 0; background: var(--band);
  border: 1px solid var(--border); border-radius: 12px; transition: 0.2s;
}
.switch span::before {
  content: ""; position: absolute; height: 14px; width: 14px;
  left: 3px; top: 3px; background: var(--muted); border-radius: 50%;
  transition: 0.2s;
}
.switch input:checked + span { background: var(--accent); }
.switch input:checked + span::before {
  transform: translateX(17px); background: var(--accent-ink);
}
#history-dialog { width: min(540px, calc(100vw - 24px)); }
#image-dialog { width: min(440px, calc(100vw - 24px)); }
#image-dialog .img-scroll {
  max-height: calc(100vh - 220px); max-height: calc(100dvh - 220px);
  overflow-y: auto; padding-right: 4px;
}
.img-tabs { display: flex; gap: 6px; margin-bottom: 12px; }
.img-tab {
  flex: 1; background: var(--band); color: var(--muted);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 6px 0; cursor: pointer; font: inherit; font-size: 13px;
}
.img-tab.active {
  background: var(--accent); color: var(--accent-ink);
  border-color: var(--accent); font-weight: 600;
}
.img-init-row { display: grid; gap: 10px; }
.img-drop {
  border: 1px dashed var(--border); border-radius: 8px;
  padding: 10px; text-align: center; color: var(--faint);
  cursor: pointer; font-size: 13px; min-height: 60px;
  display: flex; align-items: center; justify-content: center;
  flex-direction: column; gap: 6px; overflow: hidden;
}
.img-drop:hover { border-color: var(--accent); color: var(--muted); }
.img-drop img {
  max-width: 100%; max-height: 160px; border-radius: 6px; display: block;
}
.img-mask-wrap { display: flex; gap: 10px; align-items: flex-start; }
#img-mask-canvas {
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 8px; cursor: crosshair; touch-action: none;
  max-width: 100%;
}
.img-mask-tools { display: flex; flex-direction: column; gap: 8px; }
.img-mask-btns { display: flex; flex-direction: column; gap: 6px; }
.img-mask-btns button { font-size: 12px; padding: 5px 10px; }
dialog button.secondary:hover { color: var(--muted); }
#image-dialog textarea {
  width: 100%; background: var(--bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 10px; font: inherit; resize: vertical;
}
#image-dialog input {
  width: 100%; background: var(--bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 10px; font: inherit;
}
.img-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
.img-grid .opt-label { margin: 10px 0 4px; }
.opt-faint { color: var(--faint); font-size: 11px; }
dialog button.primary {
  background: var(--accent); color: var(--accent-ink);
  border-color: var(--accent); font-weight: 600;
}
dialog button.primary:hover { color: var(--accent-ink); }
.hist-actions { display: flex; gap: 8px; margin-bottom: 10px; }
.hist-actions input {
  flex: 1; background: var(--bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 10px; font: inherit;
}
.hist-list { display: grid; gap: 6px; max-height: 50vh; overflow-y: auto; }
.sess-row {
  display: flex; align-items: center; gap: 10px;
  background: var(--band); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px;
}
.sess-row .sess-main { flex: 1; min-width: 0; }
.sess-row .sess-name {
  font-family: var(--mono); font-size: 13px; color: var(--ink);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-row .sess-sub {
  font-size: 11.5px; color: var(--faint); font-family: var(--mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sess-row button {
  background: none; border: 0; cursor: pointer; color: var(--muted);
  font-size: 12px; padding: 4px 8px; border-radius: 6px;
}
.sess-row button:hover { color: var(--accent); }
.sess-row button.danger:hover { color: var(--err); }
.sess-empty { color: var(--faint); font-size: 13px; padding: 8px; }
@media (max-width: 640px) { header .meta { display: none; } }
</style>
</head>
<body>
<header>
  <span class="name">K<b>0</b>B<b>0</b>L</span>
  <span class="meta" id="meta">connecting…</span>
  <button id="image-btn" class="hbtn" title="generate an image">🎨</button>
  <button id="history-btn" class="hbtn" title="sessions">≡</button>
  <button id="options" class="hbtn" title="options">⚙</button>
</header>
<main id="transcript" aria-live="polite"></main>
<form id="form">
  <input id="input" autocomplete="off" placeholder="ask, or / for commands"
         aria-label="message">
  <button id="send">send</button>
</form>
<div id="overlay" hidden></div>
<dialog id="image-dialog">
  <h3>generate an image</h3>
  <div class="img-tabs">
    <button id="img-tab-txt2img" class="img-tab active">txt2img</button>
    <button id="img-tab-img2img" class="img-tab">img2img</button>
    <button id="img-tab-inpaint" class="img-tab">inpaint</button>
  </div>
  <div class="img-scroll">
  <label class="opt-label">model
    <select id="img-model">
      <option value="pony">pony — fast, tag-style prompting (SDXL)</option>
      <option value="qwen">qwen — best prompt adherence, slower</option>
      <option value="chroma">chroma — Flux-class quality</option>
    </select>
  </label>
  <div id="img-init-row" class="img-init-row" hidden>
    <label class="opt-label">init image
      <div class="img-drop" id="img-init-drop">
        <span id="img-init-name">drop / click to load</span>
        <img id="img-init-preview" alt="" hidden>
      </div>
      <input type="file" id="img-init-file" accept="image/png,image/jpeg,image/webp" hidden>
    </label>
    <div id="img-mask-block" hidden>
      <div class="opt-label">mask — paint what to change</div>
      <div class="img-mask-wrap">
        <canvas id="img-mask-canvas" width="384" height="384"></canvas>
        <div class="img-mask-tools">
          <label class="opt-label">brush
            <input type="range" id="img-brush-size" min="4" max="96" value="28">
          </label>
          <div class="img-mask-btns">
            <button id="img-mask-clear">clear</button>
            <button id="img-mask-invert" class="secondary">invert</button>
          </div>
        </div>
      </div>
    </div>
    <label class="opt-label" id="img-denoise-label">denoising strength
      <input id="img-denoise" type="number" min="0" max="1" step="0.05" value="0.75">
    </label>
  </div>
  <label class="opt-label">prompt
    <textarea id="img-prompt" rows="3"
      placeholder="what to draw"></textarea>
  </label>
  <label class="opt-label">negative prompt <span class="opt-faint">(optional)</span>
    <input id="img-negative" placeholder="what to avoid">
  </label>
  <div class="img-grid">
    <label class="opt-label">size
      <select id="img-size">
        <option value="">model default</option>
        <option value="512x512">512 × 512</option>
        <option value="768x768">768 × 768</option>
        <option value="1024x1024" selected>1024 × 1024</option>
      </select>
    </label>
    <label class="opt-label">steps
      <input id="img-steps" type="number" min="0" max="150"
        placeholder="model default">
    </label>
    <label class="opt-label">seed
      <input id="img-seed" type="number" min="0"
        placeholder="random">
    </label>
  </div>
  <div class="img-grid">
    <label class="opt-label">sampler
      <select id="img-sampler">
        <option value="">model default</option>
      </select>
    </label>
    <label class="opt-label">scheduler
      <select id="img-scheduler">
        <option value="">model default</option>
      </select>
    </label>
    <label class="opt-label">noise / rng
      <select id="img-rng">
        <option value="">server default</option>
        <option value="cuda">cuda (sd-webui)</option>
        <option value="cpu">cpu (comfyui)</option>
        <option value="std_default">std_default</option>
      </select>
    </label>
  </div>
  <div class="img-grid">
    <label class="opt-label">cfg scale
      <input id="img-cfg" type="number" min="0" max="30" step="0.5"
        placeholder="model default">
    </label>
    <label class="opt-label">batch
      <input id="img-batch" type="number" min="1" max="8" value="1">
    </label>
    <label class="opt-label">eta
      <input id="img-eta" type="number" min="0" max="1" step="0.05"
        placeholder="sampler default">
    </label>
  </div>
  </div>
  <div class="opt-actions">
    <button id="img-cancel">cancel</button>
    <button id="img-generate" class="primary">generate</button>
  </div>
</dialog>
<dialog id="history-dialog">
  <h3>sessions</h3>
  <div class="hist-actions">
    <input id="hist-name" placeholder="name to save as" maxlength="60">
    <button id="hist-save">save current</button>
  </div>
  <div id="hist-list" class="hist-list"></div>
  <div class="opt-actions">
    <button id="hist-close">close</button>
  </div>
</dialog>
<dialog id="options-dialog">
  <h3>options</h3>
  <label class="opt-label">theme
    <select id="opt-theme"></select>
  </label>
  <label class="opt-label">chat model
    <select id="opt-model"></select>
  </label>
  <div class="opt-row">
    <label class="opt-label">show thinking</label>
    <label class="switch"><input type="checkbox" id="opt-think"><span></span></label>
  </div>
  <div class="opt-row">
    <label class="opt-label">tool output</label>
    <select id="opt-tools">
      <option value="off">off</option>
      <option value="summary">summary</option>
      <option value="full">full</option>
    </select>
  </div>
  <div class="opt-actions">
    <button id="opt-clear">clear conversation</button>
    <button id="opt-close">close</button>
  </div>
</dialog>
<script>
const THEMES = {
  "k0b0l-dark": null,
  "hotdog-3x": {"--bg":"#A80000","--surface":"#8E0000","--band":"#5C0000","--ink":"#FFFF00","--muted":"#FFCC00","--faint":"#CC9900","--accent":"#FFB000","--accent-ink":"#000000","--code-bg":"#000000","--think-bg":"#5C0000","--warn":"#FF8C00","--err":"#FF2D2D","--border":"#FFFF00"},
  "beos": {"--bg":"#D6D3CE","--surface":"#C9C5BE","--band":"#C9C5BE","--ink":"#101010","--muted":"#4A4640","--faint":"#8A867E","--accent":"#1A1AB8","--accent-ink":"#FFFFFF","--code-bg":"#FFFFFF","--think-bg":"#C9C5BE","--warn":"#A08000","--err":"#C00000","--border":"#8A867E"},
  "commodore-64": {"--bg":"#352879","--surface":"#2A1F60","--band":"#241A52","--ink":"#A6A0F0","--muted":"#9B93E8","--faint":"#6C5EB5","--accent":"#B8C76F","--accent-ink":"#352879","--code-bg":"#1B1240","--think-bg":"#241A52","--warn":"#B8C76F","--err":"#9A6759","--border":"#6C5EB5"},
  "edit-com": {"--bg":"#0000AA","--surface":"#000088","--band":"#000066","--ink":"#AAAAAA","--muted":"#767676","--faint":"#545454","--accent":"#FFFF55","--accent-ink":"#0000AA","--code-bg":"#000044","--think-bg":"#000066","--warn":"#FFFF55","--err":"#FF5555","--border":"#00AAAA"},
  "amber": {"--bg":"#000000","--surface":"#140D00","--band":"#2B1D00","--ink":"#FFB000","--muted":"#FFC46B","--faint":"#8A5F00","--accent":"#FFD9A0","--accent-ink":"#000000","--code-bg":"#140D00","--think-bg":"#2B1D00","--warn":"#E8A200","--err":"#FF7A00","--border":"#8A5F00"},
  "matrix": {"--bg":"#000000","--surface":"#001A0A","--band":"#003B00","--ink":"#00FF41","--muted":"#7CFF9B","--faint":"#008F11","--accent":"#7CFF9B","--accent-ink":"#000000","--code-bg":"#001A0A","--think-bg":"#003B00","--warn":"#B6FF00","--err":"#FF3B3B","--border":"#008F11"},
};
const DEFAULT_PALETTE = {"--bg":"#0a0e14","--surface":"#11151d","--band":"#161b26","--ink":"#e6e9ef","--muted":"#8b93a3","--faint":"#5c6474","--accent":"#62e2c6","--accent-ink":"#062a22","--code-bg":"#131722","--think-bg":"#12141c","--warn":"#e2b962","--err":"#e2627a","--border":"#1d2330"};
const ROOT_VARS = ["--bg","--surface","--band","--ink","--muted","--faint","--accent","--accent-ink","--code-bg","--think-bg","--warn","--err","--border"];
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
let genBar = null;
let genLabel = null;
let currentTheme = "k0b0l-dark";
let whoEl = null;
let typingEl = null;
let busyTimer = null;
let busyStart = null;
let busy = false;

function setBusy(on) {
  // Remember the request across reconnects and element churn: a busy
  // frame can arrive before the assistant row exists (inference starts
  // before the turn header is drawn), and the input must never stay
  // wedged off because a turn element went away.
  busy = on;
  if (typingEl) {
    typingEl.classList.toggle("busy", on);
  }
  if (whoEl) {
    whoEl.classList.toggle("busy", on);
  }
  if (on && !busyStart) {
    busyStart = Date.now();
    clearInterval(busyTimer);
    busyTimer = setInterval(() => {
      if (busyStart) {
        const secs = Math.round((Date.now() - busyStart) / 1000);
        if (typingEl) {
          const clock = typingEl.querySelector(".busyclock");
          if (clock) clock.textContent = secs + "s";
        }
      }
    }, 1000);
  } else if (!on) {
    clearInterval(busyTimer);
    busyTimer = null;
    busyStart = null;
    if (typingEl) {
      const clock = typingEl.querySelector(".busyclock");
      if (clock) clock.textContent = "";
    }
  }
}

function applyTheme(name) {
  if (name !== "k0b0l-dark" && !THEMES[name]) name = "k0b0l-dark";
  const palette = THEMES[name] || DEFAULT_PALETTE;
  for (const key of ROOT_VARS) {
    document.documentElement.style.setProperty(key, palette[key]);
  }
  currentTheme = name;
  const sel = document.getElementById("opt-theme");
  if (sel && sel.value !== name) sel.value = name;
}

function newTurn() {
  turn = document.createElement("div");
  turn.className = "turn";
  transcript.appendChild(turn);
}
function ensureTurn() {
  // Slash commands and late frames can arrive with no open turn (the
  // server sends no "assistant" row for /image, and a notice can land
  // after turn_end). Rendering into a null turn throws and silently
  // kills every later frame in the batch -- the "stalled" webui.
  if (!turn) newTurn();
}
function who(name, cls) {
  ensureTurn();
  const el = document.createElement("div");
  el.className = "who " + (cls || "");
  el.textContent = name;
  turn.appendChild(el);
  return el;  // the assistant handler anchors the dots to this element
}
function scroll() {
  transcript.scrollTop = transcript.scrollHeight;
}
function el(tag, cls, text) {
  ensureTurn();
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
      applyTheme(m.theme || "k0b0l-dark");
      break;
    case "you":
      newTurn(); who("you", "you");
      proseEl = el("div", "prose"); proseEl.textContent = m.text; scroll();
      break;
    case "assistant":
      // A new turn must not inherit the previous turn's prose anchor:
      // the you-handler set proseEl, and the delta-handler appends to
      // whatever proseEl holds -- without this reset the model's words
      // stream into the *user's* reply block.
      newTurn();
      proseEl = null; thinkEl = null; genBar = null; genLabel = null;
      whoEl = who(m.thinking ? "model (thinking)" : "model");
      typingEl = document.createElement("span");
      typingEl.className = "typing";
      typingEl.innerHTML = '<span class="dot">·</span>' +
                           '<span class="dot">·</span>' +
                           '<span class="dot">·</span>' +
                           '<span class="busyclock"></span>';
      whoEl.appendChild(typingEl);
      if (busy) setBusy(true);  // busy can precede the row it belongs to
      break;
    case "busy":
      setBusy(m.on);
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
      // A panel ends any open prose/thinking run: later deltas must
      // create fresh elements so the transcript order matches the frames.
      proseEl = null; thinkEl = null;
      el("div", "tool call", m.text); scroll(); break;
    case "tool_result":
      const r = el("div", "tool result" + (m.ok ? "" : " error"));
      r.textContent = m.text;
      proseEl = null; thinkEl = null; scroll(); break;
    case "code":
      ensureTurn();
      const cd = document.createElement("details");
      cd.className = "code";
      const sum = document.createElement("summary");
      sum.textContent = m.language || "code";
      const bd = document.createElement("div"); bd.className = "body";
      const pre = document.createElement("pre"); pre.className = "code";
      pre.textContent = m.text;
      bd.appendChild(pre); cd.appendChild(sum); cd.appendChild(bd);
      turn.appendChild(cd);
      proseEl = null; thinkEl = null; scroll(); break;
    case "notice":
      el("div", "notice", m.text); scroll(); break;
    case "error":
      el("div", "errorline", m.text); scroll(); break;
    case "status":
      el("div", "statusline", m.text); scroll(); break;
    case "image":
      ensureTurn();
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.className = "gen";
      img.src = "/image/" + encodeURIComponent(m.path);
      img.alt = m.label || "generated image";
      fig.appendChild(img);
      const cap = document.createElement("figcaption");
      cap.textContent = m.path + (m.label ? " · " + m.label : "");
      fig.appendChild(cap);
      const del = document.createElement("button");
      del.className = "img-del";
      del.title = "delete this image";
      del.textContent = "✕";
      del.addEventListener("click", () => {
        if (!del.dataset.confirmed) {
          del.dataset.confirmed = "1";
          del.textContent = "sure?";
          setTimeout(() => {
            del.dataset.confirmed = "";
            del.textContent = "✕";
          }, 2500);
          return;
        }
        ws.send(JSON.stringify({ image_delete: m.path }));
        fig.remove();
      });
      fig.appendChild(del);
      turn.appendChild(fig);
      proseEl = null; thinkEl = null;
      genBar = null;
      scroll();
      break;
    case "gen_progress":
      ensureTurn();
      if (!genBar) {
        const wrap = document.createElement("div");
        wrap.className = "genbar-wrap";
        genLabel = document.createElement("div");
        genLabel.className = "genbar-label";
        genBar = document.createElement("div");
        genBar.className = "genbar";
        const fill = document.createElement("div");
        fill.className = "fill";
        genBar.appendChild(fill);
        wrap.appendChild(genLabel);
        wrap.appendChild(genBar);
        turn.appendChild(wrap);
        scroll();
      }
      if (m.total > 0 && m.step > 0) {
        genBar.classList.remove("indeterminate");
        const pct = Math.min(100, Math.round(100 * m.step / m.total));
        genBar.querySelector(".fill").style.width = pct + "%";
        genLabel.textContent = m.phase + " · " + m.step + "/" + m.total
          + " (" + pct + "%)";
      } else {
        genBar.classList.add("indeterminate");
        genLabel.textContent = m.phase;
      }
      scroll();
      break;
    case "turn_end":
      setBusy(false);
      turn = null; proseEl = null; thinkEl = null;
      whoEl = null; typingEl = null;
      send.disabled = false; input.focus();
      persistCache();
      break;
    case "restore_begin":
      // Server truth incoming: drop the localStorage guess so the
      // restored transcript is painted exactly once.
      restoring = true;
      transcript.innerHTML = "";
      turn = null; proseEl = null; thinkEl = null;
      whoEl = null; typingEl = null; genBar = null; genLabel = null;
      break;
    case "restore_end":
      restoring = false;
      persistCache();
      break;
    case "clear":
      transcript.innerHTML = "";
      turn = null; proseEl = null; thinkEl = null;
      whoEl = null; typingEl = null; genBar = null; genLabel = null;
      try { localStorage.removeItem(CACHE_KEY); } catch (err) {}
      break;
    case "session_list":
      renderSessionList(m.sessions || []);
      break;
    case "raw":
      // unhandled line kinds (slash-command output) render as muted prose
      el("div", "prose", m.text); scroll(); break;
  }
};
ws.onopen = () => { meta.textContent = "connected"; input.focus(); };
ws.onclose = () => {
  meta.textContent = "disconnected — the input still works; messages " +
    "queue in the page until the server returns";
  setBusy(false);
  send.disabled = false;  // a dead socket must not wedge the input
  input.focus();
};

document.getElementById("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  // A bare "/image" (optionally "<model> <prompt>") opens the image
  // dialog instead of sending the stripped-down command: the dialog is
  // the full surface (negative prompt, size, steps, seed).
  if (text === "/image" || text.startsWith("/image ")) {
    const rest = text.slice("/image".length).trim();
    let model = "";
    let prompt = rest;
    const first = rest.split(/\s+/)[0];
    if (["pony", "qwen", "chroma"].includes(first) && rest.includes(" ")) {
      model = first;
      prompt = rest.slice(first.length).trim();
    }
    input.value = "";
    openImageDialog({ model: model, prompt: prompt });
    return;
  }
  send.disabled = true;
  input.value = "";
  ws.send(JSON.stringify({ text }));
});

// -- session tracking (client cache + management panel) -------------------
const CACHE_KEY = "k0b0l-webui-cache-v1";
let restoring = false;

function persistCache() {
  // A best-effort local copy of the transcript: the page paints
  // instantly on reload while the server catches up, and a dead
  // server still shows the conversation. Never fatal. Suppressed while
  // a server restore repaints the page -- the guess must not
  // overwrite the truth mid-repaint.
  if (restoring) return;
  try {
    const rows = [];
    for (const t of transcript.children) {
      const you = t.querySelector && t.querySelector(".who.you");
      const prose = [];
      for (const child of t.children) {
        if (String(child.className || "").includes("prose")) {
          prose.push(child.textContent || "");
        }
      }
      // `\\n` inside this file's PAGE string is consumed by *Python* at
    // page-build time -- the browser would receive a raw newline inside
    // a string literal, a SyntaxError that killed the entire script
    // (every handler, every button). A charCode carries no backslash,
    // so the page survives the Python string intact.
    rows.push({ you: !!you, text: prose.join(String.fromCharCode(10)) });
    }
    localStorage.setItem(CACHE_KEY, JSON.stringify(rows));
  } catch (err) { /* storage full or blocked: the server is the truth */ }
}

function restoreCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return false;
    const rows = JSON.parse(raw);
    if (!Array.isArray(rows) || !rows.length) return false;
    for (const row of rows) {
      newTurn();
      if (row.you) {
        who("you", "you");
        proseEl = el("div", "prose");
        proseEl.textContent = row.text;
      } else {
        who("model");
        proseEl = el("div", "prose");
        proseEl.textContent = row.text;
      }
    }
    turn = null; proseEl = null; thinkEl = null;
    return true;
  } catch (err) { return false; }
}

function openHistory() {
  document.getElementById("history-dialog").showModal();
  ws.send(JSON.stringify({ session: { verb: "list" } }));
}

function renderSessionList(entries) {
  const list = document.getElementById("hist-list");
  list.innerHTML = "";
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "sess-empty";
    empty.textContent = "no saved sessions yet";
    list.appendChild(empty);
    return;
  }
  for (const s of entries) {
    const row = document.createElement("div");
    row.className = "sess-row";
    const main = document.createElement("div");
    main.className = "sess-main";
    const nameEl = document.createElement("div");
    nameEl.className = "sess-name";
    nameEl.textContent = s.name;
    const sub = document.createElement("div");
    sub.className = "sess-sub";
    const when = s.mtime ? new Date(s.mtime * 1000).toLocaleString() : "";
    sub.textContent = (s.turns || 0) + " turns · " +
      (s.messages || 0) + " msgs · " + when +
      (s.preview ? " · " + s.preview : "");
    main.appendChild(nameEl); main.appendChild(sub);
    row.appendChild(main);
    const loadBtn = document.createElement("button");
    loadBtn.textContent = "load";
    loadBtn.addEventListener("click", () => {
      ws.send(JSON.stringify({ session: { verb: "load", name: s.name } }));
    });
    row.appendChild(loadBtn);
    const delBtn = document.createElement("button");
    delBtn.className = "danger";
    delBtn.textContent = "✕";
    delBtn.title = "delete";
    delBtn.addEventListener("click", () => {
      ws.send(JSON.stringify({ session: { verb: "delete", name: s.name } }));
      if (s.name === "__webui_last__") {
        // the live conversation is gone server-side: drop the local
        // copy too, or a repaint resurrects it
        try { localStorage.removeItem(CACHE_KEY); } catch (err) {}
        transcript.innerHTML = "";
        turn = null; proseEl = null; thinkEl = null;
        whoEl = null; typingEl = null; genBar = null; genLabel = null;
      }
      setTimeout(() => ws.send(JSON.stringify(
        { session: { verb: "list" } })), 200);
    });
    row.appendChild(delBtn);
    list.appendChild(row);
  }
}

// -- the image dialog --------------------------------------------------------
const imageDialog = document.getElementById("image-dialog");
const IMG_MODES = ["txt2img", "img2img", "inpaint"];
let imgMode = "txt2img";
let imgInitB64 = "";      // data URL of the loaded init image
let imgInitNatural = null; // {width, height}

function setImageMode(mode) {
  imgMode = mode;
  for (const m of IMG_MODES) {
    document.getElementById("img-tab-" + m).classList
      .toggle("active", m === mode);
  }
  document.getElementById("img-init-row").hidden = mode === "txt2img";
  document.getElementById("img-mask-block").hidden = mode !== "inpaint";
  document.getElementById("img-denoise-label").hidden = mode === "txt2img";
}
for (const m of IMG_MODES) {
  document.getElementById("img-tab-" + m).addEventListener("click",
    () => setImageMode(m));
}

// -- init image loading (click / drag-drop / paste) ---------------------------
const initDrop = document.getElementById("img-init-drop");
const initFile = document.getElementById("img-init-file");
function loadInitFile(file) {
  if (!file || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    imgInitB64 = String(reader.result);
    const img = new Image();
    img.onload = () => {
      imgInitNatural = { width: img.width, height: img.height };
      const preview = document.getElementById("img-init-preview");
      preview.src = imgInitB64; preview.hidden = false;
      document.getElementById("img-init-name").textContent =
        file.name + " (" + img.width + "×" + img.height + ")";
      // size the mask canvas to the image
      const canvas = document.getElementById("img-mask-canvas");
      canvas.width = img.width; canvas.height = img.height;
      clearMask();
    };
    img.src = imgInitB64;
  };
  reader.readAsDataURL(file);
}
initDrop.addEventListener("click", () => initFile.click());
initFile.addEventListener("change", () => loadInitFile(initFile.files[0]));
initDrop.addEventListener("dragover", (e) => e.preventDefault());
initDrop.addEventListener("drop", (e) => {
  e.preventDefault();
  loadInitFile(e.dataTransfer.files[0]);
});

// -- the mask canvas: paint white = generate, black = keep -------------------
const maskCanvas = document.getElementById("img-mask-canvas");
const maskCtx = maskCanvas.getContext("2d");
let maskPainting = false;
let maskHasPaint = false;

function clearMask() {
  maskCtx.fillStyle = "#000";
  maskCtx.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
  maskHasPaint = false;
}
function maskPos(e) {
  const rect = maskCanvas.getBoundingClientRect();
  const scaleX = maskCanvas.width / rect.width;
  const scaleY = maskCanvas.height / rect.height;
  const pt = e.touches ? e.touches[0] : e;
  return { x: (pt.clientX - rect.left) * scaleX,
           y: (pt.clientY - rect.top) * scaleY };
}
function maskPaint(e) {
  if (!maskPainting) return;
  e.preventDefault();
  const p = maskPos(e);
  const size = parseInt(
    document.getElementById("img-brush-size").value, 10);
  maskCtx.fillStyle = "#fff";
  maskCtx.beginPath();
  maskCtx.arc(p.x, p.y, size / 2, 0, Math.PI * 2);
  maskCtx.fill();
  maskHasPaint = true;
}
maskCanvas.addEventListener("pointerdown", (e) => {
  maskPainting = true; maskPaint(e);
});
maskCanvas.addEventListener("pointermove", maskPaint);
addEventListener("pointerup", () => { maskPainting = false; });
document.getElementById("img-mask-clear").addEventListener("click", clearMask);
document.getElementById("img-mask-invert").addEventListener("click", () => {
  const w = maskCanvas.width, h = maskCanvas.height;
  const data = maskCtx.getImageData(0, 0, w, h);
  for (let i = 0; i < data.data.length; i += 4) {
    data.data[i] = 255 - data.data[i];
  }
  maskCtx.putImageData(data, 0, 0);
});
clearMask();

// -- populate samplers/schedulers --------------------------------------------
// Static first, from the sd.cpp binary's own --help (commit a1ded76):
// the options must be pickable BEFORE the card swaps to images, and
// the live /sdapi route only answers while sd-server runs -- which in
// llm mode it does not. A live fetch then appends anything a newer
// build adds; it 502s quietly while the llm holds the card.
const IMG_SAMPLERS = ["euler", "euler_a", "heun", "dpm2", "dpm++2s_a",
  "dpm++2m", "dpm++2mv2", "ipndm", "ipndm_v", "lcm", "ddim_trailing",
  "tcd", "res_multistep", "res_2s", "er_sde", "euler_cfg_pp",
  "euler_a_cfg_pp", "euler_ge", "dpm++2m_sde", "dpm++2m_sde_bt", "lms"];
const IMG_SCHEDULERS = ["discrete", "karras", "exponential", "ays", "gits",
  "sgm_uniform", "simple", "smoothstep", "kl_optimal", "lcm",
  "bong_tangent", "ltx2", "logit_normal", "flux2", "flux", "beta",
  "llada_image"];

function fillSelect(id, names) {
  const sel = document.getElementById(id);
  const have = new Set(Array.from(sel.options).map((o) => o.value));
  for (const n of names) {
    if (have.has(n)) continue;
    const opt = document.createElement("option");
    opt.value = n; opt.textContent = n;
    sel.appendChild(opt);
  }
}

function loadSamplers() {
  fillSelect("img-sampler", IMG_SAMPLERS);
  fillSelect("img-scheduler", IMG_SCHEDULERS);
  fetch("/image-api/sdapi/v1/samplers")
    .then((r) => (r.ok ? r.json() : null))
    .then((list) => {
      if (Array.isArray(list)) {
        fillSelect("img-sampler", list.map((s) => s.name));
      }
    })
    .catch(() => {});
}
loadSamplers();

function openImageDialog(prefill) {
  // prefill: {model, prompt} parsed off a typed "/image ..." line, so
  // the dialog and the command agree. Fields keep whatever the last
  // dialog run left unless the typed line replaces them.
  if (prefill && prefill.prompt) {
    document.getElementById("img-prompt").value = prefill.prompt;
  }
  if (prefill && prefill.model) {
    document.getElementById("img-model").value = prefill.model;
  }
  imageDialog.showModal();
  loadSamplers();
  document.getElementById("img-prompt").focus();
}

document.getElementById("image-btn").addEventListener("click",
  () => openImageDialog(null));
document.getElementById("img-cancel").addEventListener("click",
  () => imageDialog.close());
document.getElementById("img-generate").addEventListener("click", () => {
  const prompt = document.getElementById("img-prompt").value.trim();
  if (!prompt) {
    document.getElementById("img-prompt").focus();
    return;
  }
  if (imgMode !== "txt2img" && !imgInitB64) {
    document.getElementById("img-init-drop").style.borderColor = "var(--err)";
    setTimeout(() => {
      document.getElementById("img-init-drop").style.borderColor = "";
    }, 1500);
    return;
  }
  const sizeRaw = document.getElementById("img-size").value;
  let width = 0, height = 0;
  if (sizeRaw) {
    const parts = sizeRaw.split("x");
    width = parseInt(parts[0], 10); height = parseInt(parts[1], 10);
  } else if (imgInitNatural) {
    width = imgInitNatural.width; height = imgInitNatural.height;
  }
  const stepsRaw = document.getElementById("img-steps").value;
  const seedRaw = document.getElementById("img-seed").value;
  const cfgRaw = document.getElementById("img-cfg").value;
  const etaRaw = document.getElementById("img-eta").value;
  const batchRaw = document.getElementById("img-batch").value;
  const denoiseRaw = document.getElementById("img-denoise").value;
  const payload = {
    model: document.getElementById("img-model").value,
    prompt: prompt,
    negative_prompt: document.getElementById("img-negative").value.trim(),
    width: width, height: height,
    steps: stepsRaw ? parseInt(stepsRaw, 10) : 0,
    seed: seedRaw ? parseInt(seedRaw, 10) : -1,
    sampler: document.getElementById("img-sampler").value,
    scheduler: document.getElementById("img-scheduler").value,
    rng: document.getElementById("img-rng").value,
    cfg: cfgRaw ? parseFloat(cfgRaw) : 0,
    eta: etaRaw === "" ? null : parseFloat(etaRaw),
    batch_count: batchRaw ? Math.max(1, Math.min(8,
      parseInt(batchRaw, 10))) : 1,
    mode: imgMode,
  };
  if (imgMode !== "txt2img") {
    payload.init_image_b64 = imgInitB64;
    payload.denoise_strength = denoiseRaw === ""
      ? null : parseFloat(denoiseRaw);
    if (imgMode === "inpaint") {
      if (!maskHasPaint) {
        // an empty mask would regenerate the whole image silently --
        // refuse the exact mistake A1111's users make
        document.getElementById("img-mask-canvas").style.borderColor =
          "var(--err)";
        setTimeout(() => {
          document.getElementById("img-mask-canvas").style.borderColor = "";
        }, 1500);
        return;
      }
      payload.mask_b64 = maskCanvas.toDataURL("image/png");
    }
  }
  imageDialog.close();
  ws.send(JSON.stringify({ image: payload }));
});

document.getElementById("history-btn").addEventListener("click", openHistory);
document.getElementById("hist-close").addEventListener("click", () => {
  document.getElementById("history-dialog").close();
});
document.getElementById("hist-save").addEventListener("click", () => {
  const box = document.getElementById("hist-name");
  ws.send(JSON.stringify({ session: { verb: "save",
    name: box.value.trim() || "web-session" } }));
  box.value = "";
  setTimeout(() => ws.send(JSON.stringify({ session: { verb: "list" } })),
    200);
});

// Paint the cached copy immediately; the server's restored history
// clears and repaints it when the socket connects. The cache is the
// page's instant-paint guess at the last conversation -- the server's
// restore frames are the truth that replace it.
restoreCache();

const dialog = document.getElementById("options-dialog");
const themeSel = document.getElementById("opt-theme");
const modelSel = document.getElementById("opt-model");
const thinkBox = document.getElementById("opt-think");
const toolsSel = document.getElementById("opt-tools");

for (const name of Object.keys(THEMES)) {
  const opt = document.createElement("option");
  opt.value = name; opt.textContent = name === "k0b0l-dark" ? "k0b0l dark (default)" : name;
  themeSel.appendChild(opt);
}
themeSel.value = currentTheme;
themeSel.addEventListener("change", () => {
  applyTheme(themeSel.value);
  ws.send(JSON.stringify({ theme: themeSel.value }));
});

fetch("/models").then((r) => r.json()).then((d) => {
  for (const name of d.models || []) {
    const opt = document.createElement("option");
    opt.value = name; opt.textContent = name;
    modelSel.appendChild(opt);
  }
  modelSel.value = meta.textContent.split(" · ")[0] || "";
}).catch(() => {});
modelSel.addEventListener("change", () => {
  if (modelSel.value) ws.send(JSON.stringify({ text: "/model " + modelSel.value }));
});

thinkBox.addEventListener("change", () => {
  ws.send(JSON.stringify({ text: "/think " + (thinkBox.checked ? "on" : "off") }));
});
toolsSel.addEventListener("change", () => {
  ws.send(JSON.stringify({ text: "/tools " + toolsSel.value }));
});
document.getElementById("opt-clear").addEventListener("click", () => {
  ws.send(JSON.stringify({ text: "/clear" }));
  dialog.close();
});
document.getElementById("opt-close").addEventListener("click", () => dialog.close());
document.getElementById("options").addEventListener("click", () => dialog.showModal());
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
        #: Fence state for the streaming delta path; see ``delta``.
        self._fence_open = False
        self._fence_raw = ""

    def _frame(self, payload: dict[str, Any]) -> None:
        frame = json.dumps(payload)
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                self.socket.send({"type": "websocket.send", "text": frame})))

    # -- render sinks: ChatUI's command surface lands here -----------------
    def line(self, text: str = "") -> None:
        self._frame({"kind": "raw", "text": text})

    def delta(self, text: str) -> None:
        """Stream prose to the page, but hold fenced blocks until they close.

        A streamed tool-call fence is the engine's own plumbing: the ⚙
        panel already shows the call, and letting the raw JSON reach the
        page as prose reads as the same call arriving twice (the reported
        bug). A fence that closes as real code is emitted as a code frame
        instead. Same rule as the Textual front end's ``_write`` -- applied
        chunk-by-chunk rather than at turn end, so prose keeps streaming.
        """
        rest = text
        while True:
            if not self._fence_open:
                head, fence, rest = rest.partition("```")
                if head:
                    self._frame({"kind": "delta", "text": head})
                if not fence:
                    return
                self._fence_open = True
                self._fence_raw = ""
            else:
                body, fence, rest = rest.partition("```")
                self._fence_raw += body
                if not fence:
                    return
                # The fence closed: tool plumbing is dropped, code renders.
                raw = self._fence_raw
                language, _, payload = raw.partition("\n")
                language = language.strip()
                if (language not in ("tool", "tool_call")
                        and not payload.lstrip().startswith('{"name"')):
                    self._frame({"kind": "code", "language": language,
                                 "text": (payload if language else raw)
                                 .strip("\n")})
                self._fence_open = False
                self._fence_raw = ""

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

    def gen_progress(self, phase: str, step: int, total: int) -> None:
        """A generation-progress frame: the page renders a live bar."""
        self._frame({"kind": "gen_progress", "phase": phase,
                     "step": step, "total": total})

    # -- turn lifecycle: the page keys its turn bookkeeping on these -------
    def turn_start(self, model: str, protocol: str) -> None:
        self._fence_open = False
        self._fence_raw = ""
        self._frame({"kind": "assistant"})

    def turn_end(self) -> None:
        if self._fence_open:
            # An unclosed fence is shown, not swallowed -- a near-miss
            # block the operator can see beats content that vanished.
            self._frame({"kind": "delta", "text": "```" + self._fence_raw})
            self._fence_open = False
            self._fence_raw = ""
        self._frame({"kind": "turn_end"})

    # -- history repaint: a loaded session replays through the normal ------
    # -- handlers, so a restored transcript is visually identical ----------
    def send_history(self, messages: list[dict[str, Any]]) -> None:
        """Repaint stored messages as frames through the page's handlers."""
        import re as _re

        fence_re = _re.compile(r"```tool\n(\{.*?\})\n```", _re.DOTALL)
        for message in messages:
            role = message.get("role")
            if role == "user":
                body = str(message.get("content", ""))
                if body.startswith("TOOL RESULT for"):
                    head, _, rest = body.partition("\n")
                    name = head.replace("TOOL RESULT for ", "")
                    self._frame({"kind": "tool_result", "ok": True,
                                 "text": rest.strip()[:800]})
                    continue
                self._frame({"kind": "you", "text": body})
            elif role == "assistant":
                self._frame({"kind": "assistant"})
                body = str(message.get("content", ""))
                calls = message.get("tool_calls") or []
                for call in calls:
                    function = call.get("function") or {}
                    self._frame({"kind": "tool_call",
                                 "text": summarize_call(
                                     str(function.get("name", "")),
                                     function.get("arguments") or {})})
                shown = fence_re.sub("", body).strip()
                if shown:
                    self._frame({"kind": "delta", "text": shown})
                self._frame({"kind": "turn_end"})
            elif role == "tool":
                # native-protocol results: role "tool" carries the
                # "name -> ok/error" summary plus the body
                body = str(message.get("content", ""))
                head, _, rest = body.partition("\n")
                self._frame({"kind": "tool_result", "ok": True,
                             "text": rest.strip()[:800]})
        # Images the conversation generated: anything still on disk from
        # this workspace is offered, newest first, after the messages.
        try:
            images = sorted(
                (self.ui_session_root() / "images").glob("*.png"),
                key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            images = []
        for path in images[:20]:
            self._frame({"kind": "image",
                         "path": f"images/{path.name}"})

    def ui_session_root(self) -> Path:
        return self.session.workspace.root

    def inference_start(self) -> None:
        self._frame({"kind": "busy", "on": True})

    def inference_end(self) -> None:
        self._frame({"kind": "busy", "on": False})

    def refresh_folds(self, kind: str, folded: bool) -> None:
        pass  # folds are client-side details elements in the web UI

    def greet(self) -> None:
        for key, value in self.session.describe():
            self.line(f"{key:>10}: {value}")
        self.line("type /help for commands; the web front end adds none")


class SessionManager:
    """Named chat sessions over the engine's ``~/.k0b0l-sessions`` store.

    The web front end keeps one unnamed slot per browser tab (autosaved
    after every completed turn, restored on connect) and lets the
    operator additionally save under explicit names -- the same files
    the TUI's /session commands use, so the two front ends share one
    session universe.
    """

    #: The autosave slot every web tab restores from on connect. A
    #: shared slot on purpose: "restore last session" means the last
    #: conversation, whichever tab or TUI run had it.
    AUTOSAVE = "__webui_last__"

    def __init__(self, ui: "WebChat") -> None:
        self.ui = ui
        from rt_harness.infotools import _session_dir

        self.dir = _session_dir()

    def save(self, name: str) -> str:
        import json

        path = self.dir / f"{name}.json"
        payload = {"messages": self.ui.session.messages}
        path.write_text(json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8")
        return f"session saved to {path.name}"

    def load(self, name: str) -> tuple[bool, str]:
        import json

        path = self.dir / f"{name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, f"no session named {name!r}"
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return False, f"session {name!r} is empty or unreadable"
        self.ui.session.messages[:] = messages
        self.ui.session.turns = sum(
            1 for m in messages if m.get("role") == "user")
        self.ui.session.tool_calls_made = sum(
            len((m.get("tool_calls") or [])) for m in messages)
        return True, f"session {name!r} loaded ({len(messages)} messages)"

    def delete(self, name: str) -> tuple[bool, str]:
        path = self.dir / f"{name}.json"
        if not path.is_file():
            return False, f"no session named {name!r}"
        path.unlink()
        return True, f"session {name!r} deleted"

    def listing(self) -> list[dict[str, Any]]:
        """Every stored session with meta for a picker: newest first."""
        rows: list[dict[str, Any]] = []
        try:
            paths = sorted(self.dir.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return rows
        for path in paths:
            row: dict[str, Any] = {"name": path.stem,
                                   "mtime": path.stat().st_mtime}
            try:
                import json

                data = json.loads(path.read_text(encoding="utf-8"))
                messages = data.get("messages") or []
                row["messages"] = len(messages)
                row["turns"] = sum(1 for m in messages
                                   if m.get("role") == "user")
                first_user = next((str(m.get("content", ""))[:60]
                                   for m in messages
                                   if m.get("role") == "user"), "")
                row["preview"] = first_user
            except (OSError, ValueError, AttributeError):
                row["messages"] = 0
                row["preview"] = "(unreadable)"
            rows.append(row)
        return rows


# -- the server --------------------------------------------------------------

def main() -> int:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, FileResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    from rt_harness.tui import _pick_client
    from rt_harness.themes import load_saved_theme, save_theme

    config = Config.from_env(None)
    client = _pick_client(config)
    try:
        client.version()
    except OllamaError as exc:
        print(f"endpoint not reachable at {config.api_url}: {exc}")
        return 1

    chat_config = config.chat
    saved_theme = load_saved_theme(list(WEB_THEMES))
    #: The swap supervisor sits on the GPU box that serves the endpoint,
    #: on the fixed sibling port (same derivation as tools/imagegen.py).
    swap_api = (f"http://{urlparse(config.api_url).hostname or '127.0.0.1'}"
                f":7861")

    async def page(request):
        return HTMLResponse(PAGE)

    async def ws_endpoint(websocket: WebSocket) -> None:
        nonlocal saved_theme
        await websocket.accept()
        # WebChat IS a ChatUI: the session it builds binds itself as the
        # hooks, every render sinks to the socket, and dispatch's whole
        # slash-command surface works unchanged.
        ui = WebChat(websocket, client, config,
                     tool_output=chat_config.tool_output,
                     show_thinking=config.show_thinking)
        sessions = SessionManager(ui)
        # Restore the last session: the web's contract is that a refresh
        # or a reopened tab continues where the last conversation left
        # off. Fails soft -- an empty or unreadable autosave just means
        # a fresh chat.
        restored = False
        try:
            ok, _ = sessions.load(SessionManager.AUTOSAVE)
            restored = ok
        except Exception:  # noqa: BLE001 - a bad file is a fresh chat
            restored = False
        await websocket.send_json({
            "kind": "meta", "model": chat_config.model,
            "protocol": ui.session.protocol,
            "num_ctx": chat_config.num_ctx,
            "theme": saved_theme or "k0b0l-dark",
            "restored": restored,
        })
        if restored:
            # Through ui._frame with the history frames, then a flush:
            # direct send_json raced the queued frames and closed the
            # bracket before the replay painted.
            ui._frame({"kind": "restore_begin"})
            ui.send_history(ui.session.messages)
            ui._frame({"kind": "restore_end"})
            await asyncio.sleep(0.2)
        loop = asyncio.get_event_loop()
        try:
            while True:
                message = await websocket.receive_json()
                # -- session control messages first: they carry no "text",
                # so the empty-text guard below must never swallow them
                # (it did, and every panel button silently died).
                op = message.get("session")
                if isinstance(op, dict):
                    verb = str(op.get("verb", ""))
                    name = str(op.get("name", "")).strip()
                    if verb == "list":
                        await websocket.send_json({
                            "kind": "session_list",
                            "sessions": sessions.listing()})
                        continue
                    if verb == "load":
                        ok, note = sessions.load(name)
                        # All through ui._frame, the same FIFO the history
                        # frames use: a direct send_json raced the queued
                        # frames and closed the bracket before the replay.
                        ui._frame({"kind": "restore_begin"})
                        if ok:
                            ui.send_history(ui.session.messages)
                        ui._frame({"kind": "notice", "text": note})
                        ui._frame({"kind": "restore_end"})
                        sessions.save(SessionManager.AUTOSAVE)
                        await asyncio.sleep(0.2)  # let the queue flush
                        continue
                    if verb == "save":
                        note = sessions.save(name or "web-session")
                        await websocket.send_json({
                            "kind": "notice", "text": note})
                        continue
                    if verb == "delete":
                        ok, note = sessions.delete(name)
                        await websocket.send_json({
                            "kind": "notice", "text": note})
                        # A deleted session must not resurrect from the
                        # page's cache: if it was the live autosave,
                        # reset the engine too, or the very next turn
                        # re-autosaves the zombie conversation.
                        if ok and name == SessionManager.AUTOSAVE:
                            ui.session.reset()
                            await websocket.send_json({"kind": "clear"})
                        continue
                img = message.get("image")
                if isinstance(img, dict):
                    # The image dialog's structured request. Two paths:
                    # the plain one (no mode) keeps using the model-facing
                    # generate_image tool; the advanced one (mode set)
                    # calls imagegen.gen_advanced directly -- img2img,
                    # inpaint, sampler/scheduler/rng/cfg/batch are
                    # operator-only surface the tool schema never had.
                    mode = str(img.get("mode") or "")
                    model = str(img.get("model") or "pony").strip().lower()
                    if model not in ("pony", "qwen", "chroma"):
                        model = "pony"
                    prompt = str(img.get("prompt") or "").strip()
                    if not prompt:
                        await websocket.send_json({
                            "kind": "notice",
                            "text": "image generation needs a prompt"})
                        continue
                    if mode:
                        # The user's own row echoes the request whole --
                        # a truncated echo reads as a truncated prompt.
                        await websocket.send_json({
                            "kind": "you",
                            "text": f"/image {mode} {model} {prompt}"})
                        # An image turn is still a turn: the assistant row
                        # anchors the busy dots, and an immediate
                        # indeterminate bar says "generating" through the
                        # minutes of swap + weight load before any
                        # sampling step exists to report.
                        await websocket.send_json({"kind": "assistant"})
                        await websocket.send_json({"kind": "busy", "on": True})
                        await websocket.send_json({
                            "kind": "gen_progress",
                            "phase": "generating image — preparing "
                                     "(swap / weight load)",
                            "step": 0, "total": 0})
                        out_dir = Path(
                            str(ui.session.workspace.root)) / "images"
                        out_dir.mkdir(parents=True, exist_ok=True)
                        stamp = int(time.time())
                        out_name = f"{model}-{stamp}"
                        init_path = mask_path = None
                        try:
                            if img.get("init_image_b64"):
                                raw = str(img["init_image_b64"])
                                if raw.startswith("data:"):
                                    raw = raw.split(",", 1)[1]
                                init_path = out_dir / f"init-{stamp}.png"
                                init_path.write_bytes(
                                    base64.b64decode(raw))
                            if img.get("mask_b64"):
                                raw = str(img["mask_b64"])
                                if raw.startswith("data:"):
                                    raw = raw.split(",", 1)[1]
                                mask_path = out_dir / f"mask-{stamp}.png"
                                mask_path.write_bytes(
                                    base64.b64decode(raw))

                            def run_advanced(
                                    img=img, model=model, prompt=prompt,
                                    out_dir=out_dir, stamp=stamp,
                                    init_path=init_path, mask_path=mask_path):
                                import importlib.util as _ilu
                                spec = _ilu.spec_from_file_location(
                                    "imagegen",
                                    REPO / "tools" / "imagegen.py")
                                if spec is None or spec.loader is None:
                                    raise RuntimeError(
                                        "imagegen.py could not be loaded")
                                mod = _ilu.module_from_spec(spec)
                                spec.loader.exec_module(mod)
                                return mod.gen_advanced(
                                    model=model, prompt=prompt,
                                    out=str(out_dir /
                                            f"{model}-{stamp}.png"),
                                    negative_prompt=str(
                                        img.get("negative_prompt") or ""),
                                    width=int(img.get("width") or 0),
                                    height=int(img.get("height") or 0),
                                    steps=int(img.get("steps") or 0),
                                    seed=int(img.get("seed") or -1),
                                    sampler=str(
                                        img.get("sampler") or ""),
                                    scheduler=str(
                                        img.get("scheduler") or ""),
                                    rng=str(img.get("rng") or ""),
                                    cfg=float(img.get("cfg") or 0),
                                    eta=(None if img.get("eta") is None
                                         else float(img["eta"])),
                                    batch_count=int(
                                        img.get("batch_count") or 1),
                                    init_image=(str(init_path)
                                                if init_path else None),
                                    mask_image=(str(mask_path)
                                                if mask_path else None),
                                    denoise_strength=(
                                        None
                                        if img.get("denoise_strength") is None
                                        else float(
                                            img["denoise_strength"])),
                                    mask_invert=bool(
                                        img.get("mask_invert") or False),
                                )

                            # gen_advanced runs in-process, so its stderr
                            # markers land on OUR console, not in a pipe
                            # the harness reads -- poll the supervisor for
                            # progress directly and stream frames while
                            # the executor works.
                            async def poll_progress() -> None:
                                last = None
                                last_phase = None
                                last_change = time.time()
                                seen_steps = False
                                while True:
                                    await asyncio.sleep(2)
                                    try:
                                        with urllib.request.urlopen(
                                                swap_api + "/progress",
                                                timeout=2) as r:
                                            progress = json.loads(
                                                r.read()).get("progress")
                                    except Exception:  # noqa: BLE001
                                        progress = None
                                    phase = None
                                    if progress:
                                        seen_steps = True
                                        if progress != last:
                                            last = progress
                                            last_change = time.time()
                                            await websocket.send_json({
                                                "kind": "gen_progress",
                                                "phase": "sampling",
                                                "step": progress["step"],
                                                "total": progress["total"]})
                                        if time.time() - last_change > 45:
                                            # steps stalled: VAE decode
                                            phase = ("finalizing "
                                                     "(VAE decode / save)")
                                    elif seen_steps:
                                        # steps ran, then progress went
                                        # away: the completion marker
                                        phase = ("finalizing "
                                                 "(VAE decode / save)")
                                    else:
                                        phase = ("generating image — "
                                                 "preparing (swap / "
                                                 "weight load)")
                                    if phase and phase != last_phase:
                                        last_phase = phase
                                        await websocket.send_json({
                                            "kind": "gen_progress",
                                            "phase": phase,
                                            "step": 0, "total": 0})
                            progress_task = asyncio.ensure_future(
                                poll_progress())
                            try:
                                ok = await loop.run_in_executor(
                                    None, run_advanced)
                            finally:
                                progress_task.cancel()
                            if ok:
                                saved = sorted(
                                    out_dir.glob(f"{model}-{stamp}*.png"))
                                for path in saved:
                                    await websocket.send_json({
                                        "kind": "image",
                                        "path": f"images/{path.name}"})
                                await websocket.send_json({
                                    "kind": "raw",
                                    "text": (f"image saved to images/"
                                             f"{out_name}.png"
                                             + (f" (+{len(saved) - 1} more)"
                                                if len(saved) > 1 else ""))})
                            else:
                                await websocket.send_json({
                                    "kind": "error",
                                    "text": "image generation failed; see "
                                            "the server log"})
                        except Exception as exc:  # noqa: BLE001
                            # A dead turn must not kill the socket: every
                            # path below still closes the turn.
                            await websocket.send_json({
                                "kind": "error",
                                "text": f"image generation failed: {exc}"})
                        finally:
                            for temp in (init_path, mask_path):
                                if temp is not None:
                                    try:
                                        temp.unlink()
                                    except OSError:
                                        pass
                        await websocket.send_json({"kind": "turn_end"})
                        await websocket.send_json({"kind": "busy", "on": False})
                        continue
                    # plain path: the tool, unchanged
                    args: dict[str, Any] = {"prompt": prompt, "model": model}
                    negative = str(img.get("negative_prompt") or "").strip()
                    if negative:
                        args["negative_prompt"] = negative
                    size = str(img.get("size") or "").strip()
                    if size:
                        args["size"] = size
                    try:
                        steps = int(img.get("steps") or 0)
                    except (TypeError, ValueError):
                        steps = 0
                    if steps > 0:
                        args["steps"] = steps
                    try:
                        seed = int(img.get("seed") or 0)
                    except (TypeError, ValueError):
                        seed = 0
                    if seed:
                        args["seed"] = seed
                    # Echo like a typed command so the transcript shows
                    # what was asked for, then hand the GPU work to the
                    # executor: generation takes minutes and the event
                    # loop must keep serving frames.
                    await websocket.send_json({
                        "kind": "you", "text": f"/image {model} {prompt}"})
                    await websocket.send_json({"kind": "busy", "on": True})

                    def run_image(args=args):
                        return ui.session.workspace.call(
                            "generate_image", args)

                    try:
                        result = await loop.run_in_executor(
                            None, run_image)
                    except Exception as exc:  # noqa: BLE001
                        await websocket.send_json({
                            "kind": "error",
                            "text": f"image generation failed: {exc}"})
                        result = None
                    if result is not None:
                        if result.ok:
                            await websocket.send_json({
                                "kind": "raw", "text": result.text})
                        else:
                            await websocket.send_json({
                                "kind": "error", "text": result.text})
                    await websocket.send_json({"kind": "turn_end"})
                    await websocket.send_json({"kind": "busy", "on": False})
                    continue
                img_del = message.get("image_delete")
                if isinstance(img_del, str) and img_del.strip():
                    # Images are workspace files the operator owns; the
                    # path is confined exactly like the /image route.
                    rel = img_del.strip()
                    target = (chat_config.root / rel).resolve()
                    if not str(target).startswith(
                            str(chat_config.root.resolve())):
                        await websocket.send_json({
                            "kind": "notice",
                            "text": "refusing to delete outside the "
                                    "workspace"})
                    elif target.is_file():
                        try:
                            target.unlink()
                            await websocket.send_json({
                                "kind": "notice",
                                "text": f"deleted {rel}"})
                        except OSError as exc:
                            await websocket.send_json({
                                "kind": "notice",
                                "text": f"could not delete {rel}: {exc}"})
                    else:
                        await websocket.send_json({
                            "kind": "notice",
                            "text": f"no such image {rel}"})
                    continue
                wanted_theme = message.get("theme")
                if isinstance(wanted_theme, str) and wanted_theme in WEB_THEMES:
                    # The page owns the palette swap client-side; this
                    # makes the choice durable and shared with the TUI
                    # through the one prefs file, and echoes it back to
                    # the requesting tab; later tabs read the new value.
                    save_theme(wanted_theme)
                    saved_theme = wanted_theme
                    await websocket.send_json({
                        "kind": "meta", "model": chat_config.model,
                        "protocol": ui.session.protocol,
                        "num_ctx": chat_config.num_ctx,
                        "theme": wanted_theme})
                    continue
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                if text.startswith("/"):
                    # Echo the command as the user's row: a slash command
                    # is still a thing the operator typed, and without the
                    # row the whole turn (bar, result, image) renders in an
                    # unlabeled block -- or, before the page grew
                    # ensureTurn, crashed the handler on a null turn.
                    await websocket.send_json({"kind": "you", "text": text})
                    if text in ("/exit", "/quit", "/q"):
                        await websocket.send_json(
                            {"kind": "notice", "text": "the web session "
                             "stays; just close the tab"})
                        await websocket.send_json({"kind": "turn_end"})
                        continue
                    handled = await loop.run_in_executor(
                        None, ui.dispatch, text)
                    if not handled:
                        await websocket.send_json(
                            {"kind": "raw", "text": text})
                    if text.split()[0].lower() in ("/clear",):
                        # The session was reset server-side; the page's
                        # transcript must follow or the next turn paints
                        # below a conversation the model no longer holds.
                        await websocket.send_json({"kind": "clear"})
                    await websocket.send_json({"kind": "turn_end"})
                    # Slash commands can change the live session (/clear,
                    # /session load, /model): keep the autosave slot in
                    # sync, or a reconnect restores a stale conversation.
                    try:
                        sessions.save(SessionManager.AUTOSAVE)
                    except Exception:  # noqa: BLE001 - persistence is best-effort
                        pass
                    continue
                await websocket.send_json({"kind": "you", "text": text})
                # The GPU is one card shared with image generation. After an
                # image turn the model is merely unloaded (slow reload); after
                # a swap the whole llm service is *down* and the turn would
                # die on a bare connection-refused that says nothing about
                # why. Restore the service first, saying so as it happens --
                # the busy dots keep animating through the swap.
                try:
                    with urllib.request.urlopen(
                            swap_api + "/state", timeout=2) as r:
                        swap_state = json.loads(r.read())
                except Exception:  # noqa: BLE001 - no supervisor, no swap
                    swap_state = None
                if swap_state and not swap_state.get("llm", True):
                    await websocket.send_json({"kind": "busy", "on": True})
                    await websocket.send_json({
                        "kind": "notice",
                        "text": "the llm service is down — the GPU was "
                                "swapped to image generation; swapping "
                                "back (this can take a minute)..."})
                    try:
                        request = urllib.request.Request(
                            swap_api + "/swap/llm", data=b"{}",
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(
                                request, timeout=140) as r:
                            json.loads(r.read())
                        await websocket.send_json({
                            "kind": "notice",
                            "text": "the llm service is back"})
                    except Exception as exc:  # noqa: BLE001
                        await websocket.send_json({
                            "kind": "notice",
                            "text": f"could not restore the llm service: "
                                    f"{exc}"})
                    await websocket.send_json({"kind": "busy", "on": False})
                # A swapped-away model reloads its weights on this turn:
                # 10-40 s for the 8B, ~130 s for the 14B, and until the
                # first token there is no frame at all. Say so before the
                # silence, or the turn reads as hung.
                try:
                    rows = client.model_details()
                except Exception:  # noqa: BLE001 - a probe must not kill the turn
                    rows = []
                for row in rows:
                    if (str(row.get("id")) == chat_config.model
                            and str(row.get("state", "")).lower()
                            == "unloaded"):
                        await websocket.send_json({
                            "kind": "notice",
                            "text": f"loading {chat_config.model} "
                                    "(weights were swapped off the card; "
                                    "this can take a minute)"})
                        break
                await loop.run_in_executor(None, ui.ask, text)
                # Autosave after every completed turn: "restore last
                # session" must mean the conversation as it stands, not
                # as it stood when the operator last remembered to save.
                try:
                    sessions.save(SessionManager.AUTOSAVE)
                except Exception:  # noqa: BLE001 - persistence is best-effort
                    pass
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

    async def image_api(request):
        """A thin same-origin proxy to sd-server (:7860) for the page's
        read-only fetches (sampler/scheduler lists). The sdapi routes
        answer only while the card is swapped to images, so a 502 while
        the llm holds the card is normal and the page treats it as
        "model default" everywhere."""
        target = "http://" + (urlparse(config.api_url).hostname
                               or "127.0.0.1") + ":7860" \
            + request.url.path.replace("/image-api", "", 1) \
            + ("?" + str(request.url.query) if request.url.query else "")
        try:
            with urllib.request.urlopen(target, timeout=5) as r:
                return JSONResponse(json.loads(r.read()),
                                    status_code=r.status)
        except Exception as exc:  # noqa: BLE001 - not swapped = not there
            return JSONResponse({"error": str(exc)}, status_code=502)

    app = Starlette(routes=[
        Route("/", page),
        Route("/models", models),
        Route("/docs", docs),
        Route("/image-api/{path:path}", image_api),
        Route("/image/{path:path}", image),
        WebSocketRoute("/ws", ws_endpoint),
    ])
    print(f"K0B0L web on http://0.0.0.0:{PORT} "
          f"(endpoint {config.api_url})")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
