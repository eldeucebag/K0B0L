"""End to end: a fenced code block from the model reaches the screen coloured.

The unit tests prove which lexer a block settled on and which band colour the
theme defines; they cannot prove either one was painted, because they read the
transcript's own model of itself. This drives the real front end in a pty, asks
the model for one code block, and reads the escapes the terminal was handed.

Colours are asserted as literal truecolor escapes (COLORTERM=truecolor, so
textual writes them rather than the nearest 256-colour approximation), and they
are matrix's. The app loads its theme at boot from
``~/.k0b0l-chat-ui.json``, so the child gets a scratch HOME holding a matrix
preference -- reading the operator's real prefs made the run depend on whatever
theme they last left the app in, which is how a matrix assertion started failing
against their amber.
"""
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
#: The router's OpenAI-compatible base. The /v1 suffix is what makes the front
#: end pick the chat-completions client: without it the base is treated as
#: Ollama's and the boot probe asks for /api/version, which llama.cpp 404s.
URL = os.environ.get("CHAT_TEST_URL", "http://127.0.0.1:11434/v1")
SCRATCH = Path(__file__).resolve().parent / ".scratch"
ROOT = SCRATCH / "codepaint"
ROOT.mkdir(parents=True, exist_ok=True)

#: matrix: code-background, and the three greens its syntax style uses.
CODE_BAND = "#001A0A"
BAND_ESCAPE = "48;2;0;26;10"

fails = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)


# The app reads its saved theme out of HOME at boot, so hand the child a HOME of
# its own with the theme this test asserts on. Depending on the operator's real
# prefs file made the run depend on their last theme choice.
FAKE_HOME = ROOT / "home"
REAL_HOME = os.environ.get("HOME") or str(Path.home())
FAKE_HOME.mkdir(parents=True, exist_ok=True)
(FAKE_HOME / ".k0b0l-chat-ui.json").write_text(
    '{"theme": "matrix"}', encoding="utf-8"
)

env = {
    **os.environ,
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "API_URL": URL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_PREDICT": "256",
    "HOME": str(FAKE_HOME),
    # textual is a *user* install (~/.local/lib/python3.10/site-packages), so
    # moving HOME would hide it from the child unless the user base follows.
    "PYTHONUSERBASE": str(Path(REAL_HOME) / ".local"),
}

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
proc = subprocess.Popen(
    [sys.executable, "thinlizzy.py", "--chat"],
    cwd=REPO, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
    start_new_session=True,
)
os.close(slave)

captured = []


def drain(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            captured.append(chunk)


def wait_for(needle, seconds=60.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        drain(0.5)
        if needle in b"".join(captured):
            return True
    return False


check("entered the alternate screen", wait_for(b"\x1b[?1049h", 40))
check("the input prompt drew", wait_for("chat…".encode("utf-8"), 40))

# Ask for exactly one fence, so a reply that arrives without one is the model's
# doing and not ours.
os.write(
    master,
    "Reply with a single fenced code block and no other text. The block is "
    "python, and it defines add(a, b) returning a + b.\r".encode("utf-8"),
)

got_header = wait_for(b"click to fold", 300)
drain(5)

raw = b"".join(captured).decode("utf-8", "replace")
plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
plain = re.sub(r"\x1b\][^\x07]*\x07", "", plain)

check("the code block drew a header", got_header and "click to fold" in plain)
check("the header names the language", "python" in plain, plain[-500:])
check("the block body reached the screen", "def add" in plain, plain[-500:])

# Co-location, not mere presence: matrix paints its surface with the same hex
# as its code background, so an escape somewhere in the capture would prove
# nothing. "click to fold" is written by this block and nothing else, so a row
# carrying both that text and the band escape is the block's own band.
rows = raw.split("\n")
check(
    f"the block's own row was banded ({CODE_BAND})",
    any("click to fold" in row and BAND_ESCAPE in row for row in rows),
    "the fold header never appeared on a row painted with the code background "
    "-- the child's scratch HOME should have booted it in matrix, so check "
    "that ~/.k0b0l-chat-ui.json still resolves inside the scratch dir "
    "(tests/test_folds.py covers the band without a terminal)",
)
check(
    "the code body was drawn inside the band",
    any(("return" in row or "def " in row) and BAND_ESCAPE in row for row in rows),
    "the body rows never carried the band background",
)
check("no traceback", "Traceback" not in plain, plain[-800:])

os.write(master, b"\x1b")
drain(1)
os.write(master, b"\x11")
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=10)
drain(3)

raw = b"".join(captured).decode("utf-8", "replace")
check("the process exited cleanly", proc.returncode == 0, f"rc={proc.returncode}")

(SCRATCH / "codepaint_raw.txt").write_bytes(b"".join(captured))
if fails:
    print(f"{len(fails)} failure(s)")
else:
    print("ALL CODE PAINT CHECKS PASS")
sys.exit(1 if fails else 0)
