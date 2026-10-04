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

print(f"{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
