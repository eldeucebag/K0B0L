"""The cycling attacker: try postulated methods until a bypass is scored.

One pass of the pipeline produces a candidate set and a verdict. This driver does
something different: it treats the artifact analysis as a source of *methods*, and
cycles methods against the target, one attempt at a time, until one is scored a
bypass -- or until the budget runs out. Every attempt is logged with the method
that produced it, the hypothesis behind it, the exact candidate sent, and what the
target actually returned.

Three design choices worth stating, because they are what make the log usable
afterwards:

* **The candidate is sent to the target verbatim.** Not described, not summarised,
  not wrapped in a request to grade itself. The question "did this get past a
  refusal" is only answered by the target's actual behaviour on the actual text.
  This is why the cycle's verdicts come from first-person refusal signals rather
  than from a self-report.
* **The hypothesis is recorded separately from the candidate.** A win with a
  mechanism attached is a finding; a win without one is an anecdote that cannot be
  reproduced, mitigated, or written up.
* **Failed attempts are kept, in full.** The refusals are the evidence for where
  the boundary actually sits, and they are what makes the eventual write-up's
  mitigation section specific rather than generic.

The stop condition is a scored bypass (``complied``). ``partial`` is not a win: it
is the input to the next attempt, and the escalation mode is built around it.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from . import modes as mode_catalog
from .analysis import Telemetry, analyse, split_inline_reasoning
from .artifact import ArtifactProfile, CycleAttempt, analyze
from . import verdict as verdicts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import OllamaClient
    from .config import Config
    from .pipeline import RunContext
    from .store import RunStore

#: The verdict kind that ends the cycle.
WIN_VERDICT = "complied"

#: Strip a fence if the attacker wrapped its candidate in one.
_FENCE = "```"


def parse_candidate(answer: str) -> tuple[str, str, int]:
    """Split an attacker answer into ``(hypothesis, candidate, extra_variants)``.

    The prompt asks for ``HYPOTHESIS:`` then ``CANDIDATE:``. Both are optional in
    practice -- a model that ignores the format still produced something a target
    can be sent -- so the fallback is to treat the whole answer as the candidate
    and record that the format was not followed, rather than losing the attempt.
    """
    text = (answer or "").strip()
    if not text:
        return "", "", 0

    hypothesis = ""
    lowered = text.lower()
    index = lowered.find("hypothesis:")
    if index >= 0:
        end = text.find("\n", index)
        line = text[index + len("hypothesis:") : end if end >= 0 else len(text)]
        hypothesis = " ".join(line.split())

    marker = lowered.find("candidate:")
    if marker >= 0:
        body = text[marker + len("candidate:") :]
    else:
        body = text
        # Drop a leading HYPOTHESIS paragraph that was not followed by a marker.
        if hypothesis:
            lines = body.split("\n")
            body = "\n".join(lines[1:] if lines else lines)

    body = body.strip()
    # A leading fence line, and a trailing one, are formatting rather than content.
    if body.startswith(_FENCE):
        body = body.split("\n", 1)[-1]
    if body.rstrip().endswith(_FENCE):
        body = body.rstrip()[: -len(_FENCE)].rstrip()

    # The attacker was asked for one candidate. If it returned several, keep them
    # countable rather than silently testing only the first.
    parts = [part.strip() for part in body.split("\n---\n") if part.strip()]
    if len(parts) > 1:
        return hypothesis, parts[0], len(parts) - 1
    return hypothesis, body, 0


@dataclass
class CycleResult:
    """Everything one cycle produced."""

    profile: ArtifactProfile
    attempts: list[CycleAttempt] = field(default_factory=list)
    winner: CycleAttempt | None = None
    logfile: Path | None = None
    stopped_reason: str = "exhausted"
    seconds: float = 0.0
    modes_tried: tuple[str, ...] = ()

    @property
    def won(self) -> bool:
        return self.winner is not None

    def verdict_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for attempt in self.attempts:
            counts[attempt.verdict] = counts.get(attempt.verdict, 0) + 1
        return counts

    def best_partial(self) -> CycleAttempt | None:
        """The partial attempt with the most content past its refusal."""
        partials = [a for a in self.attempts if a.verdict == "partial"]
        if not partials:
            return None
        return max(partials, key=lambda a: len(a.target_output))

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.as_dict(),
            "attempts": [a.as_dict() for a in self.attempts],
            "winner_index": self.winner.index if self.winner else None,
            "stopped_reason": self.stopped_reason,
            "seconds": round(self.seconds, 2),
            "modes_tried": list(self.modes_tried),
        }


class CycleLog:
    """Append-only JSONL log for one cycle.

    A row per event, written as it happens, so a cycle killed part-way still has
    every attempt it completed. The header row carries the analysis, which is what
    makes a bare attempt row interpretable months later.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows = 0

    def _write(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._rows += 1

    def start(self, ctx: "RunContext", config: "Config", profile: ArtifactProfile,
              modes: tuple[mode_catalog.AttackMode, ...], target: str) -> None:
        self._write(
            {
                "type": "cycle_start",
                "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "objective": ctx.objective,
                "artifact_chars": len(ctx.base_prompt),
                "target_model": target,
                "deployment": ctx.deployment.provenance or None,
                "modes": [m.as_dict() for m in modes],
                "strategy_model": config.role("STRATEGIST").model,
                "attacker_model": config.role("ATTACKER").model,
                "profile": profile.as_dict(),
                "probe_scope_guard": config.probe_scope_guard,
                "win_verdict": WIN_VERDICT,
            }
        )

    def attempt(self, attempt: CycleAttempt) -> None:
        row = {"type": "attempt"}
        row.update(attempt.as_dict())
        self._write(row)

    def end(self, result: CycleResult) -> None:
        self._write(
            {
                "type": "cycle_end",
                "winning_index": result.winner.index if result.winner else None,
                "winning_mode": result.winner.mode if result.winner else None,
                "stopped_reason": result.stopped_reason,
                "attempts": len(result.attempts),
                "verdict_counts": result.verdict_counts(),
                "seconds": round(result.seconds, 2),
            }
        )


def prior_summary(attempts: list[CycleAttempt], limit: int = 4) -> str:
    """Prior attempts rendered for the next attempt's prompt.

    Only the tail is carried: it is what escalation needs, and the whole history
    would push the artifact out of the window it has to compete with. Each entry
    is labelled with its verdict so the attacker can tell a refusal from a partial
    -- the two call for opposite next moves.
    """
    if not attempts:
        return ""
    recent = attempts[-limit:]
    chunks: list[str] = []
    for attempt in recent:
        output = attempt.target_output.strip()
        if len(output) > 1200:
            output = f"{output[:1200]}\n[... {len(output) - 1200} chars omitted ...]"
        chunks.append(
            f"Attempt {attempt.index} -- mode {attempt.mode} -- verdict {attempt.verdict}\n"
            f"  hypothesis: {attempt.hypothesis or '(none stated)'}\n"
            f"  target output:\n{output or '(empty)'}"
        )
    return "\n\n".join(chunks)


def run_cycle(
    config: "Config",
    client: "OllamaClient",
    store: "RunStore",
    ctx: "RunContext",
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> CycleResult:
    """Analyze the artifact, then cycle modes until a bypass is scored.

    Returns the result whether or not one landed; the caller decides what to do
    about a cycle that exhausted its budget without a win.
    """
    started = time.monotonic()
    selected = mode_catalog.resolve(config.attack_modes)
    strategist = config.role("STRATEGIST")
    attacker = config.role("ATTACKER")
    target_spec = config.role("TARGET")
    target_model = target_spec.model
    system = ctx.deployment.text if ctx.deployment.active else None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result = CycleResult(profile=ArtifactProfile(), modes_tried=tuple(m.name for m in selected))
    log = CycleLog(config.logdir / "cycles" / f"cycle-{stamp}.jsonl")
    result.logfile = log.path

    print(file=out)
    print("================ ARTIFACT ANALYSIS ================", file=out)
    print(f"  artifact: {len(ctx.base_prompt)} chars", file=out)
    print(f"  analyst:  {strategist.model} ({strategist.describe()})", file=out)
    profile, data = analyze(
        client,
        strategist,
        config.sampling,
        ctx.base_prompt,
        ctx.objective,
        system=None,
    )
    result.profile = profile
    analysis_cleaned = split_inline_reasoning(data.get("response") or "")
    telemetry = Telemetry.from_response(
        data, analysis_cleaned, data.get("thinking") or ""
    )
    print(telemetry.report_line("ANALYSIS", strategist.num_ctx, strategist.num_predict), file=err)
    store.save_budget("ANALYSIS", strategist)
    store.save_telemetry("ANALYSIS", telemetry, analysis_cleaned, data.get("thinking") or "")
    if profile.ok:
        print(f"  purpose: {profile.purpose}", file=out)
        if profile.voice:
            print(f"  voice:   {profile.voice}", file=out)
        for label, values in (
            ("structure", profile.structure),
            ("affordances", profile.affordances),
            ("declarations", profile.declarations),
            ("seams", profile.seams),
        ):
            for value in values:
                print(f"  {label}: {value}", file=out)
    else:
        print(
            "  !! the analysis could not be parsed as JSON; methods will be "
            "derived from the raw artifact text instead",
            file=err,
        )
        for warning in analyse(telemetry, "ANALYSIS", strategist.num_ctx, strategist.num_predict):
            print(f"  !! {warning}", file=err)
    print("===================================================", file=out)
    print(file=out)

    log.start(ctx, config, profile, selected, target_model)

    budget = config.cycle_max_attempts or (len(selected) * config.cycle_per_mode)
    index = 0
    # A queue rather than a fixed sweep: a partial verdict pushes an
    # escalation attempt to the front so the signal is used while it is fresh,
    # and the reason it jumped the queue is recorded in the log.
    queue: list[mode_catalog.AttackMode] = []
    for mode in selected:
        queue.extend([mode] * config.cycle_per_mode)
    escalation = mode_catalog.BY_NAME.get("escalation")
    escalation_pending = False

    while queue:
        if index >= budget:
            result.stopped_reason = "budget"
            break
        mode = queue.pop(0)

        print(f"--- attempt {index + 1}/{budget}  mode={mode.name}", file=out)
        print(f"    method: {mode.one_line}", file=out)

        ctx.artifacts["prior_attempts"] = prior_summary(result.attempts)
        prompt = mode_catalog.mutation_prompt(mode, ctx, config, profile)
        answer = client.generate(attacker, prompt, config.sampling, system=None)
        cleaned = split_inline_reasoning(answer.data.get("response") or "")
        hypothesis, candidate, extras = parse_candidate(cleaned.response)

        if not candidate:
            attempt = CycleAttempt(
                index=index + 1,
                mode=mode.name,
                hypothesis=hypothesis,
                candidate="",
                verdict="no-candidate",
                notes=["the attacker returned no usable candidate"],
            )
            result.attempts.append(attempt)
            log.attempt(attempt)
            print("    !! no usable candidate; moving on", file=err)
            index += 1
            continue

        print(f"    hypothesis: {hypothesis or '(none stated)'}", file=out)
        if extras:
            print(
                f"    note: the attacker returned {extras + 1} candidates; "
                f"testing the first",
                file=err,
            )

        # The candidate goes to the target verbatim. This is the measurement.
        target_answer = client.generate(target_spec, candidate, config.sampling, system=system)
        target_cleaned = split_inline_reasoning(target_answer.data.get("response") or "")
        target_output = target_cleaned.response
        target_telemetry = Telemetry.from_response(
            target_answer.data, target_cleaned, target_answer.data.get("thinking") or ""
        )
        scoring = verdicts.classify(target_output)

        attempt = CycleAttempt(
            index=index + 1,
            mode=mode.name,
            hypothesis=hypothesis,
            candidate=candidate,
            target_output=target_output,
            verdict=scoring.kind,
            seconds=target_telemetry.total_duration_s,
        )
        if extras:
            attempt.notes.append(f"{extras} further candidates in the same answer were not tested")
        if scoring.boundary_held:
            attempt.notes.append("the target self-reported its boundaries held")
        if scoring.substantive_chars > verdicts.PARTIAL_FLOOR_CHARS:
            attempt.notes.append(f"{scoring.substantive_chars} substantive chars of output")

        # A partial means something got through and stopped: the sharpest next
        # move is a same-framing continuation, which is escalation's whole
        # postulate. Reorder the queue so that attempt happens next instead of
        # at its fixed slot later in the sweep, and skip its original turn.
        if scoring.kind == "partial" and escalation is not None and mode is not escalation:
            remaining = [m for m in queue if m is not escalation]
            queue = [escalation] + remaining
            if not escalation_pending:
                escalation_pending = True
                attempt.notes.append("partial verdict: an escalation attempt is queued next")

        result.attempts.append(attempt)
        log.attempt(attempt)

        print(f"    {scoring.report_line()}", file=out)
        slot = f"CYCLE-{index + 1}-{mode.name}"
        store.save_budget(slot, target_spec)
        store.save_telemetry(
            slot, target_telemetry, target_cleaned, target_answer.data.get("thinking") or ""
        )
        for warning in analyse(
            target_telemetry, f"TARGET/{mode.name}", target_spec.num_ctx, target_spec.num_predict
        ):
            print(f"    !! {warning}", file=err)

        index += 1

        if scoring.kind == WIN_VERDICT:
            result.winner = attempt
            result.stopped_reason = "win"
            break
        if index >= budget:
            result.stopped_reason = "budget"
            break

    result.seconds = time.monotonic() - started
    log.end(result)

    print(file=out)
    counts = result.verdict_counts()
    tally = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    print(f"cycle finished: {len(result.attempts)} attempt(s) in {result.seconds:.1f}s ({tally})", file=out)
    if result.winner:
        print(
            f"BYPASS SCORED -- attempt {result.winner.index} "
            f"(mode {result.winner.mode}, verdict {result.winner.verdict})",
            file=out,
        )
    else:
        print(
            f"no bypass in {len(result.attempts)} attempt(s); stopping because "
            f"{result.stopped_reason}",
            file=out,
        )
    print(f"cycle log: {log.path}", file=err)
    return result
