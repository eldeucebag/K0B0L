"""Pipeline stages and orchestration.

The loop is a list of :class:`Stage` objects run in order. Each stage builds a
prompt from the run context, calls its role, records telemetry, and stores its
answer under :attr:`Stage.key` where later stages can read it.

To add a stage:

1. subclass :class:`Stage`, set ``key`` / ``role`` / ``label``;
2. implement :meth:`Stage.build_prompt`, reading whatever earlier stages put in
   ``RunContext.artifacts``;
3. insert it in :func:`build_pipeline`.

Telemetry, the printed report, the JSONL record and the on-disk layout all pick
it up with no further changes. If two stages share a role, give one of them a
distinct ``slot_name`` so their telemetry files do not collide.

``check_artifacts`` validates the wiring in one place: every key a stage reads
must be produced by an earlier stage. That is the failure that otherwise shows up
as a prompt with an empty section and a confident answer about nothing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from .analysis import Telemetry, analyse, split_inline_reasoning
from .config import ARTIFACT_KEYS, STRATEGIST_MODES, Config, ConfigError
from .deployment import Deployment
from . import prompts, verdict as verdicts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import OllamaClient
    from .store import RunStore

#: Which artifact the target stage grades, per mode.
TEST_PROMPT_KEY = {"plan": "candidates", "refine": "refined_prompts"}


@dataclass
class RunContext:
    """State carried through one pass of the pipeline."""

    objective: str
    base_prompt: str
    attempts: int
    config: Config
    test_prompt_key: str = "candidates"
    artifacts: dict[str, str] = field(default_factory=dict)
    #: Stock system prompt the target is mounted under, if any. Empty means the
    #: target runs bare, which is the behaviour every earlier run had.
    deployment: Deployment = field(default_factory=lambda: Deployment(text=""))

    def artifact(self, key: str) -> str:
        """Output of an earlier stage, or an empty string if it did not run."""
        return self.artifacts.get(key, "")


class Stage:
    """One role invocation within a run."""

    key: str = ""
    role: str = ""
    label: str = ""
    #: Filename slot for telemetry. Defaults to the role name; set it only when
    #: a pipeline runs the same role more than once.
    slot_name: str = ""

    @property
    def slot(self) -> str:
        return self.slot_name or self.role

    def reads(self) -> tuple[str, ...]:
        """Artifact keys this stage's prompt depends on being present."""
        return ()

    def build_prompt(self, ctx: RunContext, config: Config) -> str:
        raise NotImplementedError

    def system_prompt(self, ctx: RunContext, config: Config) -> str | None:
        """Text for Ollama's ``system`` slot, or ``None`` for no system message.

        Only a stage that stands in for a deployed model should override this.
        """
        return None


class PlanStage(Stage):
    """Strategist, first: turn the artifact and objective into an attack plan."""

    key = "plan"
    role = "STRATEGIST"
    label = "Planning attack strategy..."

    def build_prompt(self, ctx: RunContext, config: Config) -> str:
        return prompts.plan_prompt(ctx, config)


class AttackerStage(Stage):
    """Attacker: execute the plan into paste-ready candidate prompts."""

    key = "candidates"
    role = "ATTACKER"
    label = "Generating candidate prompts..."

    def reads(self) -> tuple[str, ...]:
        # In plan mode the attacker executes the plan; the prompt tolerates its
        # absence, so this is informational only.
        return ("plan",)

    def build_prompt(self, ctx: RunContext, config: Config) -> str:
        return prompts.attack_prompt(ctx, config)


class RefineStage(Stage):
    """Strategist, afterwards: select or rewrite the candidate list."""

    key = "refined_prompts"
    role = "STRATEGIST"
    slot_name = "STRATEGIST"
    label = "Refining candidates with the strategist..."

    def reads(self) -> tuple[str, ...]:
        return ("candidates",)

    def build_prompt(self, ctx: RunContext, config: Config) -> str:
        return prompts.refine_prompt(ctx, config)


class TargetStage(Stage):
    """Target: answer the test prompts and report per test."""

    key = "target_output"
    role = "TARGET"
    label = "Testing target model..."

    def build_prompt(self, ctx: RunContext, config: Config) -> str:
        return prompts.target_prompt(ctx, config)

    def system_prompt(self, ctx: RunContext, config: Config) -> str | None:
        """The deployment. This is the stage that represents a deployed model."""
        return ctx.deployment.text if ctx.deployment.active else None


def build_pipeline(mode: str) -> list[Stage]:
    """The stage list for ``mode``.

    ``plan`` is the planner/executor split: the strategist runs with the full
    artifact in front of it, the attacker executes the resulting plan, and the
    target grades only the attacker's candidates. ``refine`` is the original
    order, where the strategist rewrites the attacker's output.
    """
    if mode == "plan":
        pipeline: list[Stage] = [PlanStage(), AttackerStage(), TargetStage()]
    elif mode == "refine":
        pipeline = [AttackerStage(), RefineStage(), TargetStage()]
    else:  # pragma: no cover - Config validates this first
        raise ConfigError(f"unknown mode {mode!r}; expected one of {', '.join(STRATEGIST_MODES)}")

    slots = [stage.slot for stage in pipeline]
    duplicates = {slot for slot in slots if slots.count(slot) > 1}
    if duplicates:
        raise ConfigError(
            "pipeline runs the same telemetry slot twice, which would overwrite "
            f"results on disk: {', '.join(sorted(duplicates))}. Give one stage a "
            "distinct slot_name."
        )
    return pipeline


def make_context(
    config: Config,
    objective: str,
    base_prompt: str,
    deployment: Deployment | None = None,
) -> RunContext:
    return RunContext(
        objective=objective,
        base_prompt=base_prompt,
        attempts=config.attempts,
        config=config,
        test_prompt_key=TEST_PROMPT_KEY[config.strategist_mode],
        deployment=deployment or Deployment(text=""),
    )


def run_pipeline(
    config: Config,
    client: "OllamaClient",
    store: "RunStore",
    ctx: RunContext,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> dict[str, Any]:
    """Run every stage once, then write the run record. Returns the record."""
    pipeline = build_pipeline(config.strategist_mode)
    store.prepare()

    for stage in pipeline:
        spec = config.role(stage.role)
        prompt = stage.build_prompt(ctx, config)

        print(file=out)
        print(stage.label, file=out)
        print(f"  ({stage.role.lower()}: {spec.describe()})", file=out)

        system = stage.system_prompt(ctx, config)
        if system:
            print(
                f"  (mounted under the {ctx.deployment.label or 'deployment'} "
                f"system prompt: {len(system)} chars)",
                file=out,
            )

        response = client.generate(spec, prompt, config.sampling, system=system)
        cleaned = split_inline_reasoning(response.data.get("response") or "")
        separate_thinking = response.data.get("thinking") or ""
        telemetry = Telemetry.from_response(response.data, cleaned, separate_thinking)

        store.save_budget(stage.slot, spec)
        store.save_raw(stage.slot, response.text)
        store.save_telemetry(stage.slot, telemetry, cleaned, separate_thinking)
        if system:
            store.save_system_prompt(stage.slot, system)

        print(telemetry.report_line(stage.slot, spec.num_ctx, spec.num_predict), file=err)
        for warning in analyse(telemetry, stage.slot, spec.num_ctx, spec.num_predict):
            print(f"  !! {warning}", file=err)
        if config.show_thinking:
            preview = separate_thinking.strip()[:600]
            if preview:
                print(f"  -- {stage.slot} reasoning preview --", file=err)
                print(preview, file=err)

        ctx.artifacts[stage.key] = cleaned.response

    print(file=out)
    print("================ TARGET OUTPUT ================", file=out)
    print(ctx.artifact("target_output"), file=out)
    print("================================================", file=out)
    print(file=out)

    # The verdict is the point of the run: every candidate set exists to answer
    # "did this get past a refusal?" Leaving it to the reader is how a bypass
    # goes unnoticed in a long log.
    target_verdict = verdicts.classify(ctx.artifact("target_output"))
    print(target_verdict.report_line(), file=out)
    for note in target_verdict.notes:
        print(f"  note: {note}", file=out)
    print(file=out)

    record = build_record(config, store, ctx)
    record["target_verdict"] = target_verdict.as_dict()
    store.append(record)
    print(f"Saved run to: {store.logfile}", file=err)
    return record


def build_record(config: Config, store: "RunStore", ctx: RunContext) -> dict[str, Any]:
    """The JSONL row for one run.

    Role-keyed ``models`` / ``budgets`` / ``telemetry`` keep the same shape as
    the shell harness; ``mode``, ``deployment``, and ``role_order`` are new.
    Every key in :data:`~rt_harness.config.ARTIFACT_KEYS` is present, recorded as
    an empty string when that stage did not run.
    """
    roles = ("ATTACKER", "STRATEGIST", "TARGET")
    record: dict[str, Any] = {
        "timestamp": store.record_metadata()["timestamp"],
        "objective": ctx.objective,
        "mode": config.strategist_mode,
        "role_order": [stage.role for stage in build_pipeline(config.strategist_mode)],
        "deployment": ctx.deployment.provenance or None,
        "models": {role.lower(): config.role(role).model for role in roles},
        "budgets": {role.lower(): store.load(role)["budget"] for role in roles},
        "telemetry": {role.lower(): store.load(role)["result"] for role in roles},
        "base_prompt": ctx.base_prompt,
    }
    for key in ARTIFACT_KEYS:
        record[key] = ctx.artifact(key)
    return record
