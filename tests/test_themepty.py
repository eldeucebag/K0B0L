"""End-to-end themes: does /theme reach the real Textual UI and stick?

Boots the actual chat in a pty, switches themes, and reads the escape stream
for the colours Textual actually painted — a theme that renders in a unit test
but paints nothing is not a theme. Then it restarts the app to prove the choice
survived.

Needs a reachable Ollama (``CHAT_TEST_URL``, default the :11436 instance) for
the same reason every pty suite here does: ``run_textual`` pings it first.
HOME is redirected into .scratch so the run cannot touch the operator's own
``~/.k0b0l-chat-ui.json`` or chat history.
"""
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
URL = os.environ.get("CHAT_TEST_URL", "http://127.0.0.1:11436")
SCRATCH = Path(__file__).resolve().parent / ".scratch"
ROOT = SCRATCH / "themepty"
HOME = SCRATCH / "themepty-home"
REAL_HOME = os.environ.get("HOME") or str(Path.home())
shutil.rmtree(ROOT, ignore_errors=True)
shutil.rmtree(HOME, ignore_errors=True)
ROOT.mkdir(parents=True, exist_ok=True)
HOME.mkdir(parents=True, exist_ok=True)

fails: list[str] = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)


ENV = {
    **os.environ,
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "HOME": str(HOME),
    # Textual is a *user* install (~/.local/lib/python3.10/site-packages), so
    # redirecting HOME would hide it from the child unless the user base moves
    # with it.
    "PYTHONUSERBASE": str(Path(REAL_HOME) / ".local"),
    "OLLAMA_URL": URL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_PREDICT": "64",
}

#: #00FF41 as Textual writes it in a truecolor SGR foreground sequence.
MATRIX_FG = "38;2;0;255;65"
#: #A80000 as a background sequence (the hotdog-3x window colour).
HOTDOG_BG = "48;2;168;0;0"

#: SGR and other escape sequences, so a check can read the text Textual painted
#: rather than the byte stream that painted it.
ANSI = re.compile(rb"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


class Session:
    """One pty-hosted chat, with the drain/wait helpers these suites share."""

    def __init__(self):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, "thinlizzy.py", "--chat"],
            cwd=REPO, env=ENV, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, start_new_session=True,
        )
        os.close(slave)
        self.captured: list[bytes] = []

    def drain(self, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.captured.append(chunk)

    def raw(self) -> bytes:
        return b"".join(self.captured)

    def text(self) -> str:
        """What was painted, not how: escapes stripped, rows joined.

        RichLog wraps at the widget width, and a long name can land across the
        break ("commodore-" on one row, "64" on the next, with a cursor move in
        between). Searching ``raw()`` for such a name fails even though it is on
        screen, so text checks go through here.
        """
        plain = ANSI.sub(b"", self.raw()).decode("utf-8", "replace")
        return plain.replace("\r", "").replace("\n", "")

    def mark(self) -> int:
        return len(self.raw())

    def wait_for(self, needle: bytes, seconds: float = 40.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.drain(0.5)
            if needle in self.raw():
                return True
        return False

    def wait_for_text(self, needle: str, seconds: float = 40.0) -> bool:
        """Like :meth:`wait_for`, but for text rather than bytes.

        Textual repaints a line cell by cell once the screen has content to
        diff against, so a literal string can be on screen without ever
        appearing contiguously in ``raw()``. Text assertions use this.
        """
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.drain(0.5)
            if needle in self.text():
                return True
        return False

    def send(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8") + b"\r")
        self.drain(1.0)

    def close(self) -> int:
        os.write(self.master, b"/exit\r")
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.drain(0.5)
        os.close(self.master)
        return self.proc.returncode


# -- session one: the commands, and what they paint --------------------------
session = Session()
check("the chat booted", session.wait_for(b"K0B0L", 60))
check("the input prompt drew", session.wait_for("chat…".encode("utf-8"), 30))

session.send("/theme")
check("/theme with no argument lists them", session.wait_for_text("themes: textual-dark", 20))
# The list wraps in a 100-column pty and a name can break across the fold, so
# read the painted text (escapes stripped, rows joined) rather than the bytes.
for name in ("hotdog-3x", "beos", "commodore-64", "edit-com", "amber", "matrix"):
    check(f"/theme lists {name}", name in session.text())
check("/theme names the current one", session.wait_for_text("current: textual-dark", 20))

before = session.mark()
session.send("/theme matrix")
check("/theme matrix acknowledges", session.wait_for_text("theme: matrix", 20))
# A repaint after the switch, so the new colours are in the stream.
session.drain(2.0)
check("the matrix theme actually painted",
      MATRIX_FG.encode() in session.raw()[before:], session.raw()[-200:])
check("no matrix green before the switch",
      MATRIX_FG.encode() not in session.raw()[:before])

session.send("/theme commodore")
check("a prefix selects the theme", session.wait_for_text("theme: commodore-64", 40))
session.send("/theme AMBER")
check("case is ignored", session.wait_for_text("theme: amber", 20))
session.send("/theme textual-")
check("an ambiguous prefix is refused",
      session.wait_for_text("ambiguous", 20) and session.wait_for_text("textual-", 20))
session.send("/theme nope")
check("an unknown theme is refused",
      session.wait_for_text("unknown", 20) and session.wait_for_text("nope", 20))

session.send("/theme hotdog")
check("hotdog-3x is selectable", session.wait_for_text("theme: hotdog-3x", 20))
session.drain(2.0)
check("the hotdog theme painted its window colour",
      HOTDOG_BG.encode() in session.raw(), session.raw()[-200:])

rc = session.close()
raw = session.raw().decode("utf-8", "replace")
plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
check("session one exited cleanly", rc == 0, f"rc={rc}")
check("no traceback", "Traceback" not in plain, plain[-600:])

# -- the choice is remembered ------------------------------------------------
prefs = HOME / ".k0b0l-chat-ui.json"
try:
    saved = json.loads(prefs.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    saved = {"error": str(exc)}
check("the theme was saved", saved.get("theme") == "hotdog-3x", repr(saved))
check("the history file went to the redirected HOME",
      (HOME / ".k0b0l-chat-history").exists())

# -- session two: a restart comes back in the saved theme --------------------
second = Session()
check("the second chat booted", second.wait_for(b"K0B0L", 60))
second.drain(3.0)
check("the saved theme is applied at startup",
      HOTDOG_BG.encode() in second.raw(), second.raw()[-200:])
rc2 = second.close()
check("session two exited cleanly", rc2 == 0, f"rc={rc2}")

(SCRATCH / "themepty_capture.txt").write_text(
    re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", second.raw().decode("utf-8", "replace"))
)
print(f"{len(fails)} failure(s)" if fails else "ALL THEME PTY CHECKS PASS")
sys.exit(1 if fails else 0)
