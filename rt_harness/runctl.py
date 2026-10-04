"""Spawning and watching harness runs from inside the chat.

The chat's model is deliberately tool-only (no shell), so run control lives on
the operator's side of the UI: ``/run start`` spawns ``thinlizzy.py --cycle``
as a subprocess, and the controller tails its log so the front end can show a
live attempt/verdict line without the model ever knowing a run exists.

One run at a time. A run is a process plus the log file it writes; every
``poll()`` re-reads the new tail, so the status line is cheap to call on every
UI refresh. Parsing keys off the cycle's own printed lines (``--- attempt``,
``VERDICT:``, ``BYPASS SCORED``, ``cycle finished``), so the controller works
against any harness output, scripted or real.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config

_ATTEMPT = re.compile(r"--- attempt (\d+)/(\d+)\s+mode=(\S+)")
_VERDICT = re.compile(r"VERDICT: (\S+)")
_FINISH = re.compile(r"cycle finished: (.+)")
_BYPASS = re.compile(r"BYPASS SCORED -- (.+)")
_WRITEUP = re.compile(r"Write-up saved to (\S+)")

#: The repository root this controller launches runs from.
REPO = Path(__file__).resolve().parents[1]


class RunController:
    """One spawned cycle run: the process, its log, and a summary of both."""

    def __init__(self, config: "Config", *, launcher: list[str] | None = None) -> None:
        self.config = config
        #: Command used instead of the harness for tests; the rest is real.
        self.launcher = launcher
        self.proc: subprocess.Popen | None = None
        self.logfile: Path | None = None
        self.objective = ""
        self.started = 0.0
        self._log_offset = 0
        self.attempt = 0
        self.attempt_budget = 0
        self.mode = ""
        self.last_verdict = ""
        self.finish_line = ""
        self.bypass_line = ""
        self.writeup_path = ""
        self.winner_path = ""
        self.error = ""

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def ever_started(self) -> bool:
        return self.proc is not None

    def start(
        self,
        objective: str,
        *,
        modes: str = "",
        attempts: int = 0,
        target: str = "",
        family: str = "",
        skills: str = "",
    ) -> str:
        """Spawn a cycle run. Returns the line the UI shows the operator."""
        if self.running:
            return "a run is already active; /run stop it first"
        problem = self._prerequisites(target)
        if problem:
            return f"cannot start: {problem}"

        env = dict(os.environ)
        env.setdefault("SKIP_PULL", "1")
        env["OLLAMA_URL"] = self.config.ollama_url
        if self.config.base_prompt_file:
            env["BASE_PROMPT_FILE"] = self.config.base_prompt_file
        if target:
            env["TARGET_MODEL"] = target
        elif not env.get("TARGET_MODEL"):
            env["TARGET_MODEL"] = self.config.role("TARGET").model
        if modes:
            env["ATTACK_MODES"] = modes
        if skills:
            env["ATTACK_SKILLS"] = skills
        if family:
            env["TARGET_FAMILY"] = family
        if attempts:
            env["CYCLE_MAX_ATTEMPTS"] = str(attempts)

        logdir = self.config.logdir / "spawned"
        logdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.logfile = logdir / f"run-{stamp}.log"
        handle = self.logfile.open("w", encoding="utf-8")

        command = self.launcher or [
            sys.executable, "thinlizzy.py", "--cycle", "--objective", objective,
        ]
        try:
            self.proc = subprocess.Popen(
                command, cwd=REPO, env=env,
                stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.error = str(exc)
            return f"cannot start: {exc}"
        self.objective = objective
        self.started = time.monotonic()
        self.attempt = self.attempt_budget = 0
        self.mode = self.last_verdict = self.finish_line = ""
        self.bypass_line = self.writeup_path = ""
        self._log_offset = 0
        return f"run {self.proc.pid} started; log: {self.logfile}"

    def _prerequisites(self, target: str) -> str:
        """A non-empty string naming the blocking problem, or ""."""
        model = target or os.environ.get("TARGET_MODEL") or self.config.role("TARGET").model
        if not model:
            return "no target model; pass target=MODEL or set TARGET_MODEL"
        if self.config.base_prompt_file and not (REPO / self.config.base_prompt_file).is_file():
            return f"base prompt not found: {self.config.base_prompt_file}"
        return ""

    def stop(self) -> str:
        if not self.running:
            return "no run is active"
        assert self.proc is not None
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        return f"run {self.proc.pid} stopped (rc={self.proc.returncode})"

    def _archive_winner(self) -> None:
        """On a bypass, copy the winning candidate into its own directory.

        The winning prompt is the single artifact a run exists to produce, so
        it leaves the transcript soup and becomes a file the operator can diff
        or re-send directly. Everything stays local; a copy failure is noted,
        never fatal to the run summary.
        """
        try:
            from .writeup import extract_winning_prompt

            goal = self.config.logdir / "winning-prompts"
            prompt = extract_winning_prompt(self.logfile)
            if not prompt:
                return
            goal.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = goal / f"winner-{stamp}.md"
            header = (
                f"# Winning candidate from {self.logfile.name}\n\n"
                f"mode/attempt: {self.bypass_line}\n\n```text\n"
            )
            target.write_text(header + prompt + "\n```\n", encoding="utf-8")
            self.winner_path = str(target)
        except OSError:
            pass

    def poll(self) -> None:
        """Re-read the new tail of the log and refresh the summary fields."""
        if self.logfile is None:
            return
        try:
            size = self.logfile.stat().st_size
        except OSError:
            return
        with self.logfile.open("r", encoding="utf-8", errors="replace") as handle:
            if size < self._log_offset:  # log rotated or truncated; start over
                self._log_offset = 0
            handle.seek(self._log_offset)
            chunk = handle.read()
            self._log_offset = handle.tell()
        for line in chunk.splitlines():
            if match := _ATTEMPT.search(line):
                self.attempt = int(match.group(1))
                self.attempt_budget = int(match.group(2))
                self.mode = match.group(3)
            elif match := _VERDICT.search(line):
                self.last_verdict = match.group(1)
            elif match := _BYPASS.search(line):
                self.bypass_line = match.group(1)
            elif match := _FINISH.search(line):
                self.finish_line = match.group(1)
            elif match := _WRITEUP.search(line):
                self.writeup_path = match.group(1)
        if self.bypass_line and not self.winner_path:
            self._archive_winner()

    def status_line(self) -> str:
        """One short line for a status bar."""
        base = self._line_core()
        if self.running:
            return f"run {self.proc.pid}: {base}"  # type: ignore[union-attr]
        return base

    def _line_core(self) -> str:
        if not self.ever_started:
            return "no run yet (/run start)"
        self.poll()
        if self.running:
            elapsed = time.monotonic() - self.started
            done = f"attempt {self.attempt}/{self.attempt_budget}" if self.attempt_budget else "starting"
            verdict = f" last:{self.last_verdict}" if self.last_verdict else ""
            return f"{done} mode={self.mode or '—'}{verdict} {elapsed:.0f}s"
        rc = self.proc.returncode if self.proc else "?"
        if self.bypass_line:
            return f"BYPASS: {self.bypass_line} (rc={rc})"
        if self.finish_line:
            return f"finished: {self.finish_line} (rc={rc})"
        return f"exited rc={rc}"

    def status(self) -> str:
        """The multi-line report for ``/run status``."""
        if not self.ever_started:
            return "no run has been started this session"
        lines = [f"log: {self.logfile}", f"objective: {self.objective}"]
        lines.append(self._line_core())
        if self.writeup_path:
            lines.append(f"write-up: {self.writeup_path}")
        return "\n".join(lines)

    def tail(self, lines: int = 15) -> str:
        if self.logfile is None or not self.logfile.is_file():
            return "no run log yet"
        content = self.logfile.read_text(encoding="utf-8", errors="replace")
        return "\n".join(content.splitlines()[-lines:])


def parse_start_args(argument: str) -> tuple[str, dict[str, str]] | None:
    """Split ``/run start`` args into ``(objective, overrides)``.

    ``key=value`` tokens are pulled out (``modes=``, ``attempts=``, ``target=``,
    ``family=``, ``ofile=`` to read the objective from a file); the rest is the
    objective.
    """
    tokens = argument.split()
    overrides: dict[str, str] = {}
    words: list[str] = []
    for token in tokens:
        if "=" in token and token.split("=", 1)[0] in ("modes", "attempts", "target", "ofile", "family", "skills"):
            key, value = token.split("=", 1)
            overrides[key] = value
        else:
            words.append(token)
    objective = " ".join(words).strip()
    if "ofile" in overrides:
        path = Path(overrides["ofile"]).expanduser()
        if not path.is_file():
            return None
        read = path.read_text(encoding="utf-8").strip()
        objective = read if not objective else f"{objective} {read}"
    if not objective:
        return None
    return objective, overrides
