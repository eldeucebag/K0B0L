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
    # Windows 3.1 "Hot Dog Stand", from the scheme's own value table:
    # #FFFF00 desktop, black menu/desktop text (the scheme's own Menu
    # Text 0.0.0), #FF0000 windows, black title bars, #C0C0C0 button
    # face, #808080 button shadow, white #FFFFFF menu bar. Readability:
    # body is BLACK on yellow; folds/results are the silver button face
    # with the scheme's dark red; code/thinking sit on the white menu
    # bar. Every pair is contrast-checked (see check_webui_themes.py).
    "hotdog-3x": {
        "--bg": "#FFFF00", "--surface": "#C0C0C0", "--band": "#C0C0C0",
        "--ink": "#000000", "--muted": "#A00000", "--faint": "#808080",
        "--accent": "#FF0000", "--accent-ink": "#FFFF00",
        "--code-bg": "#FFFFFF", "--think-bg": "#FFFFFF",
        "--warn": "#FF0000", "--err": "#A00000",
        "--border": "#000000",
        "--syn-k": "#FF0000", "--syn-s": "#A00000", "--syn-n": "#FF0000",
        "--syn-c": "#808080", "--syn-f": "#000000",
    },
    # BeOS R5, from Haiku's preserved default UI colors: #D8D8D8 panel
    # grey, #336698 desktop, #0000E5 keyboard-navigation blue (the
    # interactive highlight BeOS actually used for links/selection --
    # the #FFCB00 tab is a BACKGROUND with black text and reads badly
    # as foreground on light grey), document background white,
    # failure #C00000 (the TUI's own BeOS error red).
    "beos": {
        "--bg": "#D8D8D8", "--surface": "#DEDEDE", "--band": "#E8E8E8",
        "--ink": "#101010", "--muted": "#3F3F3F", "--faint": "#707070",
        "--accent": "#0000E5", "--accent-ink": "#FFFFFF",
        "--code-bg": "#FFFFFF", "--think-bg": "#E8E8E8",
        "--warn": "#606030", "--err": "#C00000", "--border": "#ACACAC",
        "--syn-k": "#0000E5", "--syn-s": "#27528C", "--syn-n": "#6698CB",
        "--syn-c": "#9C9C9C", "--syn-f": "#336698",
    },
    # C64 boot screen, colorec (pepto's own remeasured palette -- the
    # 2001 values were violet-heavy): blue #2E2C9B background, light
    # blue #706DEB border (colors 6/14, the actual boot pairing), with
    # white body text for legibility (light blue on blue measures
    # ~2.6:1; white is VIC-II color 1 and what C64 software used when
    # it needed to be READ). Folds and code are black (color 0) with
    # grey #B2B2B2 (color F) secondary text. All 16 colorec colors.
    "commodore-64": {
        "--bg": "#2E2C9B", "--surface": "#2E2C9B", "--band": "#000000",
        "--ink": "#FFFFFF", "--muted": "#B2B2B2", "--faint": "#7B7B7B",
        "--accent": "#EDF171", "--accent-ink": "#2E2C9B",
        "--code-bg": "#000000", "--think-bg": "#000000",
        "--warn": "#EDF171", "--err": "#FF6C76", "--border": "#706DEB",
        "--syn-k": "#EDF171", "--syn-s": "#A9FF9F", "--syn-n": "#FFFFFF",
        "--syn-c": "#7B7B7B", "--syn-f": "#75CEC8",
    },
    # MS-DOS Editor (EDIT.COM/QBasic IDE), EGA: #0000AA blue screen,
    # #AAAAAA silver text, #00AAAA cyan title/menu bar, #FFFF55 yellow,
    # #55FF55 bright green, #FF5555 bright red, #55FFFF bright cyan.
    # Readability: body silver on blue (the editor's own pairing,
    # 7.4:1); muted is bright cyan (3.9:1) the way QBasic dimmed
    # selections; code blocks are the black of a DOS text window.
    "edit-com": {
        "--bg": "#0000AA", "--surface": "#0000AA", "--band": "#0000AA",
        "--ink": "#AAAAAA", "--muted": "#55FFFF", "--faint": "#5555FF",
        "--accent": "#00AAAA", "--accent-ink": "#000000",
        "--code-bg": "#000000", "--think-bg": "#000055",
        "--warn": "#FFFF55", "--err": "#FF5555", "--border": "#00AAAA",
        "--syn-k": "#FFFF55", "--syn-s": "#55FF55", "--syn-n": "#FFFFFF",
        "--syn-c": "#5555FF", "--syn-f": "#55FFFF",
    },
    "amber": {
        "--bg": "#000000", "--surface": "#140D00", "--band": "#2B1D00",
        "--ink": "#FFB000", "--muted": "#FFC46B", "--faint": "#8A5F00",
        "--accent": "#FFD9A0", "--accent-ink": "#000000",
        "--code-bg": "#140D00", "--think-bg": "#2B1D00",
        "--warn": "#E8A200", "--err": "#FF7A00", "--border": "#8A5F00",
        "--syn-k": "#FFD9A0", "--syn-s": "#FFB000", "--syn-n": "#FFC46B",
        "--syn-c": "#8A5F00", "--syn-f": "#FFD9A0",
    },
    "matrix": {
        "--bg": "#000000", "--surface": "#001A0A", "--band": "#003B00",
        "--ink": "#00FF41", "--muted": "#7CFF9B", "--faint": "#008F11",
        "--accent": "#7CFF9B", "--accent-ink": "#000000",
        "--code-bg": "#001A0A", "--think-bg": "#003B00",
        "--warn": "#B6FF00", "--err": "#FF3B3B", "--border": "#008F11",
        "--syn-k": "#7CFF9B", "--syn-s": "#00FF41", "--syn-n": "#7CFF9B",
        "--syn-c": "#008F11", "--syn-f": "#00FF41",
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
#: Code tokens for the default (the pastel set the :root block declares);
#: every palette must own its tokens or the previous theme's survive.
WEB_THEMES["k0b0l-dark"].update({
    "--syn-k": "#c792ea", "--syn-s": "#c3e88d", "--syn-n": "#f78c6c",
    "--syn-c": "#5c6474", "--syn-f": "#82aaff",
})

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
  /* code tokens: per-theme, so a retro theme's code blocks don't wear
     another era's pastels */
  --syn-k: #c792ea; --syn-s: #c3e88d; --syn-n: #f78c6c;
  --syn-c: #5c6474; --syn-f: #82aaff;
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
  padding: 10px 16px; border-bottom: 1px solid var(--border);
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
       text-transform: uppercase; margin-bottom: -8px;
       display: flex; align-items: center; gap: 8px; }
.who.you { color: var(--accent); }
.who.busy { color: var(--muted); }
.who-copy { background: none; border: 0; color: var(--faint);
  cursor: pointer; font-size: 13px; line-height: 1; padding: 0 2px;
  opacity: 0; transition: opacity 0.12s ease; }
.who:hover .who-copy { opacity: 1; }
.who-copy:hover { color: var(--accent); }
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
/* markdown-rendered prose: the renderer emits real tags and <br>, so
   pre-wrap would double-space every line */
.prose.md { white-space: normal; }
.prose.md p { margin: 0 0 0.5em; }
.prose.md p:last-child { margin-bottom: 0; }
.prose.md h1, .prose.md h2, .prose.md h3, .prose.md h4 {
  margin: 0.7em 0 0.3em; line-height: 1.3; }
.prose.md h1 { font-size: 1.3em; } .prose.md h2 { font-size: 1.18em; }
.prose.md h3 { font-size: 1.08em; } .prose.md h4 { font-size: 1em; }
.prose.md code {
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 4px; padding: 0.05em 0.35em; font: 0.9em var(--mono);
}
.prose.md a { color: var(--accent); }
.prose.md ul, .prose.md ol { margin: 0.3em 0 0.6em; padding-left: 1.5em; }
.prose.md blockquote {
  border-left: 3px solid var(--border); margin: 0.45em 0;
  padding: 0.1em 0.9em; color: var(--muted);
}
.prose.md table { border-collapse: collapse; margin: 0.5em 0;
  display: block; overflow-x: auto; }
.prose.md th, .prose.md td {
  border: 1px solid var(--border); padding: 4px 10px; text-align: left; }
.prose.md th { color: var(--muted); font-family: var(--mono);
  font-size: 0.85em; }
.prose.md hr { border: 0; border-top: 1px solid var(--border);
  margin: 0.8em 0; }
details { background: var(--band); border-radius: 8px; }
details summary {
  cursor: pointer; padding: 6px 12px; color: var(--muted);
  font-size: 12.5px; font-family: var(--mono); user-select: none;
  list-style: none;
}
details summary::before { content: "▸ "; color: var(--faint); }
details[open] summary::before { content: "▾ "; }
details.think summary { color: var(--muted); }
details .body { padding: 2px 12px 10px; }
details.think .body { color: var(--muted); font-size: 13.5px;
  white-space: pre-wrap; }
details.think .body, details.think summary { background: var(--think-bg); }
details.code .body { margin: 0; }
pre.code {
  margin: 0; padding: 10px 12px; overflow-x: auto;
  background: var(--code-bg); border-radius: 0 0 8px 8px;
  font: 13px/1.5 var(--mono);
}
.k { color: var(--syn-k); } .s { color: var(--syn-s); } .n { color: var(--syn-n); }
.c { color: var(--syn-c); font-style: italic; } .f { color: var(--syn-f); }
/* embedded code blocks: their own place in the transcript, the same
   visual treatment the thinking fold gets -- a band, not body text */
details.code { background: var(--think-bg); }
details.code summary { color: var(--accent); }
details.code .code-copy { background: none; border: 0;
  color: var(--faint); cursor: pointer; font-size: 13px;
  line-height: 1; padding: 0 2px; opacity: 0;
  transition: opacity 0.12s ease; }
details.code:hover .code-copy { opacity: 1; }
details.code .code-copy:hover { color: var(--accent); }
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
  border: 1px solid var(--border); display: block; }
audio.gen-audio { width: min(100%, 420px); display: block; }
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
.artifact-actions {
  display: flex; gap: 6px; margin-top: 4px;
}
.artifact-actions .act {
  background: var(--band); color: var(--muted);
  border: 1px solid var(--border); border-radius: 6px;
  font-size: 11.5px; padding: 3px 10px; cursor: pointer;
  font-family: var(--mono);
}
.artifact-actions .act:hover { color: var(--accent); border-color: var(--accent); }
form { display: flex; gap: 10px; padding: 12px 16px;
  border-top: 1px solid var(--border); background: var(--surface);
  align-items: flex-end; }
#input, #attach-note {
  background: var(--bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px;
  font: 14px/1.4 ui-monospace, "Cascadia Mono", Menlo, monospace;
}
#input {
  flex: 1; resize: none; overflow-y: auto;
  /* Three lines tall at rest (1fr rows on the shell mean the form is
     a fixed band; the textarea owns its height), growing with the
     message up to a sane ceiling. */
  min-height: 3.9em; max-height: 14em;
  transition: min-height 0.12s ease;
}
#input:focus { outline: none; border-color: var(--accent); }
#attach {
  background: none; color: var(--muted); border: 1px solid var(--border);
  border-radius: 8px; width: 42px; height: 42px; cursor: pointer;
  font-size: 20px; line-height: 1; flex-shrink: 0;
}
#attach:hover { color: var(--accent); border-color: var(--accent); }
#attach.has-file { color: var(--accent); border-color: var(--accent); }
#attach-file { display: none; }
#send {
  background: var(--accent); color: var(--accent-ink); border: 0;
  border-radius: 8px; padding: 0 20px; font-weight: 600; cursor: pointer;
  height: 42px;
}
#send:disabled { opacity: 0.4; cursor: default; }
#send.cancel {
  /* The button IS the cancel affordance while a generation runs: the
     operator asked for cancel-in-place-of-send, not a greyed-out send.
     Danger-tinted so the state change is visible at a glance. */
  background: transparent; color: var(--err);
  border: 1px solid var(--err);
}
#send.cancel:hover { background: var(--err); color: var(--accent-ink); }
.hbtn {
  flex-shrink: 0;
  margin-left: auto; background: none; border: 1px solid var(--border);
  color: var(--muted); border-radius: 8px; width: 30px; height: 26px;
  cursor: pointer; font-size: 14px; line-height: 1;
}
.hbtn:hover { color: var(--accent); border-color: var(--accent); }
.skills-list { display: grid; gap: 6px; max-height: 40vh; overflow-y: auto; }
.skill-row {
  display: flex; align-items: center; gap: 10px;
  background: var(--band); border-radius: 8px; padding: 8px 12px;
}
.skill-row .sk-name { font-family: var(--mono); color: var(--ink);
  font-size: 13px; flex-shrink: 0; }
.skill-row .sk-desc { color: var(--muted); font-size: 12px;
  overflow-wrap: anywhere; }
.skill-row button {
  margin-left: auto; flex-shrink: 0;
  background: var(--surface); color: var(--muted);
  border: 1px solid var(--border); border-radius: 6px;
  font-size: 11.5px; padding: 3px 10px; cursor: pointer;
  font-family: var(--mono);
}
.skill-row button.on { color: var(--accent); border-color: var(--accent); }
.skill-row button:hover { color: var(--accent); border-color: var(--accent); }
.core-details summary { color: var(--muted); font-size: 12.5px;
  cursor: pointer; margin-top: 10px; }
.core-text {
  margin: 8px 0 0; padding: 10px 12px; max-height: 30vh; overflow-y: auto;
  background: var(--code-bg); border-radius: 8px;
  color: var(--ink); font: 12.5px/1.5 var(--mono);
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.skill-edit-head { display: flex; gap: 8px; align-items: center;
  margin-top: 10px; }
.skill-edit-head .sk-name { flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
#skill-body-edit {
  width: 100%; box-sizing: border-box; margin-top: 8px;
  background: var(--code-bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; resize: vertical;
  font: 12.5px/1.5 var(--mono); white-space: pre; overflow-wrap: anywhere;
}
#skill-body-edit:focus { outline: none; border-color: var(--accent); }
#skill-edit-status { margin-top: 6px; min-height: 1em; }
.branch-switcher {
  display: flex; gap: 6px; align-items: center;
  min-width: 0; overflow-x: auto; scrollbar-width: thin;
}
.branch-chip {
  flex-shrink: 0; background: var(--band); color: var(--muted);
  border: 1px solid var(--border); border-radius: 12px;
  font-size: 11px; padding: 2px 10px; cursor: pointer;
  font-family: var(--mono); max-width: 160px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.branch-chip.on {
  color: var(--accent); border-color: var(--accent);
}
.branch-chip:hover { color: var(--accent); border-color: var(--accent); }
.branch-label { color: var(--faint); font-size: 11px;
  font-family: var(--mono); flex-shrink: 0; }
#tree-hint { color: var(--faint); font-size: 11.5px; padding: 2px 16px; }
.turn-actions { margin-top: 6px; }
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
#toast-host {
  position: fixed; bottom: 86px; left: 50%; transform: translateX(-50%);
  display: flex; flex-direction: column; align-items: center; gap: 6px;
  z-index: 60; pointer-events: none;
}
.form-fields { display: flex; flex-direction: column; gap: 12px;
  max-height: 60vh; overflow-y: auto; min-width: 46vw; }
.form-group { border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; display: flex; flex-direction: column; gap: 10px; }
.form-group-label { font-size: 11px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--faint); }
.form-field label { display: block; font-size: 12.5px;
  color: var(--muted); margin-bottom: 4px; }
.form-opts { display: flex; flex-wrap: wrap; gap: 6px; }
.form-opt { background: var(--band); color: var(--ink);
  border: 1px solid var(--border); border-radius: 12px;
  padding: 4px 12px; cursor: pointer; font-size: 13px; }
.form-opt.sel { color: var(--accent); border-color: var(--accent); }
.form-text { background: var(--bg); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
  font: 13px/1.4 ui-monospace, "Cascadia Mono", Menlo, monospace;
  width: 100%; box-sizing: border-box; }
.form-text:focus { outline: none; border-color: var(--accent); }
#form-dialog .opt-actions { margin-top: 12px; display: flex; gap: 8px;
  justify-content: flex-end; }
#form-send { background: var(--accent); color: var(--accent-ink);
  border: 0; border-radius: 8px; padding: 6px 18px; font-weight: 600;
  cursor: pointer; }
#form-cancel { background: none; color: var(--muted);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 14px;
  cursor: pointer; }
.toast {
  background: var(--surface); color: var(--ink);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 6px 14px; font-size: 12.5px; font-family: var(--mono);
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
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
  <span id="branch-switcher" class="branch-switcher"></span>
  <button id="image-btn" class="hbtn" title="generate an image">🎨</button>
  <button id="music-btn" class="hbtn" title="generate music">🎵</button>
  <button id="skills-btn" class="hbtn" title="skills & core">🧠</button>
  <button id="history-btn" class="hbtn" title="sessions">≡</button>
  <button id="options" class="hbtn" title="options">⚙</button>
</header>
<main id="transcript" aria-live="polite"></main>
<div id="tree-hint" style="display:none"></div>
<form id="form">
  <textarea id="input" rows="3" autocomplete="off"
    placeholder="ask, or / for commands" aria-label="message"
    spellcheck="false"></textarea>
  <input type="file" id="attach-file" hidden>
  <button id="attach" type="button" title="attach a file — its contents
    are appended to the prompt on send">+</button>
  <button id="send">send</button>
</form>
<div id="overlay" hidden></div>
<div id="toast-host"></div>
<dialog id="form-dialog">
  <h3 id="form-title">a question for you</h3>
  <div id="form-fields" class="form-fields"></div>
  <div class="opt-actions">
    <button id="form-cancel">cancel</button>
    <button id="form-send" class="primary">answer</button>
  </div>
</dialog>
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
    <div id="img-negative-note" class="opt-faint" style="display:none;
      font-size:11px; margin-top:2px;">chroma treats short negatives as
      soft positive content — prefer describing what you DO want</div>
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
<dialog id="music-dialog">
  <h3>generate music</h3>
  <div class="img-scroll">
  <label class="opt-label">caption
    <textarea id="music-caption" rows="3"
      placeholder="genre, mood, instruments, vocal style"></textarea>
  </label>
  <label class="opt-label">lyrics <span class="opt-faint">(optional — tag
    [verse]/[chorus]; leave empty for an instrumental take)</span>
    <textarea id="music-lyrics" rows="4"
      placeholder="[verse]&#10;..."></textarea>
  </label>
  <div class="img-grid">
      <label class="opt-label">duration (seconds)
        <input id="music-duration" type="number" min="1" max="240" value="30">
      </label>
      <label class="opt-label">steps (flow)
        <input id="music-steps" type="number" min="10" max="100" value="30">
      </label>
      <label class="opt-label">guidance scale
        <input id="music-guidance" type="number" step="0.1" min="0.1" max="5.0" value="1.7">
      </label>
      <label class="opt-label">ar guidance
        <input id="music-ar-guidance" type="number" step="0.1" min="0.1" max="5.0" value="1.5">
      </label>
      <label class="opt-label">top-k
        <input id="music-topk" type="number" min="1" max="200" value="50">
      </label>
      <label class="opt-label">decode graph arena (MB)
        <input id="music-decode-arena" type="number" min="32" max="4096" value="2048">
      </label>
      <label class="opt-label">seed
        <input id="music-seed" type="number" min="0" placeholder="random">
      </label>
      <label class="opt-label">flow uncond interval
        <input id="music-flow-uncond" type="number" min="1" max="8" value="1">
      </label>
      <label class="opt-label">flow uncond warmup
        <input id="music-flow-warmup" type="number" min="0" max="10" value="2">
      </label>
      <label class="opt-label">quality
        <select id="music-quality">
          <option value="stock">stock — q4_0 flow (fast, steps saturate)</option>
          <option value="hq">hq — q8_0 flow + bf16 depth (slower, best)</option>
        </select>
      </label>
      </div>
    <div class="opt-faint" style="font-size:11px;">about two minutes of
      wait per ten seconds of audio on this GPU. Higher steps/guidance = better quality, slower.</div>
  </div>
  <div class="opt-actions">
    <button id="music-cancel">cancel</button>
    <button id="music-generate" class="primary">generate</button>
  </div>
</dialog>
<dialog id="history-dialog">
  <h3>chats</h3>
  <div class="hist-actions">
    <input id="hist-name" placeholder="name to save as" maxlength="60">
    <button id="hist-save">save current</button>
    <button id="hist-new">new chat</button>
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
    <label class="opt-label">reasoning</label>
    <select id="opt-reason">
      <option value="auto">auto — the model's default</option>
      <option value="on">on — ask it to reason</option>
      <option value="off">off — no reasoning tokens</option>
    </select>
  </div>
  <div class="opt-row">
    <label class="opt-label">model image tool</label>
    <label class="switch"><input type="checkbox" id="opt-imagetool"><span></span></label>
  </div>
  <div class="opt-row">
    <label class="opt-label">model music tool</label>
    <label class="switch"><input type="checkbox" id="opt-musictool"><span></span></label>
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
<dialog id="skills-dialog">
  <h3>skills & core</h3>
  <div id="skills-list" class="skills-list"></div>
  <details class="core-details">
    <summary>CORE.md (the identity this session speaks through)</summary>
    <pre id="core-text" class="core-text"></pre>
  </details>
  <details class="core-details" open>
    <summary>skill editor — click a skill above to edit it</summary>
    <div class="skill-edit-head">
      <span id="skill-edit-name" class="sk-name"></span>
      <button id="skill-new" class="act">+ new</button>
      <button id="skill-save" class="act">save</button>
      <button id="skill-delete" class="act">delete</button>
    </div>
    <textarea id="skill-body-edit" rows="14" spellcheck="false"
      placeholder="click a skill above to edit it, or + new. the raw SKILL.md: frontmatter (name, description, trigger keywords) then the procedure body."></textarea>
    <div id="skill-edit-status" class="sk-desc"></div>
  </details>
  <div class="opt-actions">
    <button id="skills-close">close</button>
  </div>
</dialog>
<script>
const THEMES = {
  "k0b0l-dark": {"--bg":"#0a0e14","--surface":"#11151d","--band":"#161b26","--ink":"#e6e9ef","--muted":"#8b93a3","--faint":"#5c6474","--accent":"#62e2c6","--accent-ink":"#062a22","--code-bg":"#131722","--think-bg":"#12141c","--warn":"#e2b962","--err":"#e2627a","--border":"#1d2330","--syn-k":"#c792ea","--syn-s":"#c3e88d","--syn-n":"#f78c6c","--syn-c":"#5c6474","--syn-f":"#82aaff"},
  "hotdog-3x": {"--bg":"#FFFF00","--surface":"#C0C0C0","--band":"#C0C0C0","--ink":"#000000","--muted":"#A00000","--faint":"#808080","--accent":"#FF0000","--accent-ink":"#FFFF00","--code-bg":"#FFFFFF","--think-bg":"#FFFFFF","--warn":"#FF0000","--err":"#A00000","--border":"#000000","--syn-k":"#FF0000","--syn-s":"#A00000","--syn-n":"#FF0000","--syn-c":"#808080","--syn-f":"#000000"},
  "beos": {"--bg":"#D8D8D8","--surface":"#DEDEDE","--band":"#E8E8E8","--ink":"#101010","--muted":"#3F3F3F","--faint":"#707070","--accent":"#0000E5","--accent-ink":"#FFFFFF","--code-bg":"#FFFFFF","--think-bg":"#E8E8E8","--warn":"#606030","--err":"#C00000","--border":"#ACACAC","--syn-k":"#0000E5","--syn-s":"#27528C","--syn-n":"#6698CB","--syn-c":"#9C9C9C","--syn-f":"#336698"},
  "commodore-64": {"--bg":"#2E2C9B","--surface":"#2E2C9B","--band":"#000000","--ink":"#FFFFFF","--muted":"#B2B2B2","--faint":"#7B7B7B","--accent":"#EDF171","--accent-ink":"#2E2C9B","--code-bg":"#000000","--think-bg":"#000000","--warn":"#EDF171","--err":"#FF6C76","--border":"#706DEB","--syn-k":"#EDF171","--syn-s":"#A9FF9F","--syn-n":"#FFFFFF","--syn-c":"#7B7B7B","--syn-f":"#75CEC8"},
  "edit-com": {"--bg":"#0000AA","--surface":"#0000AA","--band":"#0000AA","--ink":"#AAAAAA","--muted":"#55FFFF","--faint":"#5555FF","--accent":"#00AAAA","--accent-ink":"#000000","--code-bg":"#000000","--think-bg":"#000055","--warn":"#FFFF55","--err":"#FF5555","--border":"#00AAAA","--syn-k":"#FFFF55","--syn-s":"#55FF55","--syn-n":"#FFFFFF","--syn-c":"#5555FF","--syn-f":"#55FFFF"},
  "amber": {"--bg":"#000000","--surface":"#140D00","--band":"#2B1D00","--ink":"#FFB000","--muted":"#FFC46B","--faint":"#8A5F00","--accent":"#FFD9A0","--accent-ink":"#000000","--code-bg":"#140D00","--think-bg":"#2B1D00","--warn":"#E8A200","--err":"#FF7A00","--border":"#8A5F00","--syn-k":"#FFD9A0","--syn-s":"#FFB000","--syn-n":"#FFC46B","--syn-c":"#8A5F00","--syn-f":"#FFD9A0"},
  "matrix": {"--bg":"#000000","--surface":"#001A0A","--band":"#003B00","--ink":"#00FF41","--muted":"#7CFF9B","--faint":"#008F11","--accent":"#7CFF9B","--accent-ink":"#000000","--code-bg":"#001A0A","--think-bg":"#003B00","--warn":"#B6FF00","--err":"#FF3B3B","--border":"#008F11","--syn-k":"#7CFF9B","--syn-s":"#00FF41","--syn-n":"#7CFF9B","--syn-c":"#008F11","--syn-f":"#00FF41"},
};
const DEFAULT_PALETTE = {"--bg":"#0a0e14","--surface":"#11151d","--band":"#161b26","--ink":"#e6e9ef","--muted":"#8b93a3","--faint":"#5c6474","--accent":"#62e2c6","--accent-ink":"#062a22","--code-bg":"#131722","--think-bg":"#12141c","--warn":"#e2b962","--err":"#e2627a","--border":"#1d2330","--syn-k":"#c792ea","--syn-s":"#c3e88d","--syn-n":"#f78c6c","--syn-c":"#5c6474","--syn-f":"#82aaff"};
const ROOT_VARS = ["--bg","--surface","--band","--ink","--muted","--faint","--accent","--accent-ink","--code-bg","--think-bg","--warn","--err","--border","--syn-k","--syn-s","--syn-n","--syn-c","--syn-f"];
let ws = null;  // assigned by connectWS(); every handler re-attaches there
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
let currentChat = "";   // the chat this tab is bound to (server truth)
let whoEl = null;
let typingEl = null;
let busyTimer = null;
let busyStart = null;
let busy = false;
let busyActivity = "";  // last observable action, for the workbar label
let proseRaw = "";   // un-marked-up model text for the open prose block

function paintProse() {
  // Repaint the open prose block from its raw markdown on every delta
  // (chat streams partial lines; a partial **bold is still plain text
  // until its closing ** arrives, so render from source each time).
  if (!proseEl) { proseEl = el("div", "prose md"); }
  proseEl.innerHTML = renderMarkdown(proseRaw);
}

function setBusy(on) {
  // Remember the request across reconnects and element churn: a busy
  // frame can arrive before the assistant row exists (inference starts
  // before the turn header is drawn), and the input must never stay
  // wedged off because a turn element went away.
  busy = on;
  // The send button becomes CANCEL while a turn runs -- the operator's
  // request: not a greyed-out send, but an active stop control in the
  // same place. Enabled (not disabled) so it can be clicked.
  send.classList.toggle("cancel", on);
  send.disabled = false;
  send.textContent = on ? "cancel" : "send";
  if (typingEl) {
    typingEl.classList.toggle("busy", on);
  }
  if (whoEl) {
    whoEl.classList.toggle("busy", on);
  }
  if (on && !busyStart) {
    busyStart = Date.now();
    // The transcript-level workbar: the header dots are easy to miss
    // and say nothing about what the model is doing. This strip pulses
    // for the whole turn -- including the long silent stretches (a big
    // tool-call JSON being streamed inside a held fence, a minutes-long
    // GPU run) where previously nothing on the page moved at all.
    const bar = ensureGenbar();
    bar.classList.add("indeterminate");
    if (genLabel) {
      genLabel.textContent = "working · 0s · "
        + (busyActivity || "starting");
    }
    clearInterval(busyTimer);
    busyTimer = setInterval(() => {
      if (busyStart) {
        const secs = Math.round((Date.now() - busyStart) / 1000);
        if (typingEl) {
          const clock = typingEl.querySelector(".busyclock");
          if (clock) clock.textContent = secs + "s";
        }
        // The bar label carries the elapsed time and what last moved:
        // "working · 87s · streaming" distinguishes a live turn from a
        // wedged one at a glance.
        if (genLabel) {
          genLabel.textContent = "working · " + secs + "s · "
            + (busyActivity || "starting");
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
    // busy-off is a turn-level terminal signal: the live strip must
    // go with it (completion, cancel, failure -- every path).
    removeGenbar();
  }
}

function applyTheme(name) {
  if (name !== "k0b0l-dark" && !THEMES[name]) name = "k0b0l-dark";
  const palette = THEMES[name] || DEFAULT_PALETTE;
  for (const key of ROOT_VARS) {
    if (palette[key]) document.documentElement.style.setProperty(key, palette[key]);
  }
  currentTheme = name;
  const sel = document.getElementById("opt-theme");
  if (sel && sel.value !== name) sel.value = name;
}

function copyText(text) {
  // Clipboard where it works, execCommand as the fallback: a failure
  // surfaces as a toast instead of a silent no-op.
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => toast("copied"),
      () => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
}
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    toast("copied");
  } catch (err) {
    toast("copy failed — select the text manually");
  }
  ta.remove();
}
function toast(text) {
  // A toast is ephemeral chrome, NOT conversation: it never opens a
  // turn (a notice-style el would create a stray turn in the
  // transcript and pollute the cache/replay), and it self-removes.
  const host = document.getElementById("toast-host");
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  (host || document.body).appendChild(t);
  setTimeout(() => t.remove(), 2200);
}
function artifactButtons(frame, opts) {
  // The action row every artifact carries: copy (the text/result),
  // retry (re-send the request), and for images copy-prompt.
  ensureTurn();
  const bar = document.createElement("div");
  bar.className = "artifact-actions";
  if (opts.copy !== undefined) {
    const b = document.createElement("button");
    b.className = "act";
    b.textContent = "copy";
    b.addEventListener("click", () => copyText(opts.copy));
    bar.appendChild(b);
  }
  if (opts.retry) {
    const r = document.createElement("button");
    r.className = "act";
    r.textContent = "retry";
    r.addEventListener("click", () => {
      if (opts.initImage) {
        // img2img/inpaint: the dialog still holds the init image and
        // mask state -- prefill the fields and open it for a confirm
        openImageDialog({ model: opts.request && opts.request.model,
                          prompt: opts.request && opts.request.prompt });
      } else {
        ws.send(JSON.stringify({ image: opts.request }));
      }
    });
    bar.appendChild(r);
  }
  if (opts.copyPrompt) {
    const p = document.createElement("button");
    p.className = "act";
    // For images this carries the full request JSON (every knob);
    // for text artifacts it is the plain prompt.
    p.textContent = opts.copyPromptIsJson ? "copy request" : "copy prompt";
    p.addEventListener("click", () =>
      copyText(opts.copyPrompt));
    bar.appendChild(p);
  }
  turn.appendChild(bar);
}

// -- markdown: escape-first, vendored, chat-grade --------------------------
// Model prose is untrusted in this harness -- everything is HTML-escaped
// BEFORE any tag is inserted, inline code is stashed out of the other
// rules, and link hrefs only ever begin with http(s):// so a
// javascript: URL cannot ride through. No CDN: the page stays
// self-contained.
function escapeHtml(s) {
  // The entities are spelled with \\u escapes in the source so this
  // file's PAGE string never carries a bare ampersand or quote
  // entity -- Python would eat them at page-build time (the same
  // class of bug as the \\n that once killed the whole script).
  return String(s)
    .replace(/&/g, "\\u0026amp;")
    .replace(/</g, "\\u0026lt;")
    .replace(/>/g, "\\u0026gt;")
    .replace(/"/g, "\\u0026quot;");
}

function renderInline(s) {
  const stash = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => {
    stash.push("<code>" + c + "</code>");
    return "\u0000" + (stash.length - 1) + "\u0000";
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[\s(])_([^_\s][^_]*?)_(?=$|[\s).,;:!?])/g,
                "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
                '<a href="$2" target="_blank" ' +
                'rel="noopener noreferrer">$1</a>');
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => stash[Number(i)]);
  return s;
}

function renderMarkdown(src) {
  // Block pass. NOTE: text is escaped first, so ">" arrives as
  // \u0026gt; and the blockquote pattern must match that. The line
  // separator uses fromCharCode for the same reason as everywhere in
  // this file: a literal \\n inside the PAGE string is consumed by
  // Python at page-build time.
  const lines = escapeHtml(src)
    .split(String.fromCharCode(10));
  let html = "";
  let para = [];
  let list = null;
  let items = [];
  let quote = null;
  const cells = (row) => row.replace(/^\s*\|/, "").replace(/\|\s*$/, "")
    .split("|").map((c) => renderInline(c.trim()));
  const flushPara = () => {
    if (para.length) {
      html += "<p>" + para.map(renderInline).join("<br>") + "</p>";
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      html += "<" + list + ">" +
        items.map((i) => "<li>" + renderInline(i) + "</li>").join("") +
        "</" + list + ">";
      list = null; items = [];
    }
  };
  const flushQuote = () => {
    if (quote) {
      html += "<blockquote>" +
        quote.map(renderInline).join("<br>") + "</blockquote>";
      quote = null;
    }
  };
  const flushAll = () => { flushPara(); flushList(); flushQuote(); };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flushAll();
      const n = Math.min(4, m[1].length);
      html += "<h" + n + ">" + renderInline(m[2]) + "</h" + n + ">";
    } else if (/^\s*(?:---+|\*\*\*)\s*$/.test(line)) {
      flushAll(); html += "<hr>";
    } else if (/^\u0026gt;\s?(.*)$/.test(line)) {
      flushPara(); flushList();
      quote = quote || [];
      quote.push(/^\u0026gt;\s?(.*)$/.exec(line)[1]);
    } else if (/^\s*[-*]\s+(.*)$/.test(line)) {
      flushPara(); flushQuote();
      if (list !== "ul") { flushList(); list = "ul"; }
      items.push(/^\s*[-*]\s+(.*)$/.exec(line)[1]);
    } else if (/^\s*\d+\.\s+(.*)$/.test(line)) {
      flushPara(); flushQuote();
      if (list !== "ol") { flushList(); list = "ol"; }
      items.push(/^\s*\d+\.\s+(.*)$/.exec(line)[1]);
    } else if (line.indexOf("|") >= 0 && i + 1 < lines.length &&
               /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1])) {
      // a GFM table: header row, separator row, body rows
      flushAll();
      const head = cells(line);
      i += 2;
      let body = "";
      while (i < lines.length && lines[i].indexOf("|") >= 0) {
        const row = cells(lines[i]);
        body += "<tr>" +
          row.map((c) => "<td>" + c + "</td>").join("") + "</tr>";
        i++;
      }
      i--;  // the outer loop increments
      html += "<table><thead><tr>" +
        head.map((c) => "<th>" + c + "</th>").join("") +
        "</tr></thead><tbody>" + body + "</tbody></table>";
    } else if (!line.trim()) {
      flushAll();
    } else {
      flushList(); flushQuote();
      para.push(line);
    }
  }
  flushAll();
  return html || "<p></p>";
}

function newTurn() {
  turn = document.createElement("div");
  turn.className = "turn";
  transcript.appendChild(turn);
}
// -- the conversation graph (client side) -----------------------------------
// Each turn maps to a server node id (arrives in the tree frames). The
// branch button under a turn forks the context at that point; sibling
// branches collapse because only one chain is active at a time.
let TREE_STATE = {nodes: [], active: null};
function turnNodeOf(turnEl) {
  return turnEl ? parseInt(turnEl.getAttribute("data-node") || "0", 10) : 0;
}
function addBranchButton(turnEl) {
  if (turnEl.querySelector(".branch-btn")) return;
  const btn = document.createElement("button");
  btn.className = "act branch-btn";
  btn.textContent = "🌿 branch";
  btn.title = "fork the context here: the new path keeps everything " +
              "up to this point, and later turns stay on the old path";
  btn.addEventListener("click", () => {
    const nid = turnNodeOf(turnEl);
    if (!nid) return;
    ws.send(JSON.stringify({ tree_branch: { from: nid } }));
  });
  // the button lives in the turn's action bar if one exists, else its own row
  const bar = turnEl.querySelector(".artifact-actions");
  if (bar) { bar.appendChild(btn); return; }
  const row = document.createElement("div");
  row.className = "artifact-actions turn-actions";
  row.appendChild(btn);
  turnEl.appendChild(row);
}
function applyTreeState(m) {
  TREE_STATE = {nodes: m.nodes || [], active: m.active};
  // mark turns with their node ids: turns are appended in node order,
  // and the tree's node list carries each node's user text -- match by
  // sequence of turns on the active chain first, then siblings.
  const turns = Array.from(document.querySelectorAll(".turn"));
  // nodes in discovery order correspond to turns that were RENDERED;
  // a repaint after a switch re-renders only the active chain, so map
  // by the active chain first.
  const active = (m.nodes || []).filter(n => n.active_chain);
  turns.forEach((t, i) => {
    const n = active[i];
    if (n) t.setAttribute("data-node", String(n.id));
    addBranchButton(t);
  });
  // sibling indicator: if any node has siblings (same parent, later
  // id than the active one), show a switch hint
  const hint = document.getElementById("tree-hint");
  if (hint) {
    const siblings = (m.nodes || []).filter(
      n => n.parent !== null && (m.nodes || []).some(
        o => o.parent === n.parent && o.id !== n.id));
    hint.style.display = siblings.length ? "block" : "none";
    hint.textContent = siblings.length
      ? "this conversation has " + siblings.length +
        " branch point(s); 🌿 on a turn forks it"
      : "";
  }
}
function renderBranchSwitcher() {
  // sibling branches of the active node's ancestors become clickable
  // chips: only one chain active at a time
  const holder = document.getElementById("branch-switcher");
  if (!holder) return;
  holder.innerHTML = "";
  if (!TREE_STATE.nodes.length) return;
  const byId = {};
  TREE_STATE.nodes.forEach(n => { byId[n.id] = n; });
  // find every node that has a sibling (same parent, different node)
  const groups = {};
  TREE_STATE.nodes.forEach(n => {
    const key = String(n.parent);
    (groups[key] = groups[key] || []).push(n);
  });
  const chips = [];
  Object.values(groups).forEach(g => {
    if (g.length < 2) return;
    g.sort((a, b) => a.id - b.id).forEach(n => {
      const chip = document.createElement("button");
      chip.className = "branch-chip" +
        (n.active_chain ? " on" : "");
      chip.textContent = "#" + n.id + " " +
        (n.user ? n.user.slice(0, 24) : "(branch)");
      chip.title = "switch context to this branch";
      chip.addEventListener("click", () => {
        ws.send(JSON.stringify({ tree_switch: { node: n.id } }));
      });
      chips.push(chip);
    });
  });
  if (chips.length) {
    const label = document.createElement("span");
    label.textContent = "branches:";
    label.className = "branch-label";
    holder.appendChild(label);
    chips.forEach(c => holder.appendChild(c));
  }
}
function ensureTurn() {
  // Slash commands and late frames can arrive with no open turn (the
  // server sends no "assistant" row for /image, and a notice can land
  // after turn_end). Rendering into a null turn throws and silently
  // kills every later frame in the batch -- the "stalled" webui.
  if (!turn) newTurn();
}

function ensureGenbar() {
  // The one progress strip per turn: created by a gen_progress frame
  // (image/music runs) or by the busy indicator (plain model turns),
  // whichever comes first -- the second merely reuses it.
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
  return genBar;
}

function removeGenbar() {
  // The strip is a LIVE indicator only: it must never linger in the
  // transcript after the artifact landed, the run failed, or the turn
  // ended -- a leftover pulsing bar reads as a stalled generation.
  if (genBar) {
    const wrap = genBar.closest(".genbar-wrap");
    if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
    genBar = null;
    genLabel = null;
  }
}
function who(name, cls) {
  ensureTurn();
  const row = document.createElement("div");
  row.className = "who " + (cls || "");
  row.textContent = name;
  // Every turn (user and model alike) carries a copy icon: it copies
  // the turn's whole text content -- the operator's prompt, or the
  // model's full answer including code blocks -- to the clipboard.
  // MYTURN is the turn this row belongs to: the module-level `turn`
  // variable moves on with later frames, so closing over it directly
  // would copy from whatever turn is rendering when the click lands.
  const myTurn = turn;
  const turnText = () => {
    // The turn's text: every content block in it, joined by newlines.
    // The who row (label + buttons + the typing dots it anchors) and
    // the action bars are chrome, not content -- skip them whole.
    const parts = [];
    for (const child of myTurn.children) {
      if (child === row) continue;
      const cls2 = String(child.className || "");
      if (cls2.includes("who") || cls2.includes("artifact-actions")) continue;
      if (cls2.includes("think") || cls2.includes("genbar")) continue;
      parts.push(child.textContent || "");
    }
    return parts.join(String.fromCharCode(10)).trim();
  };
  const cp = document.createElement("button");
  cp.className = "who-copy";
  cp.textContent = "⧉";
  cp.title = "copy this message";
  cp.addEventListener("click", () => copyText(turnText()));
  row.appendChild(cp);
  // Retry: re-send what produced this turn. A user turn re-sends its
  // own text; an assistant turn re-sends the prompt that led to it.
  if (name === "you" || name === "model" ||
      name === "model (thinking)") {
    const rt = document.createElement("button");
    rt.className = "who-copy";
    rt.textContent = "↻";
    rt.title = "retry: send again";
    rt.addEventListener("click", () => {
      let payload = null;
      if (name === "you") {
        payload = turnText();
      } else {
        // the user turn BEFORE this assistant turn, transcript order
        const turns = Array.from(transcript.children);
        const at = turns.indexOf(myTurn);
        for (let i = at - 1; i >= 0; i--) {
          const prevYou = turns[i].querySelector(".who.you");
          if (prevYou) {
            const parts = [];
            for (const child of turns[i].children) {
              const cls2 = String(child.className || "");
              if (cls2.includes("who") || cls2.includes("artifact-actions")) continue;
              if (cls2.includes("think") || cls2.includes("genbar")) continue;
              parts.push(child.textContent || "");
            }
            payload = parts.join(String.fromCharCode(10)).trim();
            break;
          }
        }
      }
      if (payload) ws.send(JSON.stringify({ text: payload }));
      else toast("nothing to retry");
    });
    row.appendChild(rt);
  }
  turn.appendChild(row);
  return row;  // the assistant handler anchors the dots to this element
}
// -- attach: a file's contents ride with the prompt on send -----------
// The + button opens a picker; the chosen file is read as TEXT (the
// harness's chat works in text -- a binary would be gibberish in the
// prompt and the model-visible surface is the message itself). The
// send handler appends it as a fenced block, so the model sees the
// file as part of the message and the transcript shows the message as
// it was sent. Images route through the img2img dialog instead.
let attachedFile = null;   // {name, text}
const attachBtn = document.getElementById("attach");
const attachFileEl = document.getElementById("attach-file");
attachBtn.addEventListener("click", () => attachFileEl.click());
attachFileEl.addEventListener("change", () => {
  const file = attachFileEl.files && attachFileEl.files[0];
  attachFileEl.value = "";   // allow re-picking the same file
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    attachedFile = { name: file.name, text: String(reader.result || "") };
    attachBtn.classList.add("has-file");
    attachBtn.title = "attached " + file.name + " — it is appended to " +
      "the prompt on send; click to replace";
    toast("attached " + file.name + " (" +
      Math.round(file.size / 102.4) / 10 + " KB)");
  };
  reader.onerror = () => toast("could not read " + file.name);
  reader.readAsText(file);
});
function consumeAttached() {
  // The prompt plus its attachment, or null when nothing is attached.
  if (!attachedFile) return null;
  const out = attachedFile;
  attachedFile = null;
  attachBtn.classList.remove("has-file");
  attachBtn.title = "attach a file — its contents are appended to the " +
    "prompt on send";
  return out;
}
input.addEventListener("input", () => {
  // Autosize: the textarea grows with its content up to the CSS cap
  // (max-height); a fixed rows=3 alone would SCROLL long drafts
  // instead of expanding, and the operator asked for an expanding bar.
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight,
    parseInt(getComputedStyle(input).maxHeight, 10) || 224) + "px";
});
input.addEventListener("keydown", (e) => {
  // Enter sends; Shift+Enter is a real newline now that the input is a
  // textarea (it was impossible to type one before).
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    document.getElementById("form").requestSubmit();
  }
});

// -- scroll: follow only when the operator is already at the bottom ----
// Forced autoscroll on every delta yanks the operator out of the
// backlog they are reading while an answer streams. The transcript
// now follows only when they are near the bottom (within ~a
// screenful); anywhere else, new content lands silently below.
// forceScroll marks moments the operator asked for: their own send,
// and a fresh connect repaint. A scroll-up cancels the follow until
// they return to the bottom. (Declared BEFORE scroll() -- a let is
// TDZ-hoisted and the replay calls scroll() long before this block's
// neighbours finish evaluating.)
let forceScroll = false;
transcript.addEventListener("scroll", () => {
  if (!nearBottom()) forceScroll = false;
});
function nearBottom() {
  return transcript.scrollHeight - transcript.scrollTop
    - transcript.clientHeight < 160;
}
function scroll() {
  if (forceScroll || nearBottom()) {
    transcript.scrollTop = transcript.scrollHeight;
  }
}
function el(tag, cls, text) {
  ensureTurn();
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  turn.appendChild(e);
  return e;
}

let wsHandlersBound = null;
function wireSocket() {
  // The frame handler lives ONCE: reconnects swap the socket object,
  // and this re-attaches to whatever connectWS() put in `ws`.
  ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  switch (m.kind) {
    case "meta":
      currentChat = m.chat || currentChat;
      meta.textContent = m.model + " · " + m.protocol + " · ctx "
        + m.num_ctx + (m.chat ? " · " + m.chat : "");
      applyTheme(m.theme || "k0b0l-dark");
      // the reasoning select reflects the server's truth (a fresh
      // connect, a /reason from another tab, a restart)
      const rs = document.getElementById("opt-reason");
      if (rs && m.reasoning) rs.value = m.reasoning;
      const itb = document.getElementById("opt-imagetool");
      if (itb && m.imagetool !== undefined) itb.checked = m.imagetool;
      const mtb = document.getElementById("opt-musictool");
      if (mtb && m.musictool !== undefined) mtb.checked = m.musictool;
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
      proseEl = null; proseRaw = ""; thinkEl = null; genBar = null; genLabel = null;
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
      // Replayed busy frames are history, not a live run: painting the
      // workbar from them litters dead pulsing strips across restored
      // turns. The replay's turn_end frames close the turns; live runs
      // still get the bar from setBusy's own ensureGenbar call.
      if (restoring) break;
      setBusy(m.on);
      break;
    case "delta":
      busyActivity = "streaming";
      // Immediate label refresh: the 1 s busy tick is too coarse to
      // prove liveness by itself -- the activity word must move the
      // moment a frame lands.
      if (genLabel && busyStart) {
        genLabel.textContent = "working · "
          + Math.round((Date.now() - busyStart) / 1000) + "s · streaming";
      }
      if (!proseEl) { proseEl = el("div", "prose md"); }
      proseRaw += m.text;
      paintProse(); scroll();
      break;
    case "thinking":
      busyActivity = "thinking";
      if (genLabel && busyStart) {
        genLabel.textContent = "working · "
          + Math.round((Date.now() - busyStart) / 1000) + "s · thinking";
      }
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
      busyActivity = "tool: " + (m.text || "").split("(")[0];
      if (genLabel && busyStart) {
        genLabel.textContent = "working · "
          + Math.round((Date.now() - busyStart) / 1000) + "s · "
          + busyActivity;
      }
      proseEl = null; proseRaw = ""; thinkEl = null;
      el("div", "tool call", m.text); scroll(); break;
    case "tool_result":
      busyActivity = "tool result";
      const r = el("div", "tool result" + (m.ok ? "" : " error"));
      r.textContent = m.text;
      proseEl = null; proseRaw = ""; thinkEl = null; scroll(); break;
    case "code":
      ensureTurn();
      const cd = document.createElement("details");
      cd.className = "code";
      cd.open = true;
      const sum = document.createElement("summary");
      const sumLang = document.createElement("span");
      sumLang.textContent = m.language || "code";
      sum.appendChild(sumLang);
      const codeCopy = document.createElement("button");
      codeCopy.className = "code-copy";
      codeCopy.textContent = "⧉ copy";
      codeCopy.title = "copy this code block";
      codeCopy.addEventListener("click", (ev) => {
        ev.preventDefault();   // a summary click would toggle the fold
        ev.stopPropagation();
        copyText(m.text || "");
      });
      sum.appendChild(codeCopy);
      const bd = document.createElement("div"); bd.className = "body";
      const pre = document.createElement("pre"); pre.className = "code";
      pre.textContent = m.text;
      bd.appendChild(pre); cd.appendChild(sum); cd.appendChild(bd);
      turn.appendChild(cd);
      proseEl = null; proseRaw = ""; thinkEl = null; scroll(); break;
    case "notice":
      el("div", "notice", m.text); scroll(); break;
    case "error":
      removeGenbar();  // a failed run must not leave a pulsing strip behind
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
      // The action bar under the artifact. For an image, "copy" means
      // the PROMPT that made it -- that is what the operator wants on
      // their clipboard; the filename is already visible in the
      // caption. "copy prompt" stays for explicitness, and retry
      // re-sends the full request.
      const req = m.request || null;
      const promptText = req ? String(req.prompt || "")
        : (m.path || "");
      artifactButtons(m, {
        copy: promptText,
        retry: !!req,
        request: req,
        initImage: !!(req && (req.init_image_b64 || req.mode !== "txt2img")),
        copyPrompt: req ? JSON.stringify(req, null, 2) : undefined,
        copyPromptIsJson: !!req,
      });
      removeGenbar();  // the artifact is the outcome; the live strip goes
      proseEl = null; proseRaw = ""; thinkEl = null;
      genBar = null;
      scroll();
      break;
    case "audio":
      ensureTurn();
      {
        const fig = document.createElement("figure");
        const aud = document.createElement("audio");
        aud.className = "gen gen-audio";
        aud.controls = true;
        aud.preload = "metadata";
        aud.src = "/image/" + encodeURIComponent(m.path);
        fig.appendChild(aud);
        const cap = document.createElement("figcaption");
        cap.textContent = m.path + (m.label ? " · " + m.label : "");
        fig.appendChild(cap);
        turn.appendChild(fig);
        // Copy carries the caption+lyrics that made the clip -- the
        // human-usable content, never the filename (the path is in the
        // caption already). Retry re-sends the request.
        const req = m.request || null;
        const captionText = req
          ? String(req.caption || "") +
            (req.lyrics ? String.fromCharCode(10) + String.fromCharCode(10)
              + String(req.lyrics) : "")
          : (m.path || "");
        artifactButtons(m, {
          copy: captionText,
          retry: !!req,
          request: req,
        });
      }
      removeGenbar();  // the clip is the outcome; the live strip goes
      proseEl = null; proseRaw = ""; thinkEl = null;
      genBar = null;
      scroll();
      break;
    case "gen_progress":
      busyActivity = m.phase || "generating";
      if (restoring) break;  // history: a dead progress bar is noise
      ensureGenbar();
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
      removeGenbar();  // terminal on every path: done, cancelled, failed
      setBusy(false);
      turn = null; proseEl = null; proseRaw = ""; thinkEl = null;
      whoEl = null; typingEl = null;
      busyActivity = "";
      forceScroll = false;   // a finished turn ends the forced follow
      input.focus();
      persistCache();
      break;
    case "restore_begin":
      // Server truth incoming: drop the localStorage guess so the
      // restored transcript is painted exactly once.
      restoring = true;
      transcript.innerHTML = "";
      turn = null; proseEl = null; proseRaw = ""; thinkEl = null;
      whoEl = null; typingEl = null; genBar = null; genLabel = null;
      break;
    case "restore_end":
      restoring = false;
      // A connect repaint is a moment the operator wants to see the
      // latest: follow to the bottom once, then normal rules apply.
      forceScroll = true;
      scroll();
      persistCache();
      break;
    case "clear":
      transcript.innerHTML = "";
      turn = null; proseEl = null; proseRaw = ""; thinkEl = null;
      whoEl = null; typingEl = null; genBar = null; genLabel = null;
      // a cleared conversation cannot answer its pending question:
      // close the dialog if it belongs to this chat
      if (formDialog.open && (!formDialog._chat ||
          formDialog._chat === currentChat)) { formDialog.close(); }
      try { localStorage.removeItem(CACHE_KEY); } catch (err) {}
      break;
    case "session_list":
      renderSessionList(m.sessions || []);
      break;
    case "ask_form":
      // the model asked the operator to fill a form: a modal dialog
      // opens on every tab; answering sends form_answer (any tab).
      openAskForm(m);
      break;
    case "ask_form_close":
      // answered (or cancelled) elsewhere: close the dialog here too
      if (formDialog.open) { formDialog.close(); }
      break;
    case "skills":
      renderSkills(m.skills || [], m.active || []);
      break;
    case "skill_text":
      // A skill's raw SKILL.md opened in the editor. An error still
      // fills the editor (the panel stays usable for a re-save).
      document.getElementById("skill-body-edit").value =
        m.text || "";
      document.getElementById("skill-edit-name").textContent =
        m.name || "(new skill)";
      skillEdit.name = m.name || "";
      skillEdit.create = !!m.new;
      skillStatus(m.error
        ? m.error + " — the editor holds a starting template"
        : "loaded " + (m.name || "") + " — edit and save", !!m.error);
      break;
    case "skill_saved":
      if (m.ok) {
        skillStatus((m.deleted ? "deleted " + m.deleted
          : "saved " + (m.name || "")) + (m.path ? " — " + m.path : ""));
        // refresh the list + keep the editor honest for a follow-up
        setTimeout(() => ws.send(JSON.stringify({ skills_list: true })), 300);
        if (m.deleted) {
          document.getElementById("skill-body-edit").value = "";
          document.getElementById("skill-edit-name").textContent = "";
          skillEdit.name = "";
          skillEdit.create = false;
        } else if (m.name) {
          skillEdit.name = m.name;
          skillEdit.create = false;
        }
      } else {
        skillStatus(m.error || "not saved", true);
      }
      break;
    case "core":
      document.getElementById("core-text").textContent =
        m.loaded ? (m.text || "(empty)") : "(no CORE.md loaded)";
      break;
    case "tree":
      applyTreeState(m);
      renderBranchSwitcher();
      break;
    case "raw":
      // unhandled line kinds (slash-command output) render as muted prose
      el("div", "prose", m.text); scroll(); break;
    case "pong":
      // the heartbeat's echo: proof the socket is alive from the
      // server's side too (see the freezer defense below)
      lastPong = Date.now();
      break;
  }
};
}
// -- socket lifecycle: survive Android's background freezer ---------------
// Android Chrome suspends the tab when it backgrounds: timers freeze and
// the socket dies silently. Without this block, coming back meant a
// stalled page (no reconnect, frames lost) and a manual refresh -- which
// used to wipe the transcript (now the server carries history across).
// Three defenses:
//   1. heartbeat: a ping every 20s; no pong in 45s = treat as dead
//   2. reconnect: on close/stall, exponential backoff up to 30s, forever
//   3. visibilitychange: on foreground, force an immediate resync --
//      the socket may be "open" but frozen; reconnect unconditionally
let wsConnected = false;
let lastPong = Date.now();
let reconnectDelay = 1000;
let reconnectTimer = null;

function connectWS() {
  ws = new WebSocket(
    (location.protocol === "https:" ? "wss://" : "ws://")
    + location.host + "/ws");
  wireSocket();
  ws.onopen = () => {
    wsConnected = true;
    lastPong = Date.now();
    reconnectDelay = 1000;
    meta.textContent = "connected";
    input.focus();
  };
  ws.onclose = () => {
    wsConnected = false;
    meta.textContent = "reconnecting…";
    setBusy(false);  // also resets the button to "send"
    scheduleReconnect();
  };
  ws.onerror = () => { /* onclose follows */ };
}
function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWS();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 30000);
}

// Heartbeat: the server echoes pings; silence means a frozen socket
// even when the browser believes it is open (Android's freezer).
setInterval(() => {
  if (!wsConnected) return;
  if (Date.now() - lastPong > 45000) {
    try { ws.close(); } catch (e) { /* already dead */ }
    return;
  }
  try { ws.send(JSON.stringify({"ping": 1})); } catch (e) { /* dying */ }
}, 20000);

// Foregrounding the tab: force a reconnect NOW -- the frozen socket
// is indistinguishable from an open one until it times out.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    if (!wsConnected || Date.now() - lastPong > 20000) {
      try { ws.close(); } catch (e) { /* already dead */ }
    }
    input.focus();
  }
});
connectWS();

document.getElementById("form").addEventListener("submit", (e) => {
  e.preventDefault();
  // While a generation runs the button reads CANCEL and stops the run;
  // a message typed during the run stays in the input, not lost.
  if (busy) {
    try { ws.send(JSON.stringify({"cancel": true})); } catch (err) {}
    return;
  }
  const typed = input.value.trim();
  // The attachment rides with the prompt: the file's text is appended
  // as a fenced block so the model sees it as part of the message.
  const attach = consumeAttached();
  const text = typed + (attach
    ? (typed ? String.fromCharCode(10) : "")
      + "attached file: " + attach.name + String.fromCharCode(10)
      + "```" + String.fromCharCode(10) + attach.text
      + String.fromCharCode(10) + "```"
    : "");
  if (!text) return;
  // A bare "/image" (optionally "<model> <prompt>") opens the image
  // dialog instead of sending the stripped-down command: the dialog is
  // the full surface (negative prompt, size, steps, seed).
  if (!attach && (text === "/image" || text.startsWith("/image "))) {
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
  // A bare "/music" (optionally "<caption>") opens the music dialog
  // instead of sending the command: same surface rule as /image.
  if (!attach && (text === "/music" || text.startsWith("/music "))) {
    const rest = text.slice("/music".length).trim();
    input.value = "";
    openMusicDialog({ caption: rest });
    return;
  }
  input.value = "";
  forceScroll = true;   // the operator's own send always follows
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
    turn = null; proseEl = null; proseRaw = ""; thinkEl = null;
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
    empty.textContent = "no chats yet";
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
    sub.textContent = (s.live ? "● open · " : "") +
      (s.asking ? "❓ asking · " : "") +
      (s.turns || 0) + " turns · " +
      (s.messages || 0) + " msgs · " + when +
      (s.preview ? " · " + s.preview : "");
    main.appendChild(nameEl); main.appendChild(sub);
    row.appendChild(main);
    const loadBtn = document.createElement("button");
    loadBtn.textContent = "open";
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
      if (s.name === currentChat) {
        // the conversation this tab is viewing is gone server-side:
        // drop the local copy too, or a repaint resurrects it
        try { localStorage.removeItem(CACHE_KEY); } catch (err) {}
        transcript.innerHTML = "";
        turn = null; proseEl = null; proseRaw = ""; thinkEl = null;
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

// Per-model known-good presets, from each model's upstream docs. The
// dialog pre-fills from these when the model changes, so an untouched
// dialog generates something good instead of a half-denoised weave:
// a Flux-class model at 6 steps / 512² / the Flux2 schedule is
// exactly what undercooked output looks like.
const MODEL_PRESETS = {
  pony:   { steps: 24, cfg: 7, size: "1024x1024",
            sampler: "euler", scheduler: "karras" },
  qwen:   { steps: 30, cfg: 4, size: "1024x1024",
            sampler: "euler", scheduler: "simple" },
  chroma: { steps: 28, cfg: 4, size: "1024x1024",
            sampler: "euler", scheduler: "flux" },
};
// "cfg 4" for chroma matches its upstream doc example; pony's SDXL
// defaults want more guidance (7); qwen's DiT prefers low cfg (4).

function applyModelPreset(model) {
  // Fill the knobs with the model's known-good values. Only the
  // technical knobs -- never the prompt, negative, or seed (seed -1
  // stays random). An operator who already chose something keeps it:
  // presets only land when the field is at its default/empty state.
  const note = document.getElementById("img-negative-note");
  if (note) {
    // chroma's negative-prompt semantics are unreliable (a Flux-Schnell
    // finetune: short negatives read as soft positive content) -- say so
    // in the field instead of letting it look broken.
    note.style.display = model === "chroma" ? "block" : "none";
  }
  const p = MODEL_PRESETS[model] || null;
  if (!p) return;
  const sizeSel = document.getElementById("img-size");
  const stepsEl = document.getElementById("img-steps");
  const cfgEl = document.getElementById("img-cfg");
  const samplerSel = document.getElementById("img-sampler");
  const schedSel = document.getElementById("img-scheduler");
  if (!stepsEl.value) stepsEl.value = String(p.steps);
  if (!cfgEl.value) cfgEl.value = String(p.cfg);
  if (sizeSel.value === "" || sizeSel.value === "1024x1024"
      || sizeSel.selectedIndex <= 0) {
    sizeSel.value = p.size;
  }
  if (samplerSel.value === "" || samplerSel.value === "euler") {
    samplerSel.value = p.sampler;
  }
  if (schedSel.value === "" || schedSel.value === "karras") {
    schedSel.value = p.scheduler;
  }
}

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
  applyModelPreset(document.getElementById("img-model").value);
  document.getElementById("img-prompt").focus();
}

document.getElementById("img-model").addEventListener("change", () => {
  applyModelPreset(document.getElementById("img-model").value);
});

document.getElementById("image-btn").addEventListener("click",
  () => openImageDialog(null));
document.getElementById("img-cancel").addEventListener("click",
  () => imageDialog.close());

const musicDialog = document.getElementById("music-dialog");
function openMusicDialog(prefill) {
  if (prefill && prefill.caption) {
    document.getElementById("music-caption").value = prefill.caption;
  }
  musicDialog.showModal();
  document.getElementById("music-caption").focus();
}
document.getElementById("music-btn").addEventListener("click",
  () => openMusicDialog(null));
document.getElementById("music-cancel").addEventListener("click",
  () => musicDialog.close());
document.getElementById("music-generate").addEventListener("click", () => {
  const caption = document.getElementById("music-caption").value.trim();
  if (!caption) {
    document.getElementById("music-caption").focus();
    return;
  }
  const payload = {
    caption: caption,
    lyrics: document.getElementById("music-lyrics").value.trim(),
    duration: parseInt(
      document.getElementById("music-duration").value, 10) || 30,
    steps: parseInt(
      document.getElementById("music-steps").value, 10) || 30,
    guidance_scale: parseFloat(
      document.getElementById("music-guidance").value) || 1.7,
    ar_guidance_scale: parseFloat(
      document.getElementById("music-ar-guidance").value) || 1.5,
    top_k: parseInt(
      document.getElementById("music-topk").value, 10) || 50,
    decode_graph_arena_mb: parseInt(
      document.getElementById("music-decode-arena").value, 10) || 2048,
    seed: parseInt(
      document.getElementById("music-seed").value, 10) || 0,
    flow_uncond_interval: parseInt(
      document.getElementById("music-flow-uncond").value, 10) || 1,
    flow_uncond_warmup: parseInt(
      document.getElementById("music-flow-warmup").value, 10) || 2,
    quality: document.getElementById("music-quality").value || "stock",
  };
  musicDialog.close();
  ws.send(JSON.stringify({ music: payload }));
});
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
document.getElementById("hist-new").addEventListener("click", () => {
  ws.send(JSON.stringify({ session: { verb: "new" } }));
});
document.getElementById("hist-save").addEventListener("click", () => {
  const box = document.getElementById("hist-name");
  ws.send(JSON.stringify({ session: { verb: "save",
    name: box.value.trim() || "web-session" } }));
  box.value = "";
  setTimeout(() => ws.send(JSON.stringify({ session: { verb: "list" } })),
    200);
});

// -- skills & core panel (🧠) ---------------------------------------------
function openSkills() {
  document.getElementById("skills-dialog").showModal();
  ws.send(JSON.stringify({ skills_list: true }));
  ws.send(JSON.stringify({ core_show: true }));
}
function renderSkills(skills, active) {
  const list = document.getElementById("skills-list");
  list.innerHTML = "";
  if (!skills.length) {
    const empty = document.createElement("div");
    empty.className = "sess-empty";
    empty.textContent = "no skills discovered (workspace skills/ or ~/.k0b0l-skills)";
    list.appendChild(empty);
  }
  for (const sk of skills) {
    const row = document.createElement("div");
    row.className = "skill-row";
    const name = document.createElement("div");
    name.className = "sk-name";
    name.textContent = sk.name;
    const desc = document.createElement("div");
    desc.className = "sk-desc";
    desc.textContent = sk.description || "";
    const btn = document.createElement("button");
    const on = active.includes(sk.name);
    btn.textContent = on ? "on" : "off";
    btn.className = on ? "on" : "";
    btn.title = on ? "pinned into every turn -- click to disable"
                   : "available on demand -- click to pin into every turn";
    btn.addEventListener("click", () => {
      ws.send(JSON.stringify({ skills_set: { name: sk.name,
        enabled: !on } }));
      // re-request the list once the server confirms, so the state
      // shown is the server's, not the click's guess
      setTimeout(() => ws.send(JSON.stringify({ skills_list: true })), 400);
    });
    // clicking the row opens the skill in the editor below
    row.addEventListener("click", () => {
      ws.send(JSON.stringify({ skills_read: sk.name }));
      skillEdit.name = sk.name;
      skillEdit.create = false;
      // make sure the editor is in view -- a collapsed details block
      // hid the edit/delete surface entirely (the operator's report)
      const details = document.getElementById("skill-body-edit")
        .closest("details");
      if (details) details.open = true;
      document.getElementById("skill-edit-name").textContent = sk.name;
      document.getElementById("skill-edit-status").textContent =
        "loading " + sk.name + "...";
    });
    row.appendChild(name); row.appendChild(desc); row.appendChild(btn);
    list.appendChild(row);
  }
}

// -- the skill editor (create / edit / delete skills in-panel) --------
// NOTE: no backslash escapes anywhere in this block -- the PAGE is a
// Python string, and a backslash-n inside these literals would be
// consumed by Python at page-build time, leaving a raw newline in the
// JS: a SyntaxError that kills every handler on the page (the
// historical "prompts vanish" failure). Newlines come from charCode 10.
const skillEdit = { name: "", create: false };
const NL = String.fromCharCode(10);
const SKILL_TEMPLATE = ["---",
  "name: my-skill",
  "description: One line saying when this skill applies.",
  "trigger:",
  "  keywords: []",
  "requires: []",
  "---",
  "",
  "# My Skill",
  "",
  "1. First step of the procedure.",
  "2. Second step.",
  ""].join(NL);
function skillStatus(text, isError) {
  const el = document.getElementById("skill-edit-status");
  el.textContent = text;
  el.style.color = isError ? "var(--err)" : "";
}
document.getElementById("skill-new").addEventListener("click", () => {
  skillEdit.name = "";
  skillEdit.create = true;
  document.getElementById("skill-edit-name").textContent = "(new skill)";
  const body = document.getElementById("skill-body-edit");
  body.value = SKILL_TEMPLATE;
  skillStatus("new skill — edit the frontmatter and body, then save " +
    "(it is created under the workspace skills/ dir)");
  body.focus();
});
document.getElementById("skill-save").addEventListener("click", () => {
  const text = document.getElementById("skill-body-edit").value;
  ws.send(JSON.stringify({ skills_write: {
    name: skillEdit.name, text: text, create: skillEdit.create } }));
  skillStatus("saving...");
});
document.getElementById("skill-delete").addEventListener("click", () => {
  if (!skillEdit.name || skillEdit.create) {
    skillStatus("nothing loaded to delete", true);
    return;
  }
  if (!document.getElementById("skill-delete").dataset.confirmed) {
    document.getElementById("skill-delete").dataset.confirmed = "1";
    document.getElementById("skill-delete").textContent = "sure?";
    setTimeout(() => {
      document.getElementById("skill-delete").dataset.confirmed = "";
      document.getElementById("skill-delete").textContent = "delete";
    }, 2500);
    return;
  }
  document.getElementById("skill-delete").dataset.confirmed = "";
  document.getElementById("skill-delete").textContent = "delete";
  ws.send(JSON.stringify({ skills_delete: { name: skillEdit.name } }));
  skillStatus("deleting " + skillEdit.name + "...");
});
// -- ask_user_choice dialog: structured forms over the socket ----------
const formDialog = document.getElementById("form-dialog");
const formFields = document.getElementById("form-fields");

// Build one field (or group) into `host`; every control records its
// answer into `values` under the item's key. Groups recurse; a group
// answers as a nested object. No innerHTML anywhere below: the spec
// is model-authored, so every string lands through textContent.
function buildFormField(item, values, host) {
  if (item.type === "group") {
    const group = document.createElement("div");
    group.className = "form-group";
    if (item.label) {
      const gl = document.createElement("div");
      gl.className = "form-group-label";
      gl.textContent = item.label;
      group.appendChild(gl);
    }
    const inner = {};
    values[item.key] = inner;
    for (const child of item.items || []) {
      buildFormField(child, inner, group);
    }
    host.appendChild(group);
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "form-field";
  const label = document.createElement("label");
  label.textContent = item.label || item.key;
  wrap.appendChild(label);
  if (item.type === "text") {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "form-text";
    input.placeholder = item.placeholder || "";
    input.value = item.default || "";
    values[item.key] = item.default || "";
    input.addEventListener("input", () => { values[item.key] = input.value; });
    wrap.appendChild(input);
  } else {  // choice
    const chosen = (item.defaults || []).slice();
    values[item.key] = chosen;
    const opts = document.createElement("div");
    opts.className = "form-opts";
    for (const opt of item.options || []) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "form-opt" +
        (chosen.includes(opt) ? " sel" : "");
      chip.textContent = opt;
      chip.addEventListener("click", () => {
        const at = chosen.indexOf(opt);
        if (item.multiple) {
          if (at >= 0) { chosen.splice(at, 1); }
          else { chosen.push(opt); }
          chip.classList.toggle("sel", chosen.includes(opt));
        } else {
          chosen.length = 0;
          chosen.push(opt);
          for (const other of opts.children) {
            other.classList.toggle("sel", other === chip);
          }
        }
      });
      opts.appendChild(chip);
    }
    wrap.appendChild(opts);
  }
  host.appendChild(wrap);
}

// Answer from the LIVE values object (every control writes into it as
// it changes; the dataset snapshot is only the initial state).
function sendFormAnswer(values) {
  formDialog.close();
  try {
    ws.send(JSON.stringify({ form_answer: {
      values: values,
      chat: formDialog._chat || currentChat || "",
    } }));
  } catch (err) { /* dead socket: the wait times out server-side */ }
}
document.getElementById("form-send").addEventListener("click", () => {
  if (formDialog._values) {
    sendFormAnswer(formDialog._values);
  }
});
document.getElementById("form-cancel").addEventListener("click", () => {
  sendFormAnswer(null);
});

function openAskForm(m) {
  // A question may arrive from a chat this tab is NOT viewing (the
  // server surfaces pending forms to every socket). The dialog names
  // the chat, and the answer routes INTO that chat.
  const chat = m.chat || currentChat;
  document.getElementById("form-title").textContent =
    (chat && chat !== currentChat ? "[" + chat + "] " : "")
    + (m.prompt || "a question for you");
  formFields.innerHTML = "";
  const values = {};
  for (const item of m.items || []) {
    buildFormField(item, values, formFields);
  }
  formDialog._values = values;
  formDialog._chat = chat;
  formDialog.showModal();
  const firstText = formFields.querySelector(".form-text");
  if (firstText) firstText.focus();
}
document.getElementById("skills-close").addEventListener("click", () => {
  document.getElementById("skills-dialog").close();
});
document.getElementById("skills-btn").addEventListener("click", openSkills);

// Paint the cached copy immediately; the server's restored history
// clears and repaints it when the socket connects. The cache is the
// page's instant-paint guess at the last conversation -- the server's
// restore frames are the truth that replace it.
restoreCache();
// The tree arrives with the server's first frames; ask for it once
// the socket is open so branch chips render before the first turn.
(function askForTree() {
  const send = () => {
    if (wsConnected) { try {
      ws.send(JSON.stringify({ tree_show: true }));
    } catch (e) {} }
    else setTimeout(send, 1000);
  };
  setTimeout(send, 500);
})();

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
// Reasoning GENERATION (not display): off stops reasoning tokens
// entirely. Sent as the /reason command so the engine's rebuild path
// (which carries the conversation) is the one that applies it.
const reasonSel = document.getElementById("opt-reason");
reasonSel.addEventListener("change", () => {
  ws.send(JSON.stringify({ text: "/reason " + reasonSel.value }));
});
// The model's image tool: unchecked, generate_image is withheld from
// the model entirely -- prompts get refined in prose instead of the
// model firing a multi-minute GPU swap on its own initiative. The 🎨
// dialog (operator-side) is unaffected.
const imageToolBox = document.getElementById("opt-imagetool");
imageToolBox.addEventListener("change", () => {
  ws.send(JSON.stringify({ text: "/imagetool "
    + (imageToolBox.checked ? "on" : "off") }));
});
// Music tool: same gate, same reasoning -- the operator decides whether
// the MODEL may fire a minutes-long music run on its own initiative.
const musicToolBox = document.getElementById("opt-musictool");
musicToolBox.addEventListener("change", () => {
  ws.send(JSON.stringify({ text: "/musictool "
    + (musicToolBox.checked ? "on" : "off") }));
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
        #: While an engine-path image run is active (typed /image or a
        #: model tool-call), every run-relevant frame this WebChat emits
        #: is recorded here: it IS the replay log a refreshed page
        #: rejoins the animation from. The engine emits from an executor
        #: thread, so the log is set/cleared by the socket handler.
        self._gen_log: list[dict[str, Any]] | None = None
        #: Live relay to peer tabs while a run is active (set by the
        #: socket handler to the serve-scope publisher, or None).
        self._relay = None
        #: The serve()-scope GEN registry (set by ws_endpoint): a
        #: model-initiated generate_image tool-call opens a run here.
        self._gen_registry: dict[str, Any] | None = None
        self._own_run = False

    def set_gen_log(self, log: list[dict[str, Any]] | None) -> None:
        """Attach this run's replay log (the socket handler calls it
        around model-initiated image runs)."""
        self._gen_log = log

    # -- ask_user_choice bridge: executor <-> page dialog -----------------
    def ask_form(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Queue a structured form on the OWNING CHAT and block for
        the answer -- independent of whether anyone is viewing that
        chat right now. Called on the EXECUTOR thread by the tool.

        The form is recorded server-side (the chat's pending_form) and
        surfaced to EVERY live socket -- the operator's rule: a
        question must not depend on the session being open and active.
        Any tab may open it; the frame names the chat so a foreign tab
        answers INTO the right conversation. A socket that joins the
        chat later replays the pending form on connect; the dialog
        stays until answered or cancelled.

        The answer arrives as a ``form_answer`` socket message; the
        handler stores it and sets the event. A dialog Cancel answers
        None, which the tool turns into "the operator cancelled".
        """
        import threading

        done = threading.Event()
        holder: dict[str, Any] = {}
        self._form_wait = (done, holder)
        frame = {"kind": "ask_form", "prompt": spec.get("prompt", ""),
                 "items": spec.get("items", []),
                 "chat": getattr(self, "_chat_box_name", "") or ""}
        # Queue server-side: the question survives no-viewer windows,
        # reconnects, and joins after the asking turn started.
        hook = getattr(self, "_chat_queue_form", None)
        if callable(hook):
            hook(frame)
        # Surface to every live socket in every chat (not just the
        # owning chat's viewers): any tab may answer.
        surface = getattr(self, "_chat_surface_form", None)
        if callable(surface):
            try:
                surface(frame)
            except Exception:  # noqa: BLE001 - surfacing is best-effort
                pass
        else:
            self._frame(frame)
        # One turn blocks here up to 10 minutes; the operator may be
        # thinking. A vanished page is covered by the timeout -- the
        # tool reports honestly rather than wedging the turn forever.
        if not done.wait(timeout=600):
            self._form_wait = None
            unqueue = getattr(self, "_chat_unqueue_form", None)
            if callable(unqueue):
                unqueue()
            raise TimeoutError("no form answer within 600s")
        self._form_wait = None
        unqueue = getattr(self, "_chat_unqueue_form", None)
        if callable(unqueue):
            unqueue()
        return holder.get("answer")

    def set_relay(self, publisher) -> None:
        """While an engine-path run is active, this WebChat's frames are
        relayed to every other tab live (the replay log records them
        regardless; peers should not wait for a refresh to see the
        animation continue)."""
        self._relay = publisher

    def set_relay_publisher(self, publisher) -> None:
        """The serve-scope publisher, available for the tool-call path:
        WebChat.tool_call arms the relay when it opens a run."""
        self._relay_publisher = publisher

    def set_gen_registry(self, registry: dict[str, Any] | None) -> None:
        """The serve()-scope GEN dict: lets a model-initiated
        generate_image tool-call open its own run record."""
        self._gen_registry = registry

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        """A generate_image tool-call opens a run record -- the run
        must outlive this socket just like the dialog and typed paths:
        a refresh mid-turn rejoins the animation instead of losing it.
        Every other tool renders as always."""
        if (name == "generate_image" and self._gen_registry is not None
                and self._gen_registry.get("active") is None):
            run = {"frames": [],
                   "request": {k: v for k, v in arguments.items()
                               if k not in ("init_image", "mask")},
                   "ended": 0.0}
            self._gen_registry["active"] = run
            self._own_run = True
            self._gen_log = run["frames"]
            self._relay = getattr(self, "_relay_publisher", None)
            self._frame({"kind": "assistant"})
            self._frame({"kind": "busy", "on": True})
            self._frame({"kind": "gen_progress",
                         "phase": "generating image — preparing "
                                  "(swap / weight load)",
                         "step": 0, "total": 0})
        self._frame({"kind": "tool_call",
                     "text": summarize_call(name, arguments)})

    def _frame(self, payload: dict[str, Any]) -> None:
        frame = json.dumps(payload)
        log = self._gen_log
        if log is not None and payload.get("kind") in (
                "you", "assistant", "busy", "gen_progress", "image",
                "audio", "raw", "error", "notice", "turn_end"):
            # list.append is GIL-atomic; safe from the executor thread.
            log.append(dict(payload))
        relay = self._relay
        if relay is not None and payload.get("kind") in (
                "busy", "gen_progress", "image", "audio", "raw", "error",
                "notice", "turn_end"):
            # Peer tabs follow this run live. The publisher runs on the
            # event loop; this may be an executor thread, so schedule.
            try:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        relay(json.loads(frame))))
            except RuntimeError:
                pass
        try:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._send_guarded(frame)))
        except RuntimeError:
            pass  # loop closed: server shutting down

    async def _send_guarded(self, frame: str) -> None:
        """A dead tab (refresh mid-turn) must not spray unretrieved-task
        exceptions into the log -- the send is best-effort, always.
        The server-wide session has no socket of its own: its frames go
        through the serve-scope publisher (`_frame_out`) instead, which
        fans out to every live tab."""
        out = getattr(self, "_frame_out", None)
        if out is not None:
            await out(json.loads(frame))
            return
        if self.socket is None:
            return
        try:
            await self.socket.send(
                {"type": "websocket.send", "text": frame})
        except Exception:  # noqa: BLE001
            pass

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
                elif language in ("tool", "tool_call"):
                    # A tool fence whose payload does not parse never
                    # reached the ⚙ panel (the engine drops near-miss
                    # calls without running them), so dropping it here
                    # too would hide the model's ONLY visible trace --
                    # the exact way a broken ask_user_choice vanished
                    # with no error anywhere. Show it as code instead.
                    from rt_harness.tools import _balanced_json
                    body = payload.strip()
                    try:
                        json.loads(body)
                    except ValueError:
                        try:
                            json.loads(_balanced_json(body))
                        except ValueError:
                            self._frame({"kind": "code", "language": "tool",
                                         "text": body})
                self._fence_open = False
                self._fence_raw = ""

    def thinking(self, text: str) -> None:
        self._frame({"kind": "thinking", "text": text,
                     "show": self.show_thinking})

    def tool_result(self, name: str, result: ToolResult) -> None:
        body = result.text if result.ok else f"error: {result.text}"
        self._frame({"kind": "tool_result", "ok": result.ok, "text": body})
        if name == "generate_image" and self._own_run:
            # Close the run this WebChat opened: the terminal frames go
            # into the replay log synchronously, the run retires, and
            # the page-side busy indicator ends with the turn (the
            # engine's own turn_end/busy-off will follow, unrecorded --
            # the run is already closed; nothing to rejoin).
            reg = self._gen_registry
            if reg is not None:
                run = reg.get("active")
                if run is not None:
                    self._frame({"kind": "turn_end"})
                    self._frame({"kind": "busy", "on": False})
                    run["ended"] = time.time()
                    reg["last"] = run
                    reg["active"] = None
            self._gen_log = None
            self._relay = None
            self._own_run = False

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
        # Attach the run's request to the frame (copy = the prompt, not
        # the filename) and persist it as a sidecar next to the image:
        # the disk-replay reads the sidecar long after this socket and
        # its run record are gone.
        frame: dict[str, Any] = {"kind": "image", "path": relative_path}
        reg = self._gen_registry
        run = reg.get("active") if reg else None
        if run is not None and run.get("request"):
            frame["request"] = run["request"]
            try:
                root = self.session.workspace.root
                sidecar = (root / relative_path).with_suffix(".prompt")
                sidecar.write_text(json.dumps(run["request"], indent=2))
            except OSError:
                pass
        self._frame(frame)

    def show_audio(self, relative_path: str) -> None:
        # Same contract as show_image: the clip renders in the chat as an
        # <audio> element; the copy button carries the caption+lyrics
        # that made it (the human-usable content), never the path.
        frame: dict[str, Any] = {"kind": "audio", "path": relative_path}
        reg = self._gen_registry
        run = reg.get("active") if reg else None
        if run is not None and run.get("request"):
            frame["request"] = run["request"]
        self._frame(frame)

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

    def render_images(self, owned: "list[str] | None" = None
                      ) -> list[dict[str, Any]]:
        """The image-turn frames for a repaint -- same shape the live
        path sends, sidecar request included. ``owned`` is this
        CHAT'S image list (file names); with per-chat repaints the
        shared images/ folder must not leak every era's generations
        into every chat -- a chat shows its own images, and unowned
        historical images belong to the legacy chat alone."""
        frames: list[dict[str, Any]] = []
        try:
            images = sorted(
                (self.ui_session_root() / "images").glob("*.png"),
                key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            images = []
        if owned is not None:
            wanted = set(owned)
            images = [p for p in images if p.name in wanted]
        for path in images[:20]:
            frames.append({"kind": "assistant"})
            frame: dict[str, Any] = {"kind": "image",
                                     "path": f"images/{path.name}"}
            # The sidecar written at generation time carries the prompt
            # and knobs: replayed images keep their copy/retry buttons,
            # and "copy" means the PROMPT -- never the filename.
            sidecar = path.with_suffix(".prompt")
            try:
                frame["request"] = json.loads(sidecar.read_text())
            except (OSError, ValueError):
                pass
            frames.append(frame)
            frames.append({"kind": "turn_end"})
        return frames

    def render_audio(self, owned: "list[str] | None" = None
                     ) -> list[dict[str, Any]]:
        """The audio-turn frames for a repaint -- same shape the live
        path sends. Owned-list logic mirrors render_images: a chat
        replays its own clips (by .prompt sidecar's owning chat), not
        every era's audio."""
        frames: list[dict[str, Any]] = []
        try:
            clips = sorted(
                (self.ui_session_root() / "audio").glob("*.wav"),
                key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            clips = []
        if owned is not None:
            wanted = set(owned)
            clips = [p for p in clips if p.name in wanted]
        for path in clips[:10]:
            frames.append({"kind": "assistant"})
            frame: dict[str, Any] = {"kind": "audio",
                                     "path": f"audio/{path.name}"}
            sidecar = path.with_suffix(".prompt")
            try:
                frame["request"] = json.loads(sidecar.read_text())
            except (OSError, ValueError):
                pass
            frames.append(frame)
            frames.append({"kind": "turn_end"})
        return frames

    def render_history(self, messages: list[dict[str, Any]]
                       ) -> list[dict[str, Any]]:
        """The frame list a repaint of `messages` consists of -- the
        caller sends it (send_history) or broadcasts it (webui's
        repaint_chain). Pure: no sends."""
        import re as _re

        tool_fence_re = _re.compile(r"```tool\n(\{.*?\})\n```",
                                    _re.DOTALL)
        code_fence_re = _re.compile(
            r"```([A-Za-z0-9_+#.-]*)\n(.*?)```", _re.DOTALL)
        frames: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                body = str(message.get("content", ""))
                if body.startswith("TOOL RESULT for"):
                    head, _, rest = body.partition("\n")
                    name = head.replace("TOOL RESULT for ", "")
                    frames.append({"kind": "tool_result", "ok": True,
                                   "text": rest.strip()[:800]})
                    continue
                frames.append({"kind": "you", "text": body})
            elif role == "assistant":
                frames.append({"kind": "assistant"})
                body = str(message.get("content", ""))
                calls = message.get("tool_calls") or []
                for call in calls:
                    function = call.get("function") or {}
                    frames.append({"kind": "tool_call",
                                   "text": summarize_call(
                                       str(function.get("name", "")),
                                       function.get("arguments") or {})})
                shown = tool_fence_re.sub("", body).strip()
                # Fenced CODE blocks replay as code panels (the live
                # path renders them that way); only the tool plumbing
                # is dropped. The prose splits around them.
                pos = 0
                for m in code_fence_re.finditer(shown):
                    prose = shown[pos:m.start()].strip()
                    if prose:
                        frames.append({"kind": "delta", "text": prose})
                    frames.append({"kind": "code",
                                   "language": m.group(1) or "",
                                   "text": m.group(2).rstrip("\n")})
                    pos = m.end()
                tail = shown[pos:].strip()
                if tail:
                    frames.append({"kind": "delta", "text": tail})
                frames.append({"kind": "turn_end"})
            elif role == "tool":
                body = str(message.get("content", ""))
                head, _, rest = body.partition("\n")
                frames.append({"kind": "tool_result", "ok": True,
                               "text": rest.strip()[:800]})
        return frames

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


class TurnTree:
    """The conversation as a graph: the context window is the root and
    every turn is a node whose parent is the turn it followed.

    The engine's session holds the ACTIVE chain (root -> ... -> leaf);
    this tree holds every branch ever taken. Before a turn runs, the
    active chain is projected into ``session.messages``; after it, the
    new user+assistant messages are recorded as a node on the chain.
    Switching branches = repointing the active leaf and re-projecting.

    Image turns (/image button or command) are nodes like any other:
    the button inserts the "/image <prompt>" node, the generation is its
    response content.
    """

    def __init__(self) -> None:
        # node = {"id": int, "parent": int|None, "user": str,
        #         "assistant": [messages], "kind": "chat"|"image"}
        self.nodes: dict[int, dict] = {}
        self.order: list[int] = []          # discovery order, stable ids
        self.active: int | None = None      # the leaf we prompt against
        self._next = 1

    def _mk(self, parent: int | None, kind: str = "chat") -> int:
        nid = self._next
        self._next += 1
        self.nodes[nid] = {"id": nid, "parent": parent, "user": "",
                          "assistant": [], "kind": kind}
        self.order.append(nid)
        return nid

    def chain(self, nid: int | None) -> list[int]:
        """Root -> ... -> nid (inclusive)."""
        out: list[int] = []
        while nid is not None:
            out.append(nid)
            nid = self.nodes[nid]["parent"]
        return list(reversed(out))

    def children(self, nid: int | None) -> list[int]:
        return [n for n in self.order
                if self.nodes[n]["parent"] == nid]

    def messages_for(self, nid: int | None) -> list[dict]:
        """The engine messages along the chain to nid -- the branch's
        full context, exactly what the model should see."""
        out: list[dict] = []
        for n in self.chain(nid):
            node = self.nodes[n]
            if node["user"]:
                out.append({"role": "user", "content": node["user"]})
            out.extend(node["assistant"])
        return out

    def append_turn(self, user: str, assistant: list[dict]) -> int:
        """Record a completed turn as a child of the active leaf and
        advance the active pointer to it."""
        nid = self._mk(self.active)
        node = self.nodes[nid]
        node["user"] = user
        node["assistant"] = assistant
        self.active = nid
        return nid

    def branch(self, from_node: int) -> int:
        """Fork a new path from an existing node WITHOUT creating an
        empty intermediate: the active pointer moves to the fork's
        parent, so the operator's next prompt becomes the sibling
        node itself, with the fork's parent context."""
        self.active = self.nodes[from_node]["parent"]
        return from_node

    def switch(self, nid: int) -> bool:
        """Make an existing node the active leaf (its chain becomes the
        context). Only one branch is active at a time."""
        if nid not in self.nodes:
            return False
        self.active = nid
        return True

    def describe(self, with_content: bool = True) -> dict:
        """The whole tree for the client: nodes with parent links, the
        active chain, and each node's summary."""
        nodes = []
        for nid in self.order:
            node = self.nodes[nid]
            entry = {"id": nid, "parent": node["parent"],
                     "kind": node["kind"], "active_chain":
                         nid in set(self.chain(self.active))}
            if node["user"]:
                entry["user"] = node["user"][:80]
            if with_content:
                entry["has_image"] = any(
                    m.get("role") == "assistant"
                    and "images/" in str(m.get("content", ""))
                    for m in node["assistant"])
            nodes.append(entry)
        return {"nodes": nodes, "active": self.active}

    def to_json(self) -> dict:
        """The serializable tree: nodes (with their response messages),
        the id counter, and the active leaf. Round-trips via from_json."""
        return {
            "nodes": [{"id": nid, "parent": node["parent"],
                       "user": node["user"],
                       "assistant": node["assistant"],
                       "kind": node["kind"]}
                      for nid, node in
                      ((n, self.nodes[n]) for n in self.order)
                      if node["user"]],   # empty nodes never persisted
            "next": self._next,
            "active": self.active,
        }

    def from_json(self, data: dict) -> bool:
        """Rebuild from a to_json dict. False when the payload is not a
        usable tree (caller falls back to adopting the flat messages)."""
        try:
            nodes = data["nodes"]
            self.nodes = {}
            self.order = []
            for entry in nodes:
                nid = int(entry["id"])
                self.nodes[nid] = {
                    "id": nid,
                    "parent": entry["parent"],
                    "user": str(entry.get("user", "")),
                    "assistant": list(entry.get("assistant") or []),
                    "kind": str(entry.get("kind", "chat")),
                }
                self.order.append(nid)
            self._next = int(data.get("next", 0))
            self.active = data.get("active")
            if self.active is not None:
                self.active = int(self.active)
        except (KeyError, TypeError, ValueError):
            return False
        # validity: every node's parent must exist (or be the root)
        if any(n["parent"] is not None and n["parent"] not in self.nodes
               for n in self.nodes.values()):
            return False
        if self.active is not None and self.active not in self.nodes:
            return False
        return bool(self.nodes)

    def adopt(self, messages: list[dict]) -> None:
        """Bootstrap the tree from a flat history (server restart or
        session load): each user turn becomes a node on one chain.
        System messages are skipped -- the engine re-derives its own;
        the tree carries only conversation turns."""
        if self.nodes:
            return
        pending_assistant: list[dict] = []
        for m in messages:
            if m.get("role") == "system":
                continue
            if m.get("role") == "user":
                if self.active is not None and pending_assistant:
                    self.nodes[self.active]["assistant"] = pending_assistant
                    pending_assistant = []
                nid = self._mk(self.active)
                self.nodes[nid]["user"] = str(m.get("content", ""))
                self.active = nid
            else:
                pending_assistant.append(m)
        if self.active is not None and pending_assistant:
            self.nodes[self.active]["assistant"] = pending_assistant


#: SessionManager sits at module level, so the name-minter it needs
#: lives beside it. Mints a unique chat-<timestamp> file name for a
#: chat's first autosave and records it on the UI so the ChatBox's name
#: (and the listing's) can follow it.
def _mint_chat_name(ui: "WebChat") -> str:
    from rt_harness.infotools import _session_dir

    directory = _session_dir()
    base = time.strftime("chat-%Y%m%d-%H%M%S")
    name, n = base, 2
    while (directory / f"{name}.json").exists():
        name = f"{base}-{n}"
        n += 1
    return name


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

    def __init__(self, ui: "WebChat",
                 tree: "TurnTree | None" = None) -> None:
        self.ui = ui
        self.tree = tree
        from rt_harness.infotools import _session_dir

        self.dir = _session_dir()
        #: WHICH document turns autosave into -- the per-session graph
        #: contract: each session maintains its own context + tree. A
        #: named load makes THAT file the autosave target (its graph
        #: keeps growing); /clear pivots back to the scratch slot so a
        #: fresh chat can never overwrite a named document.
        self.active_session: str = SessionManager.AUTOSAVE

    def autosave(self) -> str:
        """Persist the live conversation into the ACTIVE document. With
        chats, each chat IS its own document (a chat's session manager
        has `active_session` set to the chat's own name from birth),
        so a plain save suffices. The old scratch-mirror
        (`__webui_last__`) is GONE: mirroring every chat's turn into
        one shared slot was the one-session contract, and with N chats
        it wrote chat A's turns into the slot chat B would restore.
        A chat that has never been named is saved under its generated
        chat-<ts> name on its first completed turn."""
        if self.active_session == SessionManager.AUTOSAVE:
            # An un-named chat: give it a file of its own. The name is
            # minted once and pinned, so the operator's chats keep
            # stable identities without ever typing one.
            self.active_session = _mint_chat_name(self.ui)
            follow = getattr(self.ui, "_chat_follow_rename", None)
            if callable(follow):
                follow(self.active_session)
        return self.save(self.active_session)

    def adopt_document(self, name: str) -> None:
        """A named load makes THAT file the active document: its turns
        and its graph edits (branches, switches) autosave into its own
        file. Each session maintains its own graph."""
        self.active_session = name

    def start_fresh(self) -> None:
        """/clear starts a fresh document: pivot the autosave target to
        the scratch slot so the new conversation can never write over a
        named session's file. The named graph stays on disk, untouched."""
        self.active_session = SessionManager.AUTOSAVE

    def drop_active(self, name: str) -> bool:
        """A deleted session must not resurrect: if it was the ACTIVE
        named document, pivot to the scratch slot or the next autosave
        would recreate the file. True when the pivot happened."""
        if name != SessionManager.AUTOSAVE \
                and name == self.active_session:
            self.active_session = SessionManager.AUTOSAVE
            return True
        return False

    def save(self, name: str) -> str:
        import json

        path = self.dir / f"{name}.json"
        payload: dict[str, Any] = {"messages": self.ui.session.messages}
        # The tree rides along: branches survive restarts and named
        # loads. The active chain stays in `messages` (TUI reads that),
        # so the file is both a flat session and a graph.
        if self.tree is not None and self.tree.nodes:
            payload["tree"] = self.tree.to_json()
        # Which document this conversation BELONGS to: the startup
        # restore reads it off the autosave slot and pivots the active
        # session back to the named document the operator was in when
        # the server restarted -- otherwise the next turn would fork
        # the conversation into the scratch slot.
        payload["active_session"] = self.active_session
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
        self.loaded_tree = data.get("tree")
        # The active-session marker is authoritative ONLY on the
        # autosave mirror (that is what a restart reads to pivot back
        # into the document the operator was in). A NAMED load IS its
        # own document -- a backup saved out of another conversation
        # must not pivot into that conversation's file and overwrite
        # it when continued.
        if name == SessionManager.AUTOSAVE:
            self.loaded_active = str(data.get("active_session") or "")
        else:
            self.loaded_active = name
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
            if path.stem == SessionManager.AUTOSAVE:
                continue  # the dead mirror slot is not a chat
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
    # -- chats: N self-contained conversations, every socket bound to one --
    # The operator's contract: a session IS a chat, the way chat front
    # ends work. Each chat owns its engine session (its own context
    # window, skills set, model config), its conversation graph, its
    # frame log, and the set of sockets viewing it. A turn in one chat
    # reaches only that chat's tabs -- no bleed-over. Switching is
    # binding: `session load` rebinds the requesting socket and
    # repaints it alone; every other tab keeps its own conversation.
    #
    # Still server-global on purpose: the model client (one endpoint),
    # the image-run registry GEN (one GPU), and TURN_LOCK (one
    # generation at a time server-wide -- the card and the swap chain
    # are shared hardware, not per-chat state).
    GEN: dict[str, Any] = {"active": None, "last": None}

    #: The in-flight generation TASK per chat: Cancel clicks race the
    #: handle of the chat the click came from. The one-task-at-a-time
    #: guarantee is TURN_LOCK's (server-wide), but a task handle is
    #: per-chat so a second chat's tab never shows another chat's run
    #: as its own.
    HISTORY_CAP = 2000

    class ChatBox:
        """One self-contained chat: engine session, graph, frame log,
        viewers. Built from a session file (or fresh) and registered in
        CHATS; lives until the server stops."""

        def __init__(self, name: str, ui: "WebChat",
                     tree: "TurnTree", sessions: "SessionManager") -> None:
            self.name = name
            self.ui = ui
            self.tree = tree
            self.sessions = sessions
            #: bounded frame log = this chat's transcript of record
            self.history: list[dict[str, Any]] = []
            #: sockets currently viewing THIS chat
            self.sockets: set = set()
            #: the in-flight task (text/image) this chat owns
            self.task: asyncio.Task | None = None
            #: this chat's image files (names under images/), in
            #: generation order. A chat repaints only its own images.
            self.images: list[str] = []
            #: this chat's audio clips (names under audio/), same
            #: ownership rule as images.
            self.audio: list[str] = []
            #: The chat's UNANSWERED ask_form, when one is pending:
            #: the full frame, kept server-side so the question
            #: survives connects, reconnects and no-viewer windows.
            #: None when no question is waiting. ask_form frames are
            #: still never frame-LOGGED (a replay would re-ask an
            #: answered question); this slot is the live surface
            #: contract instead: a join replays it, a list badges it.
            self.pending_form: dict[str, Any] | None = None

        def log(self, frame: dict[str, Any]) -> None:
            """Record into this chat's frame log (bounded). ask_form is
            the one exception: a replayed form would re-ask a question
            already answered."""
            kind = frame.get("kind")
            if kind == "pong" or kind == "ask_form":
                return
            self.history.append(dict(frame))
            if len(self.history) > HISTORY_CAP:
                del self.history[0:len(self.history) - HISTORY_CAP]

    CHATS: dict[str, ChatBox] = {}

    async def chat_pub(box: "ChatBox", frame: dict[str, Any]) -> None:
        """Publish to every socket viewing THIS chat, and into its
        frame log. Dead sockets are dropped, never fatal."""
        box.log(frame)
        for sock in list(box.sockets):
            try:
                await sock.send_json(frame)
            except Exception:  # noqa: BLE001
                box.sockets.discard(sock)

    async def broadcast_all(frame: dict[str, Any],
                            exclude: set = frozenset()) -> None:
        """Publish a frame to EVERY live socket across all chats --
        the ask_form surface. The operator's rule: a question the
        model asked must not depend on the session being open and
        active; any tab may surface it, and any tab may answer."""
        for other in list(CHATS.values()):
            if not other.sockets:
                continue
            for sock in list(other.sockets):
                if sock in exclude:
                    continue
                try:
                    await sock.send_json(frame)
                except Exception:  # noqa: BLE001
                    other.sockets.discard(sock)

    async def repaint_sockets(box: "ChatBox", socks: set,
                              broadcast_tree: bool = False,
                              **tree_extra) -> None:
        """Repaint the given sockets of one chat with its ACTIVE chain,
        replacing the chat's frame log with exactly the frames sent
        (so a later connect replays the current branch)."""
        frames: list[dict[str, Any]] = [{"kind": "clear"},
                                         {"kind": "restore_begin"}]
        frames.extend(box.ui.render_history(box.ui.session.messages))
        frames.extend(box.ui.render_images(box.images))
        frames.extend(box.ui.render_audio(box.audio))
        frames.append({"kind": "restore_end"})
        if broadcast_tree:
            frames.append({"kind": "tree", **box.tree.describe(),
                           **tree_extra})
        box.history.clear()
        for frame in frames:
            box.log(frame)
        for frame in frames:
            for sock in list(socks):
                try:
                    await sock.send_json(frame)
                except Exception:  # noqa: BLE001
                    if sock in box.sockets:
                        box.sockets.discard(sock)
                    socks.discard(sock)

    def _seed_chat_images(box: "ChatBox") -> None:
        """A chat's image list from two sources: its own saved history
        (every generate_image tool result names the file it wrote) and
        sidecar ownership (post-chats sidecars record the chat that
        generated them). The legacy chat additionally adopts every
        image whose sidecar records NO owner -- the pre-chats era,
        which had one shared conversation anyway."""
        import re as _re

        pat = _re.compile(r"images/([\w.\-]+\.png)")
        names: list[str] = []
        for m in box.ui.session.messages:
            for match in pat.finditer(str(m.get("content", ""))):
                name = match.group(1)
                if name not in names:
                    names.append(name)
        img_dir = Path(str(box.ui.session.workspace.root)) / "images"
        if img_dir.is_dir():
            for side in sorted(img_dir.glob("*.prompt"),
                               key=lambda p: p.stat().st_mtime):
                try:
                    req = json.loads(side.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                owner = str(req.get("chat") or "")
                name = side.stem + ".png"
                if owner == box.name and name not in names:
                    names.append(name)
                elif not owner and box.name == "legacy":
                    name_png = side.with_suffix(".png").name
                    if not (img_dir / name_png).exists():
                        continue
                    if name_png not in names:
                        names.append(name_png)
        box.images = names

    def _seed_chat_audio(box: "ChatBox") -> None:
        """A chat's audio list, same two sources as images: its saved
        history (generate_music tool results name the clip) and sidecar
        ownership (the .prompt next to a .wav records the chat)."""
        import re as _re

        pat = _re.compile(r"audio/([\\w.\\-]+\\.wav)")
        names: list[str] = []
        for m in box.ui.session.messages:
            for match in pat.finditer(str(m.get("content", ""))):
                name = match.group(1)
                if name not in names:
                    names.append(name)
        audio_dir = Path(str(box.ui.session.workspace.root)) / "audio"
        if audio_dir.is_dir():
            for side in sorted(audio_dir.glob("*.prompt"),
                               key=lambda p: p.stat().st_mtime):
                try:
                    req = json.loads(side.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                owner = str(req.get("chat") or "")
                name = side.stem + ".wav"
                if owner == box.name and name not in names:
                    names.append(name)
        box.audio = names

    def build_chat(name: str) -> "ChatBox":
        """Build a chat from its session file (or fresh when the file
        is absent), wire its renders to its own publisher, and register
        it. The name is the session file stem: the chat IS the file."""
        ui = WebChat(None, client, config,
                     tool_output=chat_config.tool_output,
                     show_thinking=config.show_thinking)
        ui.set_gen_registry(GEN)
        tree = TurnTree()
        sessions = SessionManager(ui, tree=tree)
        box = ChatBox(name, ui, tree, sessions)
        ui.set_relay_publisher(
            lambda frame: asyncio.ensure_future(chat_pub(box, frame)))
        ui._frame_out = lambda payload: asyncio.ensure_future(
            chat_pub(box, payload))
        sessions.active_session = name
        # When autosave mints a file name for an un-named chat, the
        # box (and its registry entry) must follow: the chat's identity
        # IS its file. Hook the session manager's rename onto the box.
        def _follow_rename(new_name: str, _box=box) -> None:
            old_name = _box.name
            if new_name == old_name:
                return
            _box.name = new_name
            _box.ui._chat_box_name = new_name
            if CHATS.get(old_name) is _box:
                del CHATS[old_name]
            CHATS[new_name] = _box
        box.follow_rename = _follow_rename
        ui._chat_follow_rename = _follow_rename
        ui._chat_box_name = name
        # The ask_form surface: queue on the box, surface to ALL live
        # sockets (any tab may answer; the frame names this chat).
        def _queue_form(frame: dict, _box=box) -> None:
            _box.pending_form = dict(frame)
        def _surface_form(frame: dict, _ui=ui) -> None:
            # Runs on the EXECUTOR thread (the tool blocks there):
            # schedule onto the ENGINE loop the WebChat already holds
            # (rebound live at connect), never get_event_loop() here --
            # that would mint a fresh dead loop and silently drop the
            # broadcast (the empty-socket failure).
            loop_ref = getattr(_ui, "_loop", None)
            if loop_ref is None or loop_ref.is_closed():
                return
            try:
                loop_ref.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        broadcast_all(frame)))
            except RuntimeError:
                pass
        def _unqueue_form(_box=box) -> None:
            _box.pending_form = None
        ui._chat_queue_form = _queue_form
        ui._chat_surface_form = _surface_form
        ui._chat_unqueue_form = _unqueue_form
        loaded_tree = None
        try:
            ok, _note = sessions.load(name)
            if ok:
                loaded_tree = getattr(sessions, "loaded_tree", None)
        except Exception:  # noqa: BLE001 - a bad file is a fresh chat
            pass
        if isinstance(loaded_tree, dict) and tree.from_json(loaded_tree):
            sysmsgs = [m for m in ui.session.messages
                       if m.get("role") == "system"]
            ui.session.messages[:] = sysmsgs + \
                tree.messages_for(tree.active)
        else:
            tree.adopt(ui.session.messages)
        # Prime the frame log from the loaded messages: a connect (or a
        # server restart) paints the transcript instead of an empty
        # page with context the engine holds but the page cannot see.
        # Images: this chat's own list, seeded from its saved history
        # (generate_image tool results name the file), so no other
        # chat's images leak in. Legacy additionally adopts the
        # unowned historical images (pre-chats era).
        for frame in ui.render_history(ui.session.messages):
            box.log(frame)
        _seed_chat_images(box)
        for frame in ui.render_images(box.images):
            box.log(frame)
        _seed_chat_audio(box)
        for frame in ui.render_audio(box.audio):
            box.log(frame)
        CHATS[name] = box
        return box

    def get_or_build_chat(name: str) -> "ChatBox":
        """The live in-memory chat wins over the file: a chat already
        open keeps its unsaved state (an in-flight turn, a fresh
        /clear) rather than being reverted by a disk re-read."""
        return CHATS.get(name) or build_chat(name)

    def fresh_chat_name() -> str:
        """A unique, human-readable name for a new chat."""
        base = time.strftime("chat-%Y%m%d-%H%M%S")
        name, n = base, 2
        while (sessions_dir / f"{name}.json").exists() \
                or name in CHATS:
            name = f"{base}-{n}"
            n += 1
        return name

    from rt_harness.infotools import _session_dir
    sessions_dir = _session_dir()

    # -- one-time legacy migration ---------------------------------------
    # Before chats, the sessions dir accumulated one-session-era
    # files: named saves, probe files, the __webui_last__ mirror. With
    # per-chat files those read as N separate "chats" of stale context.
    # Migrate ONCE: every pre-chats file folds into a single `legacy`
    # chat (its messages concatenated in mtime order); the originals
    # move into legacy-archive/ (kept, never deleted) so nothing is
    # lost and the migration never re-fires.
    def migrate_legacy() -> None:
        import json as _json

        if (sessions_dir / "legacy.json").is_file() \
                or (sessions_dir / "legacy-archive").is_dir():
            return
        olds = [p for p in sessions_dir.glob("*.json")
                if p.stem not in ("legacy",)]
        if not olds:
            return
        olds.sort(key=lambda p: p.stat().st_mtime)
        messages: list[dict[str, Any]] = []
        for path in olds:
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows = data.get("messages")
            if isinstance(rows, list):
                messages.extend(m for m in rows
                                if isinstance(m, dict))
        archive = sessions_dir / "legacy-archive"
        archive.mkdir(parents=True, exist_ok=True)
        if messages:
            payload = {"messages": messages, "active_session": "legacy"}
            (sessions_dir / "legacy.json").write_text(
                _json.dumps(payload, ensure_ascii=False),
                encoding="utf-8")
        for path in olds:
            try:
                path.rename(archive / path.name)
            except OSError:
                pass

    try:
        migrate_legacy()
    except Exception:  # noqa: BLE001 - migration is best-effort
        pass

    #: The chat a new connect lands in: the most recently modified
    # CHAT file -- never the dead mirror slot. The marker the old
    # mirror carried is honored during migration only; afterwards the
    # newest chat IS the operator's last conversation.
    def initial_chat() -> "ChatBox":
        try:
            paths = [p for p in sessions_dir.glob("*.json")
                     if p.stem != SessionManager.AUTOSAVE]
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            paths = []
        if not paths:
            return build_chat(fresh_chat_name())
        return get_or_build_chat(paths[0].stem)

    #: One generation at a time, server-wide: the card, the swap chain
    #: and the router are shared hardware, not per-chat state. Two
    #: chats' turns serialize here exactly the way two tabs' did.
    TURN_LOCK = asyncio.Lock()

    def log_frame(run: dict[str, Any] | None, frame: dict[str, Any]) -> None:
        """Record into the active run's replay log. SYNCHRONOUS and
        GIL-atomic -- safe to call from within ``finally`` blocks where a
        CancelledError could otherwise split the terminal frames from
        their retirement."""
        if run is not None:
            run["frames"].append(dict(frame))

    async def bcast_pub(frame: dict[str, Any]) -> None:
        """The ONE publisher every generation path uses (dialog, typed
        /image, model tool-call): record into the active run, then send
        to the chat that owns it. A run outlives whichever socket
        started it; dead sockets are dropped, never fatal. The owning
        chat is carried ON the run (GEN active runs gain a 'box')."""
        run = GEN["active"]
        log_frame(run, frame)
        box = (run or {}).get("box")
        if box is not None:
            await chat_pub(box, frame)

    def retire_run() -> dict[str, Any] | None:
        """Close the active run: terminal frames land in the replay log
        FIRST (sync appends -- no await between them and retirement, so
        cancellation cannot leave the replay half-closed), then the run
        retires to GEN['last'] for late joiners. Returns the run."""
        run = GEN["active"]
        if run is not None:
            log_frame(run, {"kind": "turn_end"})
            log_frame(run, {"kind": "busy", "on": False})
            run["ended"] = time.time()
            GEN["last"] = run
            GEN["active"] = None
        return run

    async def send_terminal_frames(box: "ChatBox") -> None:
        """The turn-close frames every page viewing this chat needs:
        turn_end then busy off. Best-effort."""
        for sock in list(box.sockets):
            try:
                await sock.send_json({"kind": "turn_end"})
                await sock.send_json({"kind": "busy", "on": False})
            except Exception:  # noqa: BLE001
                box.sockets.discard(sock)

    async def pub(box: "ChatBox", frame: dict[str, Any]) -> None:
        """Shorthand inside cancel/turn paths: publish to one chat."""
        await chat_pub(box, frame)

    async def do_cancel(box: "ChatBox", sock: "WebSocket") -> None:
        """The one Cancel path for every generation kind.

        - text turn: flip the client's cancel flag -- the stream loop
          stops at the next chunk (one token), the engine ends the turn
          gracefully, partial prose stays on the page and in history.
        - image generation: sd.cpp's HTTP surface is synchronous (this
          build has no /interrupt route; the async-jobs cancel refuses
          jobs that are already generating), so the only real stop is
          killing the sd-server process. The supervisor owns it; the
          runner's own error path then swaps the card back to the llm
          service -- the same recovery its timeout path already uses.
          The run's terminal frames are logged and the run retired, so
          no page sits wedged with dots on.
        - mid-swap (llm service down, /swap/llm in flight): the cancel
          lands as a notice and the swap completes; killing a swap would
          strand the card with neither service up.
        Broadcast the outcome so every tab's Cancel answers.
        """
        cancelled_image = False
        if GEN["active"] is not None:
            run = GEN["active"]
            # The image path is the ONLY hard-stop: this sd.cpp build has
            # no /interrupt route (the async-jobs cancel refuses jobs
            # already generating), so the real stop is the supervisor:
            # /swap/llm stops sd-server (which unblocks the runner's HTTP
            # request with a reset connection) and brings the llm service
            # back, so the card can never be stranded. The runner's own
            # error path then sees the swap already handled.
            # Music runs take the same route: the runner's engine dies
            # when the card is pulled, and its finally-clause swap-back
            # is idempotent, so /swap/llm here is safe and sufficient.
            kind_word = "music generation" if (
                run.get("request") and
                "caption" in run["request"]) else "image generation"
            try:
                with urllib.request.urlopen(
                        swap_api + "/state", timeout=2) as r:
                    state = json.loads(r.read())
            except Exception:  # noqa: BLE001 - no supervisor reachable
                state = {}
            await pub(box, {
                "kind": "notice",
                "text": f"cancel: bringing the GPU back to the llm "
                        f"service (the {kind_word} is stopped)"})
            # A music run must die via the FORCE cancel verb: plain
            # /swap/llm now REFUSES to kill a live music engine (the
            # 30-60 min generation owns the card), so an operator Cancel
            # is the one path allowed to take it down.
            cancel_path = ("/swap/music-cancel"
                           if "caption" in (run.get("request") or {})
                           else "/swap/llm")
            try:
                request = urllib.request.Request(
                    swap_api + cancel_path, data=b"{}",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=140) as r:
                    json.loads(r.read())
                await pub(box, {
                    "kind": "notice",
                    "text": "the llm service is back"})
            except Exception as exc:  # noqa: BLE001
                await pub(box, {
                    "kind": "error",
                    "text": f"cancel: could not restore the llm "
                            f"service: {exc}"})
            cancelled_image = True
            # Close the run record the same way every path does, so no
            # replay paints a wedged page. All synchronous, no awaits
            # between the frames and retirement (cancellation-proof).
            log_frame(run, {"kind": "notice",
                            "text": "generation cancelled"})
            log_frame(run, {"kind": "turn_end"})
            log_frame(run, {"kind": "busy", "on": False})
            run["ended"] = time.time()
            GEN["last"] = run
            GEN["active"] = None
            try:
                await pub(box, {"kind": "notice",
                                "text": "generation cancelled"})
                await pub(box, {"kind": "turn_end"})
                await pub(box, {"kind": "busy", "on": False})
            except Exception:  # noqa: BLE001
                pass
        else:
            # text turn: cooperative, through the client flag. The
            # engine's own turn_end + busy-off frames follow when the
            # stream unwinds; nothing more to send here.
            cancel = getattr(box.ui.session.client, "cancel_turn", None)
            if callable(cancel):
                cancel()
                await pub(box, {
                    "kind": "notice",
                    "text": "generation cancelled"})
            else:
                await pub(box, {
                    "kind": "notice",
                    "text": "this client cannot cancel mid-turn"})

        # Belt and braces: whatever path was taken, a cancelled
        # generation must never leave the page wedged in busy state.
        # A run whose executor thread is wedged in a dead urlopen (a
        # crashed image server, a down router) never reaches its own
        # finally -- the run was retired above, but the busy dots and
        # the send/cancel button key on the `busy` flag, so close the
        # visible turn here too. Idempotent with the frames above.
        if box.task is not None:
            box.task = None
        if GEN["active"] is None and not getattr(
                box.ui, "_inference_in_progress", False):
            try:
                await pub(box, {"kind": "turn_end"})
                await pub(box, {"kind": "busy", "on": False})
            except Exception:  # noqa: BLE001
                pass

    async def page(request):
        return HTMLResponse(PAGE)

    async def ws_endpoint(websocket: WebSocket) -> None:
        nonlocal saved_theme
        await websocket.accept()
        # A socket connects INTO a chat: the most recent conversation
        # for a plain page open, or the chat named in the URL (?chat=)
        # for a session-switch connect. From here every frame this
        # socket sees belongs to that chat, and every op it sends runs
        # against that chat.
        want = str(websocket.query_params.get("chat") or "").strip()
        box = (get_or_build_chat(want) if want else initial_chat())
        # A WebChat built before this loop runs holds a dead loop ref;
        # rebind the LIVE loop so call_soon_threadsafe renders work.
        box.ui._loop = asyncio.get_running_loop()
        # Connect = meta + this chat's frame log replay + live frames
        # for this chat only.
        await websocket.send_json({
            "kind": "meta", "model": box.ui.session.config.model,
            "protocol": box.ui.session.protocol,
            "num_ctx": box.ui.session.config.num_ctx,
            "theme": saved_theme or "k0b0l-dark",
            "reasoning": ("auto" if box.ui.session.config.thinking is None
                          else ("on" if box.ui.session.config.thinking
                                else "off")),
            "imagetool": not box.ui.session.config.no_image_tool,
            "musictool": not box.ui.session.config.no_music_tool,
            "chat": box.name,
            "restored": len(box.history) > 0,
        })
        # The transcript replay: everything this chat has rendered,
        # exactly as a live tab saw it. Bracketed so the page paints
        # it once.
        if box.history:
            await websocket.send_json({"kind": "restore_begin"})
            for frame in box.history:
                await websocket.send_json(frame)
            await websocket.send_json({"kind": "restore_end"})
        # A question this chat is still waiting on surfaces on join:
        # the model may have asked while no tab was viewing this chat.
        if box.pending_form:
            await websocket.send_json(box.pending_form)
        loop = asyncio.get_event_loop()
        box.sockets.add(websocket)

        async def run_turn(box: "ChatBox", text: str) -> None:
            """One text turn: swap-state probe, model reload notice, engine
            ask, tree bookkeeping, autosave. The body is the old inline
            block; the finally always closes the turn so a cancel can never
            wedge the page in busy state."""
            # -- tree bookkeeping: the turn runs against the ACTIVE
            # chain; record the node when the turn completes.
            tree_turn_user = text
            tree_prior = len(box.ui.session.messages)
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
            async with TURN_LOCK:
                if swap_state and swap_state.get("music"):
                    # A music generation owns the card for 30-60 min.
                    # Killing it to answer a chat message would destroy
                    # the run (this exact bug killed a healthy replay:
                    # engine exit -15 mid-AR). Refuse the turn with the
                    # reason; the music run restores the llm service
                    # itself when it lands.
                    await pub(box, {
                        "kind": "notice",
                        "text": "a music generation is running on the "
                                "GPU — text replies are paused until "
                                "it finishes (cancel the generation "
                                "to chat now)"})
                    await pub(box, {"kind": "turn_end"})
                    return
                if swap_state and not swap_state.get("llm", True):
                    await pub(box, {"kind": "busy", "on": True})
                    await pub(box, {
                        "kind": "notice",
                        "text": "the llm service is down — the GPU was "
                                "swapped to image generation; swapping "
                                "back (this can take a minute)..."})
                    try:
                        request = urllib.request.Request(
                            swap_api + "/swap/llm", data=b"{}",
                            headers={
                                "Content-Type": "application/json"})
                        with urllib.request.urlopen(
                                request, timeout=140) as r:
                            json.loads(r.read())
                        await pub(box, {
                            "kind": "notice",
                            "text": "the llm service is back"})
                    except Exception as exc:  # noqa: BLE001
                        await pub(box, {
                            "kind": "notice",
                            "text": f"could not restore the llm "
                                    f"service: {exc}"})
                    await pub(box, {"kind": "busy", "on": False})
            # A swapped-away model reloads its weights on this turn:
            # 10-40 s for the 8B, ~130 s for the 14B, and until the
            # first token there is no frame at all. Say so before the
            # silence, or the turn reads as hung.
            try:
                rows = client.model_details()
            except Exception:  # noqa: BLE001 - a probe must not kill the turn
                rows = []
            for row in rows:
                if (str(row.get("id")) == box.ui.session.config.model
                        and str(row.get("state", "")).lower()
                        == "unloaded"):
                    await pub(box, {
                        "kind": "notice",
                        "text": f"loading {box.ui.session.config.model} "
                                "(weights were swapped off the "
                                "card; this can take a minute)"})
                    break
            await loop.run_in_executor(None, box.ui.ask, text)
            # Tree bookkeeping: the engine appended the user message
            # AND its response to session.messages; the node's own
            # `user` text is tree_turn_user, so the node's content
            # is ONLY what followed the user row (dropping the
            # duplicate user entry messages_for would re-add).
            tree_msgs = box.ui.session.messages[tree_prior:]
            resp_only = tree_msgs[1:] if (
                tree_msgs and tree_msgs[0].get("role") == "user"
                and str(tree_msgs[0].get("content", "")) == tree_turn_user
            ) else tree_msgs
            nid = box.tree.append_turn(tree_turn_user, list(resp_only))
            await pub(box, {"kind": "tree", **box.tree.describe(),
                            "new_node": nid})
            # Autosave after every completed turn: "restore last
            # session" must mean the conversation as it stands, not
            # as it stood when the operator last remembered to save.
            try:
                box.sessions.autosave()
            except Exception:  # noqa: BLE001 - persistence is best-effort
                pass

        async def dispatch_turn(box: "ChatBox", text: str) -> None:
            """Wrap a slash command or a plain turn as a task, holding the
            TURN_LOCK only for the actual work (the lock is held while the
            engine streams, so two tasks cannot interleave)."""
            nonlocal_history_note = None  # placeholder to keep locals minimal
            if text.startswith("/"):
                await pub(box, {"kind": "you", "text": text})
                if text in ("/exit", "/quit", "/q"):
                    await pub(box, 
                        {"kind": "notice", "text": "the web session "
                         "stays; just close the tab"})
                    await pub(box, {"kind": "turn_end"})
                    return
                async with TURN_LOCK:
                    handled = await loop.run_in_executor(
                        None, box.ui.dispatch, text)
                    # The dispatch ran on the EXECUTOR: its line()
                    # renders were scheduled onto this loop via
                    # call_soon_threadsafe and may still be QUEUED.
                    # Closing the turn now ships turn_end BEFORE the
                    # command's own output, which then lands after the
                    # bracket -- invisible live, resurrected on replay
                    # (the "output belongs to the next command" bug).
                    # The documented flush: give the scheduled sends a
                    # real beat to run (ensure_future chains a second
                    # hop; sleep(0) does not reliably cross it).
                    await asyncio.sleep(0.2)
                    if not handled:
                        await pub(box, 
                            {"kind": "raw", "text": text})
                    if text.split()[0].lower() in ("/clear",):
                        # The shared session was reset server-side;
                        # EVERY tab's transcript must follow, and the
                        # frame log IS the new (empty) transcript.
                        box.history.clear()
                        await pub(box, {"kind": "clear"})
                        # /clear starts a FRESH document: pivot the
                        # autosave target back to the scratch slot so
                        # the new conversation can never write over a
                        # named session's file. The named graph keeps
                        # its own tree on disk, untouched.
                        box.sessions.start_fresh()
                        box.tree.__init__()
                        await pub(box, {"kind": "tree",
                                        **box.tree.describe()})
                    await pub(box, {"kind": "turn_end"})
                # Slash commands can change the live session (/clear,
                # /session load, /model): keep the autosave slot in
                # sync, or a reconnect restores a stale conversation.
                try:
                    box.sessions.autosave()
                except Exception:  # noqa: BLE001 - persistence is best-effort
                    pass
                return
            await pub(box, {"kind": "you", "text": text})
            await run_turn(box, text)

        async def run_image_op(box: "ChatBox", img: dict[str, Any]) -> None:
            """One image generation (the dialog / typed op) as its own
            task: the receive loop stays live, so a Cancel click from
            the same tab mid-generation is actually heard. Body is the
            old inline block; the op-level branch exits are returns
            now, and per-socket validation notices still go to the
            requesting tab."""
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
                return
            if mode:
                if GEN["active"] is not None:
                    await websocket.send_json({
                        "kind": "notice",
                        "text": "a generation is already "
                                "running; it will stream here"})
                    return
                # Every knob minus the bulky b64 payloads (a
                # retry re-uploads from the dialog state the
                # page still holds).
                req_echo = {k: v for k, v in img.items()
                            if k not in ("init_image_b64",
                                         "mask_b64")}
                my_run = {"frames": [], "request": req_echo,
                          "ended": 0.0, "box": box}
                GEN["active"] = my_run
                # The user's own row echoes the request whole.
                await bcast_pub({
                    "kind": "you",
                    "text": f"/image {mode} {model} {prompt}"})
                # An image turn is still a turn: the assistant
                # row anchors the busy dots, and an immediate
                # indeterminate bar says "generating" through
                # the minutes of swap + weight load before any
                # sampling step exists to report.
                await bcast_pub({"kind": "assistant"})
                await bcast_pub({"kind": "busy", "on": True})
                await bcast_pub({
                    "kind": "gen_progress",
                    "phase": "generating image — preparing "
                             "(swap / weight load)",
                    "step": 0, "total": 0})
                out_dir = Path(
                    str(box.ui.session.workspace.root)) / "images"
                out_dir.mkdir(parents=True, exist_ok=True)
                stamp = int(time.time())
                out_name = f"{model}-{stamp}"
                init_path = mask_path = None
                temp_paths: list[Path] = []
                if img.get("init_image_b64"):
                    raw = str(img["init_image_b64"])
                    if raw.startswith("data:"):
                        raw = raw.split(",", 1)[1]
                    init_path = out_dir / f"init-{stamp}.png"
                    init_path.write_bytes(
                        base64.b64decode(raw))
                    temp_paths.append(init_path)
                if img.get("mask_b64"):
                    raw = str(img["mask_b64"])
                    if raw.startswith("data:"):
                        raw = raw.split(",", 1)[1]
                    mask_path = out_dir / f"mask-{stamp}.png"
                    mask_path.write_bytes(
                        base64.b64decode(raw))
                    temp_paths.append(mask_path)

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
                # markers land on OUR console, not in a pipe the
                # harness reads -- poll the supervisor for
                # progress directly and stream frames while the
                # executor works.
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
                                await bcast_pub({
                                    "kind": "gen_progress",
                                    "phase": "sampling",
                                    "step": progress["step"],
                                    "total":
                                        progress["total"]})
                            if time.time() - last_change > 45:
                                # steps stalled: VAE decode
                                phase = ("finalizing "
                                         "(VAE decode / save)")
                        elif seen_steps:
                            phase = ("finalizing "
                                     "(VAE decode / save)")
                        else:
                            phase = ("generating image — "
                                     "preparing (swap / weight "
                                     "load)")
                        if phase and phase != last_phase:
                            last_phase = phase
                            await bcast_pub({
                                "kind": "gen_progress",
                                "phase": phase,
                                "step": 0, "total": 0})
                progress_task = asyncio.ensure_future(
                    poll_progress())
                ok = False
                try:
                    ok = await loop.run_in_executor(
                        None, run_advanced)
                except Exception as exc:  # noqa: BLE001
                    await bcast_pub({
                        "kind": "error",
                        "text": f"image generation failed: "
                                f"{exc}"})
                finally:
                    progress_task.cancel()
                    for temp in temp_paths:
                        try:
                            temp.unlink()
                        except OSError:
                            pass
                    # A cancelled run was closed by do_cancel (registry
                    # cleared, terminal frames sent): the executor's
                    # late error/no-image frames would paint "generation
                    # failed" AFTER the operator already saw the cancel
                    # land. Ownership check: while the run is ours it is
                    # GEN["active"]; do_cancel retires it to GEN["last"].
                    # If it is no longer active, we no longer own the
                    # close -- bail without sending anything.
                    if GEN["active"] is not my_run:
                        return
                    # Build the result frames NOW (the executor
                    # has finished or died; files are on disk).
                    after: list[dict[str, Any]] = []
                    if ok:
                        saved = sorted(out_dir.glob(
                            f"{model}-{stamp}*.png"))
                        for path in saved:
                            # Sidecar: the prompt and every knob,
                            # durable next to the image. The
                            # disk-replay reads it so copy means
                            # the prompt and retry works -- long
                            # after this run's frames are gone.
                            try:
                                path.with_suffix(
                                    ".prompt").write_text(
                                    json.dumps(req_echo,
                                               indent=2))
                            except OSError:
                                pass
                            after.append({
                                "kind": "image",
                                "path": f"images/{path.name}",
                                "request": req_echo})
                        after.append({
                            "kind": "raw",
                            "text": (f"image saved to images/"
                                     f"{out_name}.png"
                                     + (f" (+{len(saved) - 1} "
                                        f"more)"
                                        if len(saved) > 1
                                        else ""))})
                    else:
                        after.append({
                            "kind": "error",
                            "text": "image generation failed; "
                                    "see the server log"})
                    # Cancellation-proof close, all SYNCHRONOUS:
                    # result frames into the replay log, then the
                    # terminal frames, then retirement. No await
                    # between them, so a CancelledError (client
                    # died at the wrong moment) can never wedge
                    # GEN["active"] nor ship a half-run.
                    run_rec = my_run
                    for frame in after:
                        log_frame(run_rec, frame)
                    retire_run()
                    # Network copies only after the record is
                    # complete and closed -- best-effort.
                    try:
                        for frame in after:
                            await chat_pub(box, frame)
                        await chat_pub(box, {"kind": "turn_end"})
                        await chat_pub(box, {"kind": "busy", "on": False})
                    except Exception:  # noqa: BLE001
                        pass
                return

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
            # Plain path (typed /image with no mode knob): the
            # SAME run lifecycle as the dialog path -- the run
            # outlives this socket, frames broadcast to every
            # tab, and retirement is cancellation-proof.
            if GEN["active"] is not None:
                await websocket.send_json({
                    "kind": "notice",
                    "text": "a generation is already "
                            "running; it will stream here"})
                return
            req_echo_plain = dict(args)
            my_run = {"frames": [],
                      "request": req_echo_plain,
                      "ended": 0.0, "box": box}
            GEN["active"] = my_run
            # This WebChat's own renders (the tool's notices,
            # gen_progress, show_image) ARE the run's frames:
            # attach the replay log before the first echo, and
            # relay them to peer tabs live.
            box.ui.set_gen_log(my_run["frames"])
            box.ui.set_relay(None)  # the run carries its box; bcast_pub routes
            await bcast_pub({
                "kind": "you",
                "text": f"/image {model} {prompt}"})
            await bcast_pub({"kind": "busy", "on": True})
            await bcast_pub({
                "kind": "gen_progress",
                "phase": "generating image — preparing "
                         "(swap / weight load)",
                "step": 0, "total": 0})

            def run_image(args=args):
                return box.ui.session.workspace.call(
                    "generate_image", args)

            result = None
            try:
                result = await loop.run_in_executor(
                    None, run_image)
            except Exception as exc:  # noqa: BLE001
                await bcast_pub({
                    "kind": "error",
                    "text": f"image generation failed: {exc}"})
            finally:
                box.ui.set_gen_log(None)
                        # Result frames exist; log them SYNC, then retire,
                # then network-send. Same ordering contract as
                # the dialog path.
                after: list[dict[str, Any]] = []
                if result is not None and result.ok:
                    after.append({
                        "kind": "raw", "text": result.text})
                    # Sidecar for the disk-replay: the prompt and
                    # knobs, durable next to the newest image this
                    # run wrote (the raw text names it, but the
                    # file is easier to find by mtime). The OWNING
                    # CHAT rides along, so a rebuild seeds this
                    # image into the right chat.
                    try:
                        newest = max(
                            (chat_config.root / "images")
                            .glob("*.png"),
                            key=lambda p: p.stat().st_mtime)
                        req_chat = dict(req_echo_plain)
                        req_chat["chat"] = box.name
                        newest.with_suffix(".prompt").write_text(
                            json.dumps(req_chat, indent=2))
                        if newest.name not in box.images:
                            box.images.append(newest.name)
                    except (OSError, ValueError):
                        pass
                elif result is not None:
                    after.append({
                        "kind": "error", "text": result.text})
                if GEN["active"] is not my_run:
                    # Cancelled mid-flight: do_cancel already closed
                    # this run (terminal frames logged and sent, registry
                    # retired). The tool result's late error text must
                    # not overwrite the cancel the operator saw.
                    return
                run_rec = GEN["active"]
                for frame in after:
                    log_frame(run_rec, frame)
                retire_run()
                try:
                    for frame in after:
                        await chat_pub(box, frame)
                    await chat_pub(box, {"kind": "turn_end"})
                    await chat_pub(box, {"kind": "busy", "on": False})
                except Exception:  # noqa: BLE001
                    pass
            return

        async def run_music_op(box: "ChatBox", music: dict[str, Any]) -> None:
            """One music generation (the dialog / typed op) as its own
            task: the receive loop stays live, so a Cancel click from
            the same tab mid-generation is actually heard. The op goes
            through the model-facing generate_music tool, so the run
            lifecycle matches the plain image path exactly."""
            # HARD GUARD: ensure the GPU is truly free before starting.
            # The swap supervisor stops services, but a running llama
            # model keeps its VRAM until evicted. Force unload first.
            try:
                target_url = swap_api + "/swap/music"
                print(f"DEBUG: calling swap_api={target_url}", file=sys.stderr, flush=True)
                request = urllib.request.Request(
                    target_url, data=b"{}",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=180) as r:
                    json.loads(r.read())
            except Exception as exc:  # noqa: BLE001
                await websocket.send_json({
                    "kind": "error",
                    "text": f"failed to free the GPU for music: {exc}"})
                return
            # Wait for the card to report free
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                            swap_api + "/state", timeout=2) as r:
                        state = json.loads(r.read())
                    if not state.get("llm", True):
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            else:
                await websocket.send_json({
                    "kind": "error",
                    "text": "GPU did not free in time; try again"})
                return
            caption = str(music.get("caption") or "").strip()
            if not caption:
                await websocket.send_json({
                    "kind": "notice",
                    "text": "music generation needs a caption"})
                return
            args: dict[str, Any] = {"caption": caption}
            lyrics = str(music.get("lyrics") or "").strip()
            if lyrics:
                args["lyrics"] = lyrics
            try:
                duration = int(music.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            if duration > 0:
                args["duration"] = duration
            try:
                steps = int(music.get("steps") or 0)
            except (TypeError, ValueError):
                steps = 0
            if steps > 0:
                args["steps"] = steps
            try:
                seed = int(music.get("seed") or 0)
            except (TypeError, ValueError):
                seed = 0
            if seed:
                args["seed"] = seed
            try:
                decode_graph_arena_mb = int(music.get("decode_graph_arena_mb") or 0)
            except (TypeError, ValueError):
                decode_graph_arena_mb = 0
            if decode_graph_arena_mb:
                args["decode_graph_arena_mb"] = decode_graph_arena_mb
            try:
                guidance_scale = float(music.get("guidance_scale") or 0)
            except (TypeError, ValueError):
                guidance_scale = 0
            if guidance_scale:
                args["guidance_scale"] = guidance_scale
            try:
                ar_guidance_scale = float(music.get("ar_guidance_scale") or 0)
            except (TypeError, ValueError):
                ar_guidance_scale = 0
            if ar_guidance_scale:
                args["ar_guidance_scale"] = ar_guidance_scale
            try:
                top_k = int(music.get("top_k") or 0)
            except (TypeError, ValueError):
                top_k = 0
            if top_k:
                args["top_k"] = top_k
            try:
                flow_uncond_interval = int(music.get("flow_uncond_interval") or 0)
            except (TypeError, ValueError):
                flow_uncond_interval = 0
            if flow_uncond_interval:
                args["flow_uncond_interval"] = flow_uncond_interval
            try:
                flow_uncond_warmup = int(music.get("flow_uncond_warmup") or 0)
            except (TypeError, ValueError):
                flow_uncond_warmup = 0
            if flow_uncond_warmup:
                args["flow_uncond_warmup"] = flow_uncond_warmup
            quality = str(music.get("quality") or "").strip().lower()
            if quality in ("stock", "hq"):
                args["quality"] = quality
            if GEN["active"] is not None:
                await websocket.send_json({
                    "kind": "notice",
                    "text": "a generation is already "
                            "running; it will stream here"})
                return
            req_echo = dict(args)
            my_run = {"frames": [], "request": req_echo,
                      "ended": 0.0, "box": box}
            GEN["active"] = my_run
            box.ui.set_gen_log(my_run["frames"])
            box.ui.set_relay(None)
            await bcast_pub({
                "kind": "you",
                "text": "/music " + caption})
            await bcast_pub({"kind": "busy", "on": True})
            await bcast_pub({
                "kind": "gen_progress",
                "phase": "generating music — freeing the GPU "
                         "(swap / weight load)",
                "step": 0, "total": 0})

            def run_music(args=args):
                return box.ui.session.workspace.call(
                    "generate_music", args)

            result = None
            try:
                result = await loop.run_in_executor(
                    None, run_music)
            except Exception as exc:  # noqa: BLE001
                await bcast_pub({
                    "kind": "error",
                    "text": f"music generation failed: {exc}"})
            finally:
                box.ui.set_gen_log(None)
                after: list[dict[str, Any]] = []
                if result is not None and result.ok:
                    after.append({
                        "kind": "raw", "text": result.text})
                    # The audio frame rides with the result frames --
                    # built synchronously here, never scheduled live:
                    # the clip must paint BEFORE turn_end closes the
                    # turn (a live-scheduled send loses that race and
                    # the tab disconnects without ever seeing it).
                    clip_rel = None
                    try:
                        newest = max(
                            (chat_config.root / "audio")
                            .glob("*.wav"),
                            key=lambda p: p.stat().st_mtime)
                        clip_rel = f"audio/{newest.name}"
                    except (OSError, ValueError):
                        pass
                    if clip_rel:
                        after.append({
                            "kind": "audio", "path": clip_rel,
                            "request": req_echo})
                    # Sidecar next to the clip so the disk-replay can
                    # rebuild the request (copy = caption+lyrics,
                    # retry = the full op), long after this run's
                    # frames are gone. The OWNING CHAT rides along.
                    try:
                        newest = max(
                            (chat_config.root / "audio")
                            .glob("*.wav"),
                            key=lambda p: p.stat().st_mtime)
                        req_chat = dict(req_echo)
                        req_chat["chat"] = box.name
                        newest.with_suffix(".prompt").write_text(
                            json.dumps(req_chat, indent=2))
                        if newest.name not in box.audio:
                            box.audio.append(newest.name)
                    except (OSError, ValueError):
                        pass
                elif result is not None:
                    after.append({
                        "kind": "error", "text": result.text})
                if GEN["active"] is not my_run:
                    # Cancelled mid-flight: do_cancel already closed
                    # this run; the late result must not overwrite it.
                    return
                run_rec = GEN["active"]
                for frame in after:
                    log_frame(run_rec, frame)
                retire_run()
                try:
                    for frame in after:
                        await chat_pub(box, frame)
                    await chat_pub(box, {"kind": "turn_end"})
                    await chat_pub(box, {"kind": "busy", "on": False})
                except Exception:  # noqa: BLE001
                    pass
            return

        try:
            while True:
                message = await websocket.receive_json()
                # Heartbeat: the page pings to prove the socket is
                # alive (Android's freezer kills tabs silently); this
                # echo is the pong its watchdog keys on.
                if isinstance(message, dict) and message.get("ping"):
                    await websocket.send_json({"kind": "pong"})
                    continue
                # -- session control messages first: they carry no "text",
                # so the empty-text guard below must never swallow them
                # (it did, and every panel button silently died).
                op = message.get("session")
                if isinstance(op, dict):
                    verb = str(op.get("verb", ""))
                    name = str(op.get("name", "")).strip()
                    if verb == "list":
                        rows = []
                        by_name: dict[str, dict[str, Any]] = {}
                        # Files first (they carry turns/preview meta)
                        for row in box.sessions.listing():
                            if row.get("name") == SessionManager.AUTOSAVE:
                                continue
                            by_name[row["name"]] = row
                        # Live chats without files yet (a brand-new chat
                        # mid-turn autosaves only at turn end) are real
                        # conversations: list them too, newest-turn-
                        # first ordering kept by sorting on mtime.
                        for name, live_box in CHATS.items():
                            if name == SessionManager.AUTOSAVE:
                                continue
                            if name not in by_name:
                                by_name[name] = {
                                    "name": name,
                                    "mtime": time.time(),
                                    "messages": len(
                                        live_box.ui.session.messages),
                                    "turns": live_box.ui.session.turns,
                                    "preview": "(open)",
                                }
                        for row in by_name.values():
                            pending = CHATS.get(row["name"])
                            row["live"] = pending is not None
                            row["asking"] = bool(
                                pending is not None
                                and pending.pending_form is not None)
                            rows.append(row)
                        rows.sort(key=lambda r: r.get("mtime", 0),
                                  reverse=True)
                        await websocket.send_json({
                            "kind": "session_list",
                            "sessions": rows})
                        continue
                    if verb == "new":
                        # A brand-new chat: build it, rebind THIS socket,
                        # repaint the socket alone. Other tabs keep
                        # their own conversations. Saved immediately --
                        # a chat exists the moment it is opened, so it
                        # survives restarts and shows in the listing
                        # even before its first turn.
                        newbox = build_chat(fresh_chat_name())
                        box.sockets.discard(websocket)
                        box = newbox
                        box.sockets.add(websocket)
                        try:
                            box.sessions.autosave()
                        except Exception:  # noqa: BLE001
                            pass
                        await repaint_sockets(box, {websocket})
                        await websocket.send_json({"kind": "meta",
                                                   "model": box.ui.session.config.model,
                                                   "protocol": box.ui.session.protocol,
                                                   "num_ctx": box.ui.session.config.num_ctx,
                                                   "theme": saved_theme or "k0b0l-dark",
                                                   "chat": box.name})
                        continue
                    if verb == "load":
                        # Chat semantics: loading a session REBINDS this
                        # socket to that chat. The chat builds from its
                        # file when it is not already live (a live chat
                        # wins -- it may hold unsaved turns). Every
                        # OTHER tab keeps whatever it was viewing: no
                        # bleed-over, no global repaint.
                        target = get_or_build_chat(name)
                        box.sockets.discard(websocket)
                        box = target
                        box.sockets.add(websocket)
                        await repaint_sockets(box, {websocket},
                                              broadcast_tree=True)
                        await pub(box, {"kind": "meta",
                                       "chat": box.name})
                        continue
                    if verb == "save":
                        note = box.sessions.save(name or box.name)
                        await websocket.send_json({
                            "kind": "notice", "text": note})
                        continue
                    if verb == "delete":
                        ok, note = box.sessions.delete(name)
                        await websocket.send_json({
                            "kind": "notice", "text": note})
                        # A deleted chat must not resurrect: drop it from
                        # the registry, and rebind any of its sockets
                        # (including ours) to a fresh chat so the next
                        # turn cannot autosave the zombie back.
                        if ok:
                            dead = CHATS.pop(name, None)
                            if dead is not None:
                                for sock in list(dead.sockets):
                                    fresh = build_chat(fresh_chat_name())
                                    dead.sockets.discard(sock)
                                    fresh.sockets.add(sock)
                                    await repaint_sockets(fresh, {sock})
                                    if sock is websocket:
                                        box = fresh
                        continue

                img = message.get("image")
                if isinstance(img, dict):
                    # The op runs as a TASK, not inline: the receive
                    # loop stays live so Cancel is hearable mid-run
                    # (same reason text turns became tasks).
                    task = asyncio.ensure_future(run_image_op(box, img))
                    box.task = task
                    def _retire_img(t, b=box):
                        if b.task is t:
                            b.task = None
                    task.add_done_callback(_retire_img)
                    continue

                music_msg = message.get("music")
                if isinstance(music_msg, dict):
                    # Same task discipline as image ops: a music run is
                    # minutes long and Cancel must be hearable mid-run.
                    task = asyncio.ensure_future(
                        run_music_op(box, music_msg))
                    box.task = task
                    def _retire_music(t, b=box):
                        if b.task is t:
                            b.task = None
                    task.add_done_callback(_retire_music)
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
                            # keep the owning chat's list honest
                            name = Path(rel).name
                            if name in box.images:
                                box.images.remove(name)
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
                        "kind": "meta", "model": box.ui.session.config.model,
                        "protocol": box.ui.session.protocol,
                        "num_ctx": box.ui.session.config.num_ctx,
                        "theme": wanted_theme, "chat": box.name})
                    continue
                # -- skills & core: the operator surface of what the
                # session already knows (skills ride the system message;
                # CORE.md is the identity it speaks through).
                if message.get("skills_list") is True:
                    skills = box.ui._skills()
                    rows = []
                    for skill in skills:
                        rows.append({
                            "name": skill.name,
                            "description": getattr(
                                skill, "description", "") or "",
                            "enabled": skill.name
                            in getattr(box.ui, "_selected_skills", []),
                        })
                    await websocket.send_json({
                        "kind": "skills", "skills": rows,
                        "active": list(getattr(box.ui, "_selected_skills",
                                               []))})
                    continue
                if isinstance(message.get("skills_set"), dict):
                    change = message["skills_set"]
                    name = str(change.get("name", "")).strip()
                    on = bool(change.get("enabled"))
                    if not name:
                        await websocket.send_json({
                            "kind": "notice",
                            "text": "skills_set needs a name"})
                        continue
                    sel = getattr(box.ui, "_selected_skills", [])
                    if on and name not in sel:
                        sel.append(name)
                    if not on and name in sel:
                        sel.remove(name)
                    # Same contract as /skills enable|disable: the next
                    # system message carries the new doctrine set.
                    box.ui.session.reset()
                    box.history.clear()
                    await pub(box, {"kind": "clear"})
                    await pub(box, {"kind": "notice", "text":
                                    f"skills active: "
                                    f"{', '.join(sel) or '(none)'}"})
                    await pub(box, {"kind": "turn_end"})
                    box.sessions.autosave()
                    continue
                # -- skill authoring: read / write / delete ------------
                # The same contract as the TUI's SkillsEditor: the raw
                # SKILL.md (frontmatter + body) is the portability
                # contract, edits write the file the skill lives in,
                # NEW skills go to the workspace (never the operator's
                # profile dir -- read-mostly from the chat's point of
                # view; a workspace copy is the override path anyway),
                # and a save validates frontmatter before it lands.
                if message.get("skills_read") is not None:
                    from rt_harness.soul import load_soul  # noqa: F401

                    name = str(message.get("skills_read") or "").strip()
                    from rt_harness.skills import list_skills, read_skill

                    skills = list_skills(chat_config.root)
                    skill = read_skill(skills, name) if name else None
                    if skill is None:
                        await websocket.send_json({
                            "kind": "skill_text", "name": name,
                            "text": "", "path": "",
                            "new": True,
                            "error": f"no skill named {name!r}"})
                        continue
                    try:
                        text = skill.path.read_text(encoding="utf-8")
                    except OSError as exc:
                        await websocket.send_json({
                            "kind": "skill_text", "name": skill.name,
                            "text": "", "path": str(skill.path),
                            "error": f"could not read: {exc}"})
                        continue
                    await websocket.send_json({
                        "kind": "skill_text", "name": skill.name,
                        "text": text, "path": str(skill.path)})
                    continue
                if isinstance(message.get("skills_write"), dict):
                    req = message["skills_write"]
                    name = str(req.get("name") or "").strip()
                    text = str(req.get("text") or "")
                    create = bool(req.get("create"))
                    from rt_harness.skills import (
                        _FRONTMATTER_RE, _parse_yaml, list_skills,
                        read_skill)

                    # Validate exactly what the TUI editor validates: a
                    # save that silently produced an undiscoverable or
                    # unusable skill would be a lie.
                    problem = ""
                    meta: dict[str, Any] = {}
                    body = text
                    match = _FRONTMATTER_RE.match(text)
                    if match:
                        meta = _parse_yaml(match.group(1))
                        body = text[match.end():]
                    if not str(meta.get("name") or "").strip():
                        problem = ("frontmatter needs a name: "
                                   "(no frontmatter, or name: empty)")
                    elif not str(meta.get("description") or "").strip():
                        problem = ("frontmatter needs a description: "
                                   "one line saying when it applies")
                    elif not body.strip():
                        problem = "the body is empty — a skill is its procedure"
                    if problem:
                        await websocket.send_json({
                            "kind": "skill_saved", "ok": False,
                            "error": problem})
                        continue
                    skills = list_skills(chat_config.root)
                    skill = read_skill(skills, name) if name else None
                    if create or skill is None:
                        # New skill: workspace dir, slug from the
                        # FRONTMATTER name -- the name the operator
                        # actually wrote. The request's `name` field is
                        # the editor's stale slot (the template's
                        # my-skill on a fresh +new); slugging from it
                        # buried one operator's art_director under
                        # skills/my-skill/ while the file's frontmatter
                        # said art_director -- discoverable, but at a
                        # nonsense path, and unrenamable from the UI.
                        import re as _re

                        fm_name = str(meta.get("name") or "").strip()
                        slug = _re.sub(r"[^a-z0-9-]+", "-",
                                       fm_name.lower()).strip("-") \
                            or "my-skill"
                        root_dir = Path(chat_config.root) / "skills"
                        base, n = slug, 2
                        while (root_dir / base).exists() \
                                and read_skill(list_skills(
                                    chat_config.root), base) is None:
                            base = f"{slug}-{n}"
                            n += 1
                        target = root_dir / base / "SKILL.md"
                        target.parent.mkdir(parents=True, exist_ok=True)
                    else:
                        target = skill.path
                        target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        target.write_text(text, encoding="utf-8")
                    except OSError as exc:
                        await websocket.send_json({
                            "kind": "skill_saved", "ok": False,
                            "error": f"could not write: {exc}"})
                        continue
                    await websocket.send_json({
                        "kind": "skill_saved", "ok": True,
                        "name": str(meta.get("name")),
                        "path": str(target)})
                    # The index the model sees changed: rebuild prompts
                    # (same contract as skills_set -- session.reset so
                    # the next system message carries the new set).
                    box.ui.session.reset()
                    continue
                if isinstance(message.get("skills_delete"), dict):
                    req = message["skills_delete"]
                    name = str(req.get("name") or "").strip()
                    from rt_harness.skills import list_skills, read_skill

                    skills = list_skills(chat_config.root)
                    skill = read_skill(skills, name) if name else None
                    if skill is None:
                        await websocket.send_json({
                            "kind": "skill_saved", "ok": False,
                            "error": f"no skill named {name!r}"})
                        continue
                    try:
                        if skill.path.parent.is_dir() \
                                and (skill.path.parent / "SKILL.md") \
                                == skill.path:
                            import shutil

                            shutil.rmtree(skill.path.parent)
                        else:
                            skill.path.unlink(missing_ok=True)
                    except OSError as exc:
                        await websocket.send_json({
                            "kind": "skill_saved", "ok": False,
                            "error": f"could not delete: {exc}"})
                        continue
                    # The pinned set must not keep a deleted name.
                    sel = getattr(box.ui, "_selected_skills", [])
                    if name in sel:
                        sel.remove(name)
                    await websocket.send_json({
                        "kind": "skill_saved", "ok": True,
                        "deleted": name})
                    box.ui.session.reset()
                    continue
                if message.get("core_show") is True:
                    from rt_harness.soul import load_soul

                    soul = load_soul(box.ui.session.config,
                                     box.ui.session.workspace.root)
                    await websocket.send_json({
                        "kind": "core", "text": soul.text if soul.loaded
                        else "", "path": str(soul.path or ""),
                        "loaded": soul.loaded})
                    continue
                # -- the conversation graph: branch and switch ---------
                if isinstance(message.get("tree_branch"), dict):
                    req = message["tree_branch"]
                    try:
                        from_node = int(req.get("from", 0))
                    except (TypeError, ValueError):
                        from_node = 0
                    if from_node not in box.tree.nodes:
                        await websocket.send_json({
                            "kind": "notice",
                            "text": f"no node {from_node} to branch from"})
                        continue
                    async with TURN_LOCK:
                        fork = box.tree.nodes[from_node]
                        box.tree.branch(from_node)
                        # The new path's context = the chain to the
                        # fork's parent. The NEXT prompt becomes the
                        # sibling node; nothing empty is recorded.
                        sysmsgs = [m for m in box.ui.session.messages
                                   if m.get("role") == "system"]
                        box.ui.session.messages[:] = sysmsgs + \
                            box.tree.messages_for(box.tree.active)
                        await pub(box, {"kind": "tree",
                                        **box.tree.describe(),
                                        "forked_from": from_node})
                        await pub(box, {
                            "kind": "notice",
                            "text": "branched at that turn: your next "
                                    "prompt opens the new path; the old "
                                    "one stays reachable from the branch "
                                    "chips"})
                    continue
                if isinstance(message.get("tree_switch"), dict):
                    req = message["tree_switch"]
                    try:
                        nid = int(req.get("node", 0))
                    except (TypeError, ValueError):
                        nid = 0
                    if nid not in box.tree.nodes:
                        await websocket.send_json({
                            "kind": "notice",
                            "text": f"no node {nid} to switch to"})
                        continue
                    async with TURN_LOCK:
                        if not box.tree.switch(nid):
                            await websocket.send_json({
                                "kind": "notice",
                                "text": f"cannot switch to {nid}"})
                            continue
                        # Repaint every tab of THIS chat with the new
                        # active chain: the engine messages BECOME that
                        # chain. Other chats are untouched.
                        sysmsgs = [m for m in box.ui.session.messages
                                   if m.get("role") == "system"]
                        box.ui.session.messages[:] = sysmsgs + \
                            box.tree.messages_for(box.tree.active)
                        await repaint_sockets(box, set(box.sockets),
                                              broadcast_tree=True,
                                              switched_to=nid)
                        box.sessions.autosave()
                    continue
                if message.get("tree_show") is True:
                    await websocket.send_json({"kind": "tree",
                                                **box.tree.describe()})
                    continue
                # Cancel is checked BEFORE the empty-text guard on purpose:
                # it carries no text field (the panel-button rule -- an
                # op dispatched after `if not text: continue` is silently
                # swallowed, exactly the class that killed every panel
                # button when sessions shipped).
                # A form answer from the ask_user_choice dialog: the
                # executor thread is blocked in ask_form waiting on the
                # event. Before the empty-text guard like every op
                # verb. A late/second answer when nobody is waiting is
                # ignored (the wait may have timed out already).
                if isinstance(message.get("form_answer"), dict):
                    # The answer may come from a tab NOT viewing the
                    # owning chat (the ask_form surface broadcasts to
                    # every socket). Route by the form's chat name, and
                    # close the dialog on every OTHER socket too.
                    want_chat = str(
                        message["form_answer"].get("chat") or "") \
                        or box.name
                    target = CHATS.get(want_chat)
                    wait = getattr(target.ui, "_form_wait", None) \
                        if target is not None \
                        else getattr(box.ui, "_form_wait", None)
                    if wait is not None:
                        done, holder = wait
                        holder["answer"] = \
                            message["form_answer"].get("values") or None
                        done.set()
                        if target is not None:
                            target.pending_form = None
                        closer = {"kind": "ask_form_close",
                                  "chat": want_chat,
                                  "answered": True}
                        await broadcast_all(closer)
                    else:
                        await websocket.send_json({
                            "kind": "notice",
                            "text": "no form is waiting "
                                    "(it may have timed out)"})
                    continue
                if message.get("cancel") is True:
                    if box.task is not None and \
                            not box.task.done():
                        await do_cancel(box, websocket)
                        continue
                    await websocket.send_json({
                        "kind": "notice",
                        "text": "nothing is running"})
                    continue
                text = str(message.get("text", "")).strip()
                if not text:
                    continue
                # A turn (or slash command) becomes a task: the receive
                # loop stays live, so a Cancel click from this same tab
                # can be heard mid-generation. Re-submits while a turn
                # runs are refused with a notice (one engine turn at a
                # time; TURN_LOCK serializes anyway, but the operator
                # should hear why their message did not go).
                begin = getattr(box.ui.session.client, "begin_turn", None)
                if callable(begin):
                    begin()
                task = asyncio.ensure_future(dispatch_turn(box, text))
                box.task = task
                # Free the handle when the task ends so Cancel knows
                # there is nothing left to stop (a done task's Cancel
                # click answers "nothing is running" honestly).
                def _retire(t, b=box):
                    if b.task is t:
                        b.task = None
                task.add_done_callback(_retire)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # The ASGI state machine raises RuntimeError("WebSocket is
            # not connected...") when the socket is already gone (the
            # logged failure class from Android's tab freezer: the
            # receive loop outlives the connection). Treat exactly like
            # a disconnect -- the finally below drops the socket.
            pass
        finally:
            # The chat outlives this socket: drop the socket from its
            # viewer set; the conversation (and every other tab
            # viewing it) keeps going.
            for chat in CHATS.values():
                chat.sockets.discard(websocket)

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
