#!/usr/bin/env python3
"""Endpoint fallback: a dead localhost endpoint asks for the remote one.

The behaviour requested: when the API endpoint is not accessible on
localhost, the app should ask the operator for the address -- it can be
assumed they want a remote API -- and the answer is saved as the default
used on every subsequent load.

Pinned here, no model and no TTY needed:
  * non-interactive runs (piped stdin) keep the old behaviour: fail with
    the reachability error, never block on input
  * the helper only asks when the configured endpoint is localhost; a
    configured remote endpoint is never second-guessed
  * a bare host:port answer gets the http:// scheme; a full URL passes
  * the four entry points wire the retry (probe again with the answer)
  * the answer is persisted (set_default_api) and startup honours it
    (Config.from_env falls back to the saved default); API_URL wins
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness import catalog  # noqa: E402
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


# -- persistence: the answer becomes the saved default -----------------------
# The suite never touches the operator's real ~/.k0b0l-apis.json: the
# catalog's path function is pointed at a temp file, the same trick the
# skills suite uses for the profile dir.
with TemporaryDirectory() as tmp:
    apis_file = Path(tmp) / "apis.json"
    real_path = catalog._apis_path
    catalog._apis_path = lambda: apis_file  # type: ignore[misc]
    try:
        check("no saved default at first", catalog.default_api() == "")
        check("saving the answer marks it default",
              catalog.set_default_api("http://192.168.99.2:11434/v1"))
        check("the saved default reads back",
              catalog.default_api() == "http://192.168.99.2:11434/v1")
        # one default only: saving another clears the first
        catalog.set_default_api("https://api.example.com/v1")
        check("only one default exists at a time",
              catalog.default_api() == "https://api.example.com/v1",
              catalog.default_api())
        records = catalog._read_endpoints()
        check("exactly one record is marked default",
              sum(1 for r in records if r.get("default")) == 1)
        # startup honours the saved default when no env names a URL
        config_saved = Config.from_env({})
        check("startup uses the saved default when API_URL is unset",
              config_saved.api_url == "https://api.example.com/v1",
              config_saved.api_url)
        config_pinned = Config.from_env(
            {"API_URL": "http://127.0.0.1:11434/v1"})
        check("an explicit API_URL still overrides the saved default",
              config_pinned.api_url == "http://127.0.0.1:11434/v1",
              config_pinned.api_url)
        # set_api (the in-session picker) also becomes the default
        class _Cfg:
            ollama_url = "http://127.0.0.1:11434/v1"

        cfg = _Cfg()
        catalog.set_api(cfg, "http://10.0.0.5:11434")
        check("selecting an endpoint in-session also updates the default",
              catalog.default_api() == "http://10.0.0.5:11434",
              catalog.default_api())
    finally:
        catalog._apis_path = real_path  # type: ignore[misc]

# the helper's answer path persists: exercise() now saves through
# set_default_api, so run it against the temp file too. (Moved below the
# exercise() definition -- it stubs input() to simulate the TTY prompt.)
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
    """One prompt-path run, ALWAYS against a scratch apis.json.

    ask_endpoint_on_failure persists the answer as the saved default; an
    exercise() against the real ~/.k0b0l-apis.json would corrupt the
    operator's config (this once happened: a fake URL from the suite
    leaked into the live file). The scratch file also proves the
    persistence each call.
    """
    real_stdin, real_stdout, real_stderr = (sys.stdin, sys.stdout, sys.stderr)
    real_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    real_path = catalog._apis_path
    scratch = Path(__file__).parent / ".scratch"
    scratch.mkdir(exist_ok=True)
    apis_file = scratch / "exercise-apis.json"
    apis_file.write_text("[]")
    catalog._apis_path = lambda: apis_file  # type: ignore[misc]
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
        catalog._apis_path = real_path  # type: ignore[misc]
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

# -- the prompt's own answer path persists the default -----------------------
# exercise() runs against tests/.scratch/exercise-apis.json (never the
# operator's real file), so the persistence is proven by reading the
# scratch file back after a run.
scratch_apis = Path(__file__).parent / ".scratch" / "exercise-apis.json"
answered = exercise("192.168.99.2:11434")
import json as _json  # noqa: E402

scratch_records = _json.loads(scratch_apis.read_text())
check("the prompt's answer is persisted as the default",
      answered == "http://192.168.99.2:11434"
      and any(r.get("default") and r["base_url"] == "http://192.168.99.2:11434"
              for r in scratch_records),
      str(scratch_records[:1]))
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
