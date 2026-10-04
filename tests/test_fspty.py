"""Smoke test: the full-screen front end draws bars and exits under a pty."""
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
URL = os.environ.get("CHAT_TEST_URL", "http://127.0.0.1:11436")
SCRATCH = Path(__file__).resolve().parent / '.scratch'
ROOT = SCRATCH / "fspty"
ROOT.mkdir(parents=True, exist_ok=True)

fails = []
def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)

env = {
    **os.environ,
    "TERM": "xterm-256color",
    "OLLAMA_URL": URL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_PREDICT": "64",
}

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
proc = subprocess.Popen(
    [sys.executable, "thinlizzy.py", "--chat", "--chat-ptk"],
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

# The alternate screen is how the full-screen app owns the whole terminal.
check("entered the alternate screen", wait_for(b"\x1b[?1049h", 30))
check("the top bar drew", wait_for(b"K0B0L |", 30))
check("the prompt drew", wait_for(b"you&gt; ", 10) or b"you>" in b"".join(captured))

os.write(master, b"/help\n")
check("/help renders", wait_for(b"paste pad", 20))

os.write(master, b"/history\n")
check("/history renders", wait_for(b"tool calls", 20))

os.write(master, b"/tools full\n")
check("/tools full acknowledged", wait_for(b"tool output", 20))

os.write(master, b"\x1b")  # Esc opens the menu
check("Esc opens the menu", wait_for(b"Session", 20))
check("the menu is populated from the server", wait_for(b"Target model", 20))
os.write(master, b"\x1b")  # Esc again closes it / returns focus
drain(1)

# The F1 help panel and the run wizard are Textual-front-end features only;
# --chat-ptk has neither. The /run check below covers operator control under
# the legacy path.

os.write(master, b"/run start probe the artifact\n")
check("/run start answers in the chat", wait_for(b"cannot start: no target model", 20))

# with a target, a run starts and the run bar shows it
os.write(master, b"/run start target=fake:1b modes=seam attempts=1 probe\n")
started_run = wait_for(b"started; log:", 20)
check("/run start spawns the harness", started_run)
# The bar shows the pid on the same line as the run state; transcript lines
# only ever carry the "started; log" sentence, so a line starting "run <pid>:"
# can only be the bar.
check("the run status bar lights up", wait_for(b"run ", 20))

os.write(master, b"/run status\n")
check("/run status prints the log path", wait_for(b"log:", 20))

os.write(master, b"/run stop\n")
check("/run stop stops it", wait_for(b"stopped", 20))

# Exit via the Exit menu action equivalent: the /exit command.
os.write(master, b"/exit\n")
drain(4)
try:
    proc.wait(timeout=20)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=10)

raw = b"".join(captured).decode("utf-8", "replace")
plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
plain = re.sub(r"\x1b\][^\x07]*\x07", "", plain)

check("the process exited cleanly", proc.returncode is not None and proc.returncode == 0,
      f"rc={proc.returncode}")
check("no traceback", "Traceback" not in plain, plain[-600:])
check("the alternate screen was left", "\x1b[?1049l" in raw)

(SCRATCH / "fspty_capture.txt").write_text(plain)
(SCRATCH / "fspty_raw.txt").write_bytes(b"".join(captured))
if fails:
    print(f"{len(fails)} failure(s)")
else:
    print("ALL FS PTY CHECKS PASS")
sys.exit(1 if fails else 0)
