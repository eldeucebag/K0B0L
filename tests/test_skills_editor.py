#!/usr/bin/env python3
"""The skills editor modal: create, edit, save, delete, without a terminal.

Drives the real SkillsEditor in a real Textual app via Pilot: a new skill
from the template, a save that must validate (first refused, then fixed), the
file on disk after F2, the list re-scanned, and a delete that removes the
directory. The plain front end's refusal is checked too.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness import skills  # noqa: E402
from rt_harness.config import Config  # noqa: E402
from rt_harness.textual_chat import SkillsEditor  # noqa: E402

OK = 0
FAIL = 0


def check(label, ok, detail=""):
    global OK, FAIL
    if ok:
        OK += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {label}  {detail}")


async def main(tmpdir: str) -> None:
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    root = Path(tmpdir)
    workspace = root / "workspace"
    (workspace / "skills").mkdir(parents=True)

    class Harness(App):
        CSS = "#skill-editor-wrap { width: 96%; height: 88%; }"

        def compose(self) -> ComposeResult:
            yield Static("host")

    config = Config.from_env({**os.environ, "CHAT_ROOT": str(workspace),
                               "API_URL": "http://127.0.0.1:9/v1"})

    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        editor = SkillsEditor(config)
        app.push_screen(editor)
        await pilot.pause()

        # Empty workspace: the editor bootstraps a new template.
        check("an empty workspace opens on a new template",
              editor.current is not None and editor.current.parent.exists() is False)
        body = app.screen.query_one("#skill-body")
        check("the template is in the editor", "name: my-skill" in body.text,
              body.text[:60])

        # Save as-is: valid frontmatter, so it must write the file.
        await pilot.press("f2")
        await pilot.pause()
        check("a valid template saves", editor.current is not None
              and editor.current.is_file(), str(editor.current))
        found = skills.list_skills(workspace)
        check("the saved skill is discoverable",
              any(s.name == "my-skill" for s in found),
              str([s.name for s in found]))

        # Now break it: no description. The save must refuse.
        body.text = (
            "---\nname: broken\n---\n\n# Broken\n\nNo description here.\n"
        )
        await pilot.press("f2")
        await pilot.pause()
        status_widget = app.screen.query_one("#skill-status")
        status = str(getattr(status_widget, "_content", "")
                     or getattr(status_widget, "render", lambda: "")())
        check("a save without a description is refused",
              "description" in status, status[:120])

        # Fix it and save; the file on disk must carry the fix.
        body.text = (
            "---\nname: fixed\n"
            "description: After the fix.\n---\n\n# Fixed\n\nIt works now.\n"
        )
        await pilot.press("f2")
        await pilot.pause()
        check("the fixed save lands on disk",
              editor.current is not None
              and "After the fix." in editor.current.read_text())

        # Delete the open skill; the directory must go.
        doomed = editor.current
        await pilot.press("ctrl-d")
        await pilot.pause()
        check("delete removes the skill directory",
              doomed is not None and not doomed.parent.exists(),
              str(doomed))

        # A named open: /skills edit my-skill selects it.
        (workspace / "skills" / "probe").mkdir()
        (workspace / "skills" / "probe" / "SKILL.md").write_text(
            "---\nname: probe\ndescription: selection test.\n---\n\n# Probe\n\nx\n"
        )
        editor2 = SkillsEditor(config, select="probe")
        app.push_screen(editor2)
        await pilot.pause()
        check("opening by name selects the skill",
              editor2.current is not None
              and editor2.current.parent.name == "probe",
              str(editor2.current))

        # Esc dismisses.
        await pilot.press("escape")
        await pilot.pause()
        check("esc closes the editor", app.screen is not editor2)

    # -- the plain front end refuses cleanly ---------------------------------
    from rt_harness.client import OllamaClient  # noqa: E402
    from rt_harness.tui import PlainChat  # noqa: E402

    out = io.StringIO()
    ui = PlainChat(OllamaClient("http://127.0.0.1:9"), config,
                   tool_output="summary", out=out)
    ui.dispatch("/skills edit")
    check("plain mode explains it cannot host the editor",
          "Textual front end" in out.getvalue(), out.getvalue()[-120:])


with TemporaryDirectory() as td:
    asyncio.run(main(td))

print(f"\n{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
