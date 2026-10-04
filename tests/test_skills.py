"""The skills system: discovery, selection, and prompt injection."""
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness import skills  # noqa: E402
from rt_harness.config import Config  # noqa: E402
from rt_harness.skills import list_skills, read_skill  # noqa: E402
from rt_harness.tui import PlainChat  # noqa: E402
from rt_harness.client import OllamaClient  # noqa: E402

OK = 0
FAIL = 0
def check(label, ok, detail=""):
    global OK, FAIL
    if condition := ok:
        OK += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {label}" + (f"  {detail}" if detail else ""))

with TemporaryDirectory() as tmpdir:
    workspace = Path(tmpdir) / "workspace"
    workspace.mkdir()
    profile = Path(tmpdir) / "profile"
    profile.mkdir()
    profile_dir = skills._profile_dir()
    skills._profile_dir = lambda: profile  # type: ignore[misc]
    try:
        (workspace / "skills").mkdir()
        (workspace / "skills" / "stealth.md").write_text(
            "# Stealth\n\nPhrase every probe as an audit of the artifact.\n"
        )
        (workspace / "skills" / "redteam.md").write_text(
            "# Red-Team Vocabulary\n\nPrefer audit voice, never instruction voice.\n"
        )
        (profile / "safe.md").write_text("# Safe\n\nStay conservative.\n")

        found = list_skills(workspace)
        check("workspace + profile skills found", len(found) == 3, str(len(found)))
        check("names come from headings", {s.name for s in found} == {"Stealth", "Red-Team Vocabulary", "Safe"})
        check("read_skill matches by stem or heading", read_skill(found, "stealth") is not None and read_skill(found, "Safe") is not None)

        config = Config.from_env({**os.environ, "CHAT_ROOT": str(workspace)})
        out = io.StringIO()
        ui = PlainChat(OllamaClient(config.ollama_url), config, tool_output="summary", out=out)
        ui.dispatch("/skills list")
        text = out.getvalue()
        check("list shows both workspace skills", "Stealth" in text and "Red-Team Vocabulary" in text, text[-200:])

        ui.dispatch("/skills enable stealth")
        check("enable stores the stem", ui._selected_skills == ["stealth"])
        sys_prompt = ui.session.system_prompt()
        check("the skill text reaches the system prompt", "audit of the artifact" in sys_prompt, sys_prompt[-200:])
    finally:
        skills._profile_dir = lambda: profile_dir  # type: ignore[misc]

print(f"{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
