"""Drive the rich front end under a real pty, the way a terminal would."""
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
MODEL = os.environ.get("CHAT_TEST_MODEL", "hf.co/mradermacher/Gemma-4-E4B-Abliterated-Uncensored-i1-GGUF:i1-Q4_K_M")
# Scratch space lives beside these tests, never in /tmp, so a
# checkout is self-contained. Override the server or model with
# CHAT_TEST_URL / CHAT_TEST_MODEL.
SCRATCH = Path(__file__).resolve().parent / '.scratch'
ROOT = SCRATCH / "pty"

ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "notes.md").write_text("# Notes\n\nThe widget count is 41.\n")

fails: list[str] = []
def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)

env = {
    **os.environ,
    "TERM": "xterm-256color",
    "OLLAMA_URL": URL,
    "CHAT_MODEL": MODEL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_CTX": "8192",
    "CHAT_NUM_PREDICT": "300",
    "CHAT_TOOL_OUTPUT": "summary",
}
env.pop("LC_ALL", None)

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
proc = subprocess.Popen(
    [sys.executable, "thinlizzy.py", "--chat"],
    cwd=REPO, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
    start_new_session=True,
)
os.close(slave)

captured: list[bytes] = []
def drain(seconds: float) -> None:
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

def wait_for(needle: bytes, seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        drain(0.5)
        if needle in b"".join(captured):
            return True
    return False

started = wait_for(b"K0B0L chat", 60)
check("the rich front end starts and prints its banner", started)

os.write(master, b"/help\n")
check("/help renders under a pty", wait_for(b"/tools", 20))

os.write(master, b"How many widgets does notes.md report? One short sentence.\n")
answered = wait_for(b"41", 240)
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

check("the model answered the question under a pty", answered, plain[-500:])
check("a tool call was rendered in the live region", "read_file" in plain, plain[-600:])
check("a tool panel used box drawing", any(ch in plain for ch in "╭╰│╮╯"), plain[-300:])
check("no traceback in the terminal stream", "Traceback" not in plain, plain[-800:])
check("the process exited", proc.returncode is not None and proc.returncode == 0,
      f"rc={proc.returncode}")
check("the prompt was rendered", "you>" in plain)

print()
print(f"captured {len(raw)} bytes / {len(plain)} visible chars")
(SCRATCH / "pty_chat_capture.txt").write_text(plain)
(SCRATCH / "pty_chat_raw.txt").write_bytes(b"".join(captured))
# The live region must repaint in place. If it appended instead, a real
# terminal would fill with duplicated frames; the erase is a cursor-up escape.
up = len(re.findall(r"\x1b\[\d*A", raw))
check("the live region repaints in place", up > 0, f"{up} cursor-up sequences")
check("the screen is not left in an alternate buffer", "\x1b[?1049h" not in raw)
print(f"capture written to {SCRATCH / 'pty_chat_capture.txt'}")
if fails:
    print(f"{len(fails)} failure(s):")
    for f in fails:
        print(f"  - {f}")
else:
    print("ALL PTY CHECKS PASS")
sys.exit(1 if fails else 0)
