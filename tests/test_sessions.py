"""Sessions and options: save/load commands and the Options screen."""
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness.config import Config  # noqa: E402
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
    session_dir = Path.home() / ".k0b0l-sessions"
    # sandbox: point at a temp session dir by monkeypatching the helper used
    # by both the UI and infotools.
    import rt_harness.infotools as infotools
    orig = infotools._session_dir
    infotools._session_dir = lambda: Path(tmpdir)
    try:
        config = Config.from_env({**os.environ, "CHAT_ROOT": str(tmpdir)})
        out = io.StringIO()
        ui = PlainChat(OllamaClient(config.ollama_url), config, tool_output="summary", out=out)

        # no session yet, save gives an empty one on a fresh UI (system msg only)
        ui.dispatch("/session save demo")
        text = out.getvalue()
        check("save reports the path", "session saved" in text)
        check("the session file exists", (Path(tmpdir) / "demo.json").exists())

        # seed a second with two messages, then load from a fresh UI
        (Path(tmpdir) / "demo.json").write_text(
            '{"messages": [{"role":"system","content":"sys"},{"role":"user","content":"q1"}]}'
        )
        out2 = io.StringIO()
        ui2 = PlainChat(OllamaClient(config.ollama_url), config, tool_output="summary", out=out2)
        ui2.dispatch("/session load demo")
        loaded = out2.getvalue()
        check("load restores the message count", "loaded (2 messages)" in loaded, loaded[-200:])
        check("the session is on the second UI", ui2.session.messages[1]["content"] == "q1")
        ui2.dispatch("/session list")
        listing = out2.getvalue()
        check("list shows the saved name", "saved sessions: demo" in listing, listing[-200:])
    finally:
        infotools._session_dir = orig

print(f"{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
