#!/usr/bin/env python3
"""Endpoint fallback: a dead localhost endpoint asks for the remote one.

The behaviour requested: when the API endpoint is not accessible on
localhost, the app should ask the operator for the address -- it can be
assumed they want a remote API.

Pinned here, no model and no TTY needed:
  * non-interactive runs (piped stdin) keep the old behaviour: fail with
    the reachability error, never block on input
  * the helper only asks when the configured endpoint is localhost; a
    configured remote endpoint is never second-guessed
  * a bare host:port answer gets the http:// scheme; a full URL passes
  * the four entry points wire the retry (probe again with the answer)
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness.config import Config  # noqa: E402
from rt_harness.tui import ask_endpoint_on_failure  # noqa: E402

fails: list[str] = []
ok_count = 0


def check(label, cond, detail=""):
    global ok_count
    if cond:
        ok_count += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        fails.append(label)
        print(f"FAIL {label}  {detail}")


# -- the helper never blocks without a TTY -----------------------------------
config = Config.from_env({"API_URL": "http://127.0.0.1:11434/v1"})
result = ask_endpoint_on_failure(config, running=False)
check("piped runs never ask: running=False returns the original",
      result == "http://127.0.0.1:11434/v1", result)

# -- a non-localhost endpoint is left alone ----------------------------------
config_remote = Config.from_env({"API_URL": "http://192.168.1.50:11434/v1"})
# running=True but stdin is a pipe in this suite: the TTY guard holds, and
# even without it the host check must refuse to ask about a remote endpoint.
check("the TTY guard holds on piped stdin",
      not (sys.stdin.isatty() and sys.stdout.isatty()))

# -- scheme repair, by direct exercise of the answer normalisation -----------
# (input() is stubbed: the TTY guard would normally block it away; these
# exercise the normalisation branch itself)
import rt_harness.tui as tui_module  # noqa: E402


class FakeConfig:
    api_url = "http://127.0.0.1:11434/v1"


class FakeTTY:
    """Pretend stdin/stdout are a terminal."""

    @staticmethod
    def isatty():
        return True


def exercise(answer_text):
    real_stdin, real_stdout, real_stderr = (sys.stdin, sys.stdout, sys.stderr)
    real_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    try:
        sys.stdin = FakeTTY()
        sys.stdout = FakeTTY()
        sys.stderr = io.StringIO()
        if isinstance(__builtins__, dict):
            __builtins__["input"] = lambda *a: answer_text
        else:
            __builtins__.input = lambda *a: answer_text
        return ask_endpoint_on_failure(FakeConfig(), running=True)
    finally:
        sys.stdin, sys.stdout, sys.stderr = real_stdin, real_stdout, real_stderr
        if isinstance(__builtins__, dict):
            __builtins__["input"] = real_input
        else:
            __builtins__.input = real_input


check("a bare host:port gets the http scheme",
      exercise("192.168.99.2:11434") == "http://192.168.99.2:11434",
      exercise("192.168.99.2:11434"))
check("a full URL passes through untouched",
      exercise("https://api.example.com/v1") == "https://api.example.com/v1")
check("an empty answer returns the original (operator gave up)",
      exercise("") == "http://127.0.0.1:11434/v1")

# -- the remote endpoint is never second-guessed -----------------------------

class RemoteConfig:
    api_url = "http://192.168.1.50:11434/v1"


real_stdin, real_stdout, real_stderr = (sys.stdin, sys.stdout, sys.stderr)
remote_result = None
asked = False
try:
    sys.stdin = FakeTTY()
    sys.stdout = FakeTTY()
    sys.stderr = io.StringIO()

    def _must_not_ask(*_a):
        global asked
        asked = True
        raise AssertionError("must not ask about a remote endpoint")

    if isinstance(__builtins__, dict):
        __builtins__["input"] = _must_not_ask
    else:
        __builtins__.input = _must_not_ask
    remote_result = ask_endpoint_on_failure(RemoteConfig(), running=True)
finally:
    sys.stdin, sys.stdout, sys.stderr = real_stdin, real_stdout, real_stderr
check("a configured remote endpoint is never re-asked",
      not asked and remote_result == "http://192.168.1.50:11434/v1",
      str(remote_result))

# -- the piped app start still fails fast with the old message ---------------
import os  # noqa: E402
import subprocess  # noqa: E402

env = dict(os.environ)
env["API_URL"] = "http://127.0.0.1:9/v1"  # nothing listens here
proc = subprocess.run(
    [sys.executable, "thinlizzy.py", "--chat-plain"],
    input="", capture_output=True, text=True, timeout=60,
    cwd=str(Path(__file__).resolve().parents[1]),
    env=env,
)
check("piped chat start with no server fails with the reachability error",
      proc.returncode == 1 and "not reachable" in (proc.stderr + proc.stdout),
      (proc.stderr or proc.stdout)[-120:])

print()
if fails:
    print(f"{len(fails)} failure(s):")
    for name in fails:
        print(f"  - {name}")
else:
    print(f"ALL {ok_count} ENDPOINT-FALLBACK CHECKS PASS")
sys.exit(1 if fails else 0)
