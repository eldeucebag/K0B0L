"""Smoke test: the Textual chat front end boots, accepts commands, exits clean."""
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
ROOT = SCRATCH / "textualpty"
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

def wait_for_new(needle, seconds=60.0):
    """Like ``wait_for``, but only over what arrives after this call.

    The capture is cumulative: a second start produces the same "started; log:"
    line the first one did, so a needle found anywhere in the capture would
    pass on the previous command's output. Commands that repeat a message use
    this instead.
    """
    offset = len(b"".join(captured))
    deadline = time.time() + seconds
    while time.time() < deadline:
        drain(0.5)
        if needle in b"".join(captured)[offset:]:
            return True
    return False

# A needle that spans two style runs never appears contiguously in the raw
# capture: the reply "run 1093114 started" paints the pid on its own, so the
# bytes read "run \x1b[38;5;223m1093114\x1b[0m\x1b[38;5;214m started; log: ".
# Strip the escapes before searching for anything but a literal control
# sequence.
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][B0]|\x1b[=>]")

def plain(data):
    return ANSI.sub(b"", data)

def wait_for_plain(needle, seconds=20.0):
    """``wait_for`` over the capture with the escape sequences stripped."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        drain(0.5)
        if needle in plain(b"".join(captured)):
            return True
    return False

def mark():
    """Where the capture ends now -- take it *before* writing the command."""
    return len(b"".join(captured))

def wait_for_any(patterns, offset, seconds=25.0):
    """Wait for any of several patterns (bytes regexes) after ``offset``.

    The mark is passed in rather than taken here because a chained pair of
    alt-waits would take a fresh mark each: a reply that arrived during the
    first wait is not "new" to the second, so both would report False and a
    perfectly good answer would read as a failure.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        drain(0.5)
        new = plain(b"".join(captured)[offset:])
        if any(re.search(pattern, new) for pattern in patterns):
            return True
    return False

# Textual takes the alternate screen.
check("entered the alternate screen", wait_for(b"\x1b[?1049h", 30))
check("the top bar drew", wait_for(b"K0B0L", 30))
check("the input prompt drew", wait_for("chat…".encode("utf-8"), 30))

os.write(master, b"/help\r")
check("/help renders", wait_for(b"/run start", 20))

os.write(master, b"/history\r")
check("/history renders", wait_for(b"tool calls", 20))

os.write(master, b"/tools full\r")
check("/tools full acknowledged", wait_for(b"tool output", 20))

# A target model has a default now (config.DEFAULT_TARGET_MODEL), so a bare
# /run start is a real run against the GPU. Ask for the fake target explicitly.
mark_bar = mark()
os.write(master, b"/run start target=fake:1b probe the artifact\r")
check("/run start answers in the chat", wait_for_new(b"started; log:", 20))

# The run bar is a full-width row, so its text is padded out to the terminal
# width -- a padded row is what tells the bar apart from the chat's own reply,
# which is not padded. A fake target on an endpoint without that model exits
# inside a second, so the bar usually paints its outcome instead of ever having
# painted the live form ("run <pid>: attempt 0/7 ...").
match = re.search(rb"run (\d+) started", plain(b"".join(captured)))
check("the reply names the run it started", match is not None)
bar_patterns = [rb"exited rc=-?\d+ {20,}", rb"run \d+: "]
check("the run bar shows that run", wait_for_any(bar_patterns, mark_bar, 25))

os.write(master, b"/run status\r")
check("/run status prints the log path", wait_for(b"log:", 20))

# Whether the child is still alive by now depends on the model server: a fake
# target on an endpoint without that model exits within seconds, so "stopped"
# and "no run is active" are both correct answers to a stop command. What this
# test is for is that the command reaches the controller and the reply lands on
# screen; the liveness contract (one run at a time, stop really terminates it)
# is asserted in tests/test_runctl.py, where a script launcher makes it certain.
mark_stop = mark()
os.write(master, b"/run stop\r")
check("/run stop answers the chat",
      wait_for_any([rb"stopped", rb"no run is active"], mark_stop))

os.write(master, b"\x1b")  # Esc opens the menu bar (dropdown, not the palette)
check("Esc opens the menu bar", wait_for(b"Chat model", 20))
os.write(master, b"\x1b[C")                       # top-level Right: Session → Run's
drain(1)                                          # let the panel repaint
check("Right switches menus", wait_for(b"Start run (wizard)", 20))
os.write(master, b"\r")                           # Enter: open the wizard
drain(1)          # let the wizard finish mounting: F1 sent in the same frame
                  # the modal is being pushed gets handled before the modal is
                  # the active screen, and help then surfaces much later

os.write(master, b"\x1bOP")  # F1
check("F1 opens help", wait_for(b"keys", 20))
os.write(master, b"\x1b")
check("Esc closes help", wait_for("chat…".encode("utf-8"), 20))
check("the wizard shows the objective hint", wait_for(b"objective", 20))
os.write(master, b"probe the artifact\r")          # step 1
os.write(master, b"fake:1b\r")                     # target: the fake one, not the default
os.write(master, b"seam\r")                        # modes
os.write(master, b"\r")                            # family
os.write(master, b"1\r")                           # attempts
check("the wizard submits", wait_for_new(b"started; log:", 20))
mark_wiz = mark()
os.write(master, b"/run stop\r")                   # leave no run behind
check("the wizard's run is stopped",
      wait_for_any([rb"stopped", rb"no run is active"], mark_wiz))

drain(2)
# Esc anything still open (menu or a stray modal), then quit. Ctrl-Q is the
# App's priority binding: it quits whatever has focus, whereas `/exit` typed
# while the menu bar holds focus goes to the menu and the app never leaves.
os.write(master, b"\x1b")
drain(1)
os.write(master, b"\x11")
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    os.write(master, b"/exit\r")
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

# The app writes its teardown (leave the alternate screen, show the cursor) on
# the way out, and those bytes land in the pty buffer while we are waiting for
# the exit. Read them before joining the capture, or the teardown looks like it
# never happened.
drain(3)

raw = b"".join(captured).decode("utf-8", "replace")
plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
plain = re.sub(r"\x1b\][^\x07]*\x07", "", plain)

check("the process exited cleanly", proc.returncode is not None and proc.returncode == 0,
      f"rc={proc.returncode}")
check("no traceback", "Traceback" not in plain, plain[-600:])
check("the alternate screen was left", "\x1b[?1049l" in raw)

(SCRATCH / "textualpty_capture.txt").write_text(plain)
(SCRATCH / "textualpty_raw.txt").write_bytes(b"".join(captured))
if fails:
    print(f"{len(fails)} failure(s)")
else:
    print("ALL TEXTUAL PTY CHECKS PASS")
sys.exit(1 if fails else 0)
