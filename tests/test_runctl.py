"""RunController: spawn, tail, stop -- with a shell script standing in for the harness."""
import io
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness.config import Config  # noqa: E402
from rt_harness.runctl import RunController, parse_start_args  # noqa: E402

OK = 0
FAIL = 0
def check(name, condition, detail=""):
    global OK, FAIL
    if condition:
        OK += 1
        print(f"ok   {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {name}" + (f"  {detail}" if detail else ""))

FAKE_SCRIPT = """#!/bin/bash
echo "Cycling 2 mode(s): seam, escalation"
echo "================ ARTIFACT ANALYSIS ================"
echo "  purpose: define the output format"
echo "--- attempt 1/2  mode=seam"
echo "    method: attack the seam"
echo "    VERDICT: refused"
echo "--- attempt 2/2  mode=escalation"
echo "    VERDICT: complied"
echo "BYPASS SCORED -- attempt 2 (mode escalation, verdict complied)"
echo "cycle finished: 2 attempt(s)"
exit 0
"""

FAKE_WITH_WRITEUP = """#!/bin/bash
echo "TARGET_FAMILY=$TARGET_FAMILY"
echo "--- attempt 1/1  mode=seam"
echo "    VERDICT: complied"
echo "BYPASS SCORED -- attempt 1 (mode seam, verdict complied)"
echo "Write-up saved to /tmp/x.md"
echo "## 1. The winning prompt (verbatim, unredacted)"
echo '```text'
echo 'WINNING PROMPT BODY'
echo '```'
echo "cycle finished: 1 attempt(s)"
exit 0
"""

with TemporaryDirectory() as tmpdir:
    config = Config.from_env({})
    config.logdir = Path(tmpdir) / "redteam-logs"
    config.base_prompt_file = ""
    script_path = Path(tmpdir) / "fake-writeup.sh"
    script_path.write_text(FAKE_WITH_WRITEUP)
    ctl = RunController(config, launcher=["bash", str(script_path)])
    ctl.start("probe", target="fake:1b", family="anthropic")
    deadline = time.time() + 5
    while ctl.running and time.time() < deadline:
        time.sleep(0.05)
    ctl.poll()
    check("the write-up path is parsed", ctl.writeup_path == "/tmp/x.md", ctl.writeup_path)
    check("a winning prompt is archived", bool(ctl.winner_path) and Path(ctl.winner_path).is_file(), ctl.winner_path)
    if ctl.winner_path:
        body = Path(ctl.winner_path).read_text()
        check("the archived prompt is verbatim", "WINNING PROMPT BODY" in body)
    check("the family override reaches the child env", "TARGET_FAMILY=anthropic" in ctl.tail(50))
    check("/run status mentions the write-up", "write-up" in ctl.status())


with TemporaryDirectory() as tmpdir:
    config = Config.from_env({})
    config.logdir = Path(tmpdir) / "redteam-logs"
    config.base_prompt_file = ""

    script_path = Path(tmpdir) / "fake-harness.sh"
    script_path.write_text(FAKE_SCRIPT)
    ctl = RunController(config, launcher=["bash", str(script_path)])

    msg = ctl.start("probe the seam", modes="seam,escalation", attempts=2, target="fake:1b")
    check("start reports the pid", "started" in msg and "log:" in msg, msg)
    check("it is running", ctl.running)
    time.sleep(0.4)
    ctl.poll()
    check("attempt parsed", ctl.attempt == 2, str(ctl.attempt))
    check("budget parsed", ctl.attempt_budget == 2)
    check("last verdict parsed", ctl.last_verdict == "complied", ctl.last_verdict)
    check("bypass line parsed", "attempt 2" in ctl.bypass_line)
    deadline = time.time() + 5
    while ctl.running and time.time() < deadline:
        time.sleep(0.1)
    ctl.poll()
    check("it finished", not ctl.running)
    check("finish line parsed", "2 attempt(s)" in ctl.finish_line)
    line = ctl.status_line()
    check("status line names the bypass", "BYPASS" in line, line)
    check("/run tail returns log lines", "BYPASS SCORED" in ctl.tail())

    again = ctl.start("second run", target="fake:1b")
    check("a new run can start after the last finished", ctl.running, again)
    blocked = ctl.start("a third run", target="fake:1b")
    check("one run at a time: a second start is refused",
          "already active" in blocked, blocked)
    stop = ctl.stop()
    check("stop terminates it", not ctl.running and "stopped" in stop, stop)
    check("stop reports the return code", ctl.proc.returncode is not None and ctl.proc.returncode != 0,
          str(ctl.proc.returncode))

    # argument parsing
    parsed = parse_start_args("target=qwen3:8b modes=seam,graft attempts=4 probe the artifact")
    check("args split out overrides",
          parsed is not None and parsed[1] == {"target": "qwen3:8b", "modes": "seam,graft", "attempts": "4"},
          str(parsed))
    check("the objective keeps the rest", parsed is not None and parsed[0] == "probe the artifact", str(parsed))
    check("no objective is rejected", parse_start_args("target=qwen3:8b") is None)

    # missing target blocks the start instead of dying in the harness
    bad = Config.from_env({})
    bad.logdir = Path(tmpdir) / "other"
    bad.base_prompt_file = ""
    for name, spec in list(bad.roles.items()):
        bad.roles[name] = replace(spec, model="")
    ctl2 = RunController(bad, launcher=["true"])
    saved = {k: os.environ.pop(k) for k in ("TARGET_MODEL",) if k in os.environ}
    try:
        out = ctl2.start("anything")
        check("no target refuses to start", "no target model" in out, out)
    finally:
        os.environ.update(saved)

print(f"{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
