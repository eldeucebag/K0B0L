"""Run artefacts on disk: per-call telemetry and the append-only JSONL record.

Layout, unchanged from the shell harness so existing tooling and old runs stay
comparable::

    redteam-logs/
      run-<stamp>.jsonl
      telemetry-<stamp>/
        <SLOT>.budget.json     effective num_ctx / num_predict for the call
        <SLOT>.raw.json        verbatim response body
        <SLOT>.json            counters and truncation flags
        <SLOT>.system.txt      stock system prompt the stage ran under, if any
        <SLOT>.inline.txt      inline reasoning pulled out of `response`
        <SLOT>.thinking.txt    the separate `thinking` field

``SLOT`` is a stage's :attr:`~rt_harness.pipeline.Stage.slot`, which defaults to
its role name. Two stages may share a role as long as they declare distinct
slots; that is the hook for running, say, a second attacker round.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import CleanedOutput, Telemetry
from .config import RoleSpec


@dataclass
class RunStore:
    """Owns the log directory for one run."""

    logdir: Path
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S"))

    @property
    def telemetry_dir(self) -> Path:
        return Path(self.logdir) / f"telemetry-{self.timestamp}"

    @property
    def logfile(self) -> Path:
        return Path(self.logdir) / f"run-{self.timestamp}.jsonl"

    def prepare(self) -> None:
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)

    def slot_path(self, slot: str, suffix: str) -> Path:
        return self.telemetry_dir / f"{slot}.{suffix}"

    def save_budget(self, slot: str, spec: RoleSpec) -> None:
        """Record the effective budget, which the server does not echo back."""
        payload = {
            "role": slot,
            "model": spec.model,
            "num_ctx": spec.num_ctx,
            "num_predict": spec.num_predict,
        }
        _write_json(self.slot_path(slot, "budget.json"), payload)

    def save_raw(self, slot: str, text: str) -> None:
        """Archive the response body exactly as it arrived."""
        path = self.slot_path(slot, "raw.json")
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

    def save_telemetry(
        self,
        slot: str,
        telemetry: Telemetry,
        cleaned: CleanedOutput,
        separate_thinking: str,
    ) -> None:
        _write_json(self.slot_path(slot, "json"), telemetry.as_dict())
        if cleaned.inline_reasoning:
            self.slot_path(slot, "inline.txt").write_text(
                cleaned.inline_reasoning, encoding="utf-8"
            )
        if separate_thinking.strip():
            self.slot_path(slot, "thinking.txt").write_text(
                separate_thinking, encoding="utf-8"
            )

    def save_system_prompt(self, slot: str, text: str) -> None:
        """Archive the stock system prompt a stage ran under, verbatim.

        Kept because it is the half of the run that lives outside the JSONL
        record: a candidate is only interpretable next to the framing it was
        answered under.
        """
        self.slot_path(slot, "system.txt").write_text(text, encoding="utf-8")

    def load(self, slot: str) -> dict[str, Any]:
        """The recorded result and budget for ``slot``, either possibly None."""
        return {
            "result": _read_json(self.slot_path(slot, "json")),
            "budget": _read_json(self.slot_path(slot, "budget.json")),
        }

    def append(self, record: dict[str, Any]) -> None:
        """Append one run record to the JSONL log."""
        self.logdir.mkdir(parents=True, exist_ok=True)
        with self.logfile.open("a", encoding="utf-8") as handle:
            handle.write(_dumps(record) + "\n")

    def record_metadata(self) -> dict[str, str]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "logfile": str(self.logfile),
            "telemetry_dir": str(self.telemetry_dir),
        }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> Any:
    try:
        return _loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _loads(text: str) -> Any:
    return json.loads(text)
