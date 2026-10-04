"""The CLI's cycle path: it has to get as far as building a client.

It once did not. A local ``from .client import ... make_client`` inside the
``--list-models`` branch made that name local to ``main`` for its whole body,
so every invocation that skipped the branch -- ``--cycle``, which is what every
spawned run is -- died on UnboundLocalError before it touched the network. The
run controller then saw a process that had already exited, so ``/run status``
was the last thing that worked and ``/run stop`` had nothing to stop.

A closed port reaches the same line a real run does and fails for the honest
reason: the server is not reachable. None of this needs a model.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness import cli  # noqa: E402

OK = 0
FAIL = 0


def check(label, ok, detail=""):
    global OK, FAIL
    if ok:
        OK += 1
        print("ok  ", label)
    else:
        FAIL += 1
        print("FAIL", label, detail)


#: The discard port: nothing is listening, so the client fails to connect.
CLOSED = "http://127.0.0.1:9/v1"


def run(argv, **overrides):
    """``cli.main`` with a controlled environment, its output captured.

    A crash is reported as a failed call, not an unhandled traceback: the bug
    this file exists for *was* a crash, and a checker that dies on it says
    nothing about the other checks.
    """
    env = {
        "API_URL": CLOSED,
        "TARGET_MODEL": "a-target",
        "SKIP_PULL": "1",
        **overrides,
    }
    saved = dict(os.environ)
    os.environ.update({key: value for key, value in env.items() if value is not None})
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
    except Exception:
        import traceback

        return -1, out.getvalue(), err.getvalue() + traceback.format_exc()
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return rc, out.getvalue(), err.getvalue()


# The bug: this call raised UnboundLocalError instead of reaching the network.
rc, _out, err = run(["--cycle", "--objective", "probe the artifact"])
check("--cycle reaches the client and reports the dead server", rc == 1, f"rc={rc}")
check("...with the reachability message", "not reachable" in err, err[-400:])
check("...and no traceback", "Traceback" not in err, err[-400:])

# The branch that carried the local import must still work.
rc, out, err = run(["--list-models"])
check("--list-models reports the dead server too", rc == 1, f"rc={rc}")
check("...without a traceback", "Traceback" not in err, err[-400:])

# With no target model it still has to fail cleanly. The target guard itself is
# downstream of the server probe, so it cannot be observed from here -- that
# contract lives in tests/test_runctl.py, which drives the controller directly.
rc, _out, err = run(["--cycle", "--objective", "probe"], TARGET_MODEL="")
check("a cycle with no target model still fails cleanly", rc == 1, f"rc={rc}")
check("...without a traceback", "Traceback" not in err, err[-400:])


# -- chat is the default -----------------------------------------------------
#
# A bare invocation used to read objectives from stdin; it now opens the chat,
# because that is the primary use of the harness. On a dead server the two
# paths fail with different words, and that is the whole observable
# difference from here: the chat probe says "<backend> is not reachable"
# before any banner, the loop says "Server is not reachable" (and never a
# banner). The target check passes on the loop path because a default target
# exists, so the probe is what the loop reaches.

CHAT_MARK = "llama.cpp is not reachable"
LOOP_MARK = "Server is not reachable"


def run_bare(argv, **overrides):
    """A bare run with the live environment, output captured.

    This suite's own stdin is not a TTY, so a bare call takes the plain front
    end: the probe runs before anything is printed, and which probe ran is
    the difference between "opens the chat" and "runs the loop".
    """
    saved = dict(os.environ)
    os.environ.update({"API_URL": CLOSED, "SKIP_PULL": "1", **overrides})
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return rc, out.getvalue(), err.getvalue()


rc, out, err = run_bare([])
check("a bare invocation now opens the chat", CHAT_MARK in err, err[-200:])
check("...and its server probe still fails honestly", rc == 1, f"rc={rc}")
check("...without a traceback", "Traceback" not in err, err[-300:])

rc, out, err = run_bare([], K0B0L_NO_CHAT="1")
check("K0B0L_NO_CHAT=1 restores the loop", LOOP_MARK in err, err[-300:])
check("...and does not open the chat", CHAT_MARK not in err, err[-200:])

rc, out, err = run_bare(["--no-chat"])
check("--no-chat also restores the loop", LOOP_MARK in err, err[-300:])
check("...and does not open the chat", CHAT_MARK not in err, err[-200:])

rc, out, err = run_bare(["--cycle", "--objective", "probe"])
check("--cycle still wins over the chat default",
      LOOP_MARK in err and CHAT_MARK not in err, (out + err)[-200:])

rc, out, err = run_bare(["--chat"])
check("--chat is still accepted and opens the chat", CHAT_MARK in err,
      err[-200:])

print(f"{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
