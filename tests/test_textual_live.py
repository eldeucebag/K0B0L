"""Smoke: a real model turn inside the Textual chat UI (pty)."""
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
SCRATCH = Path(__file__).resolve().parent / '.scratch'
ROOT = SCRATCH / "textuallive"
ROOT.mkdir(parents=True, exist_ok=True)
(ROOT / "fact.md").write_text("The codeword is PINEAPPLE.\n")

env = {
    **os.environ,
    "TERM": "xterm-256color",
    "OLLAMA_URL": URL,
    "CHAT_MODEL": MODEL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_CTX": "4096",
    "CHAT_NUM_PREDICT": "200",
}

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
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

fails = []
def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)

check("the textual UI starts", wait_for("chat…".encode("utf-8"), 60))
os.write(master, b"Read fact.md and reply with just the codeword.\r")
answered = wait_for(b"PINEAPPLE", 300)
os.write(master, b"/exit\r")
drain(4)
try:
    proc.wait(timeout=25)
except subprocess.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=10)

raw = b"".join(captured).decode("utf-8", "replace")
plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
plain = re.sub(r"\x1b\][^\x07]*\x07", "", plain)

check("the model answered through the UI", answered, plain[-400:])
check("the turn landed in the transcript", "you> Read fact.md" in plain)
check("a tool line rendered", "read_file" in plain, plain[-600:])
check("no traceback", "Traceback" not in plain, plain[-600:])
check("clean exit", proc.returncode == 0, f"rc={proc.returncode}")
check("the alternate screen was left", "\x1b[?1049l" in raw)

(SCRATCH / "textuallive_capture.txt").write_text(plain)
print(f"{len(fails)} failure(s)" if fails else "ALL TEXTUAL LIVE CHECKS PASS")
sys.exit(1 if fails else 0)
