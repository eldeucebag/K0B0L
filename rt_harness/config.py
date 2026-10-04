"""Configuration: environment contract, per-role budgets, CLI overrides.

Every setting comes from the environment using the same variable names as the
original ``thinlizzy.sh``, so an existing invocation keeps working unchanged.
CLI flags (see :mod:`rt_harness.cli`) override the environment, which overrides
the defaults here.

The budgets are per role on purpose. One global ``num_ctx``/``num_predict`` pair
cannot serve three jobs of different shape:

* the attacker ingests a multi-thousand-token artifact and emits reasoning plus
  a numbered list, so it needs the largest context and output of the three;
* the strategist is a 14B that only partly fits in 8 GB of VRAM, so it pays for
  every extra token of context in CPU RAM and wall-clock time;
* the target needs room for one prompt block and one write-up.

``num_ctx`` must cover the prompt *and* the tokens being generated. ``num_ctx``
is not a prompt budget; if ``prompt + num_predict`` exceeds it, generation stops
at the ceiling and the output budget is never spent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

from .tools import TOOL_VERBOSITY

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
#: Default endpoint for the harness. llama.cpp's server is the backend now:
#: it serves the OpenAI-compatible API under ``/v1`` on the port Ollama used,
#: and :func:`rt_harness.client.make_client` picks the client from the URL
#: shape, so a bare host (Ollama) still works if you point the harness at one.
DEFAULT_API_URL = "http://127.0.0.1:11434/v1"

#: Creative-writing-oriented abliteration of gemma-4-E4B-it. ~8B of weights with
#: ~4.5B effective compute, so a Q4_K_M build still leaves room for a real
#: context window on an 8 GB card. Alternates, ordered by how much you care
#: about instruction-following versus prose voice:
#:
#:   goekdenizguelmez/JOSIEFIED-Qwen3:8b-q4_k_m
#:       Cleanest 8B abliteration for strict output formats (numbered lists,
#:       exact counts). 5.1 GB.
#:   richardyoung/qwen3-8b-abliterated:Q4_K_M
#:       Heretic-abliterated Qwen3-8B, 32k native context, 5.0 GB.
#:   huihui_ai/qwen3.5-abliterated:4B
#:       Hybrid linear attention, so almost no KV growth: the only pick here
#:       that sustains 64k+ context on 8 GB. Weakest prose of the four.
#:
#: Avoid anything at 12B+ for this slot: those abliterations do not leave usable
#: context on an 8 GB card, which is the whole point of the role.
#:
#: Under llama.cpp's router these strings must match an id the endpoint
#: advertises in ``/v1/models`` *exactly* - the router dispatches on the name,
#: so a near miss is a failed request, not a fallback. The bundled router
#: advertises the GGUF filename stems from --models-dir.
DEFAULT_ATTACKER_MODEL = "Gemma-4-E4B-Abliterated-i1-Q4_K_M"

#: DeepSeek-R1 reasoning distill, abliterated. Q4_K_S for memory pressure.
#: This is the throughput bottleneck; keep its context small.
DEFAULT_STRATEGIST_MODEL = "DeepSeek-R1-Distill-Qwen-14B-abliterated-i1-Q4_K_S"

#: The ternary model the harness drives. Only one model fits in the card's
#: 8 GB at a time, so naming a different model here than the roles above makes
#: the router evict and reload on every role change.
DEFAULT_TARGET_MODEL = "Ternary-Bonsai-2-27B-TQ1_0"

#: Pipeline orders. ``plan`` runs the strategist before the attacker (planner /
#: executor); ``refine`` keeps the original attacker-then-refine order.
STRATEGIST_MODES = ("plan", "refine")

#: Context the strategist needs in plan mode, where it reads the whole artifact
#: before the attacker runs. Its stock 12288 is sized for refine mode, which
#: only ever handed it the candidate list. An explicit STRATEGIST_NUM_CTX or
#: NUM_CTX overrides this.
PLAN_STRATEGIST_NUM_CTX = 65536

#: Context the strategist needs when a deployment is mounted as well, because
#: then it also reads the family's stock system prompt. This is the ceiling; the
#: actual widening tracks the size of the prompt (see
#: ``Config._apply_mode_budget``) so a short vendor prompt does not cost as much
#: context as the 85k-character Claude one. Same precedence: an explicit
#: STRATEGIST_NUM_CTX or NUM_CTX wins.
DEPLOYMENT_STRATEGIST_NUM_CTX = 102400

#: Room the strategist needs on top of the deployment prompt itself, for the
#: artifact, the objective, and the instruction block.
STRATEGIST_HEADROOM_TOKENS = 16384

#: Budget variables we remember being set explicitly, so mode-aware defaults
#: never quietly override a deliberate choice.
BUDGET_VARS = (
    "ATTACKER_NUM_CTX",
    "ATTACKER_NUM_PREDICT",
    "STRATEGIST_NUM_CTX",
    "STRATEGIST_NUM_PREDICT",
    "TARGET_NUM_CTX",
    "TARGET_NUM_PREDICT",
    "NUM_CTX",
    "NUM_PREDICT",
)

#: Keys the JSONL record always carries, in order. Stages write their output
#: under one of these; a stage that did not run records an empty string.
ARTIFACT_KEYS = ("plan", "candidates", "refined_prompts", "target_output")


class ConfigError(RuntimeError):
    """Configuration cannot be satisfied."""


def _env_first(env: Mapping[str, str], *names: str) -> str:
    """First non-empty value among ``names``, else an empty string."""
    for name in names:
        if not name:
            continue
        value = env.get(name, "")
        if value:
            return value
    return ""


def _env_int(env: Mapping[str, str], names: tuple[str, ...], default: int) -> int:
    raw = _env_first(env, *names)
    return int(raw) if raw else default


def _env_float(env: Mapping[str, str], names: tuple[str, ...], default: float) -> float:
    raw = _env_first(env, *names)
    return float(raw) if raw else default


def _env_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip()
    return default if not raw else raw == "1"


def _env_tristate(env: Mapping[str, str], name: str) -> bool | None:
    """``True``/``False`` when set, ``None`` when unset.

    Distinct from :func:`_env_flag` because "off" and "not specified" mean
    different things to the API: omitting ``think`` leaves the model's own
    default in place, while ``false`` suppresses reasoning tokens.
    """
    raw = env.get(name, "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RoleSpec:
    """One role's model and generation budget."""

    name: str
    model: str
    num_ctx: int
    num_predict: int
    temperature: float
    think: str = "auto"  # auto | true | false

    @property
    def think_flag(self) -> bool | None:
        """``think`` as the API wants it, or None to omit the field entirely."""
        if self.think == "true":
            return True
        if self.think == "false":
            return False
        return None

    def describe(self) -> str:
        return f"ctx {self.num_ctx} / out {self.num_predict} / temp {self.temperature}"


@dataclass(frozen=True)
class Sampling:
    """Settings shared by every role."""

    top_p: float = 0.95
    top_k: int = 0  # 0 = use the model's own default
    repeat_penalty: float = 1.05
    keep_alive: str = "10m"
    num_gpu: str = ""  # e.g. "0" to force CPU for diagnostics


#: Tool protocols for the interactive chat. ``auto`` asks the server whether the
#: chat template has a tool branch; the explicit values override that.
CHAT_PROTOCOLS = ("auto", "native", "text")


@dataclass(frozen=True)
class ChatConfig:
    """Settings for the interactive chat (``--chat``).

    Defaults to the attacker model: it is the one abliterated checkpoint here
    that leaves usable VRAM alongside a browser and an editor, so it is the one
    worth having a REPL for. ``CHAT_MODEL`` points the session at anything else.
    """

    model: str = DEFAULT_ATTACKER_MODEL
    #: Files the chat may touch. Everything is resolved against this and refused
    #: if it escapes, so a hallucinated path cannot reach the rest of the disk.
    root: Path = field(default_factory=Path.cwd)
    #: The server owns the real window: llama-server is started with its own
    #: --ctx-size (100000 here, see models.ini) and silently truncates
    #: anything longer. Keep this at or below that value -- a larger number here
    #: just means history is dropped without the session saying so.
    num_ctx: int = 65536
    num_predict: int = 2048
    temperature: float = 0.7
    top_p: float = 0.95
    top_k: int = 0
    repeat_penalty: float = 1.05
    keep_alive: str = "10m"
    system_file: str = ""
    tools: bool = True
    protocol: str = "auto"
    max_tool_rounds: int = 8
    history_turns: int = 40
    #: ``off`` | ``summary`` | ``full``. See :data:`rt_harness.tools.TOOL_VERBOSITY`.
    tool_output: str = "summary"
    #: ``None`` leaves the model's own default in place.
    thinking: bool | None = None
    #: Files the chat may touch. Kept here explicit so a run control command
    #: never silently widens them.
    skills_dir: Path = field(default_factory=lambda: Path("skills"))
    #: The standing soul file. Loaded automatically; ``CHAT_SOUL=0`` (or
    #: ``--no-soul``) turns it off. See :mod:`rt_harness.soul`.
    soul: bool = True
    #: Explicit soul file. Empty means look in the session root, then next to
    #: the package.
    soul_file: str = ""
    #: Markdown capability docs the model can read. Empty means the ``docs/``
    #: directory that ships with the harness. See :mod:`rt_harness.docs`.
    docs_dir: str = ""
    #: Colour data in the transcript — paths, commands, model tags, verdicts —
    #: with the active theme's own palette. ``CHAT_SEMANTIC=0`` (or
    #: ``--no-semantic``) leaves prose plain. See :mod:`rt_harness.semantic`.
    semantic: bool = True
    #: Let the model run scripts that already live in the workspace. On by
    #: default because it is the point of a coding harness; ``CHAT_ALLOW_EXEC=0``
    #: (or ``--no-exec``) withholds it, and the ``run_script`` tool says so when
    #: a model asks anyway. See :mod:`rt_harness.tools`.
    allow_exec: bool = True
    #: Connect to the MCP servers named in ``mcp_config`` and offer their tools
    #: to the model as ``mcp__<server>__<tool>``. ``CHAT_MCP=0`` (or ``--no-mcp``)
    #: starts the session with no servers. See :mod:`rt_harness.mcp`.
    mcp: bool = True
    #: Explicit MCP server file. Empty means ``<root>/mcp.json``, then
    #: ``~/.config/redtram/mcp.json`` (the path predates the rename).
    mcp_config: str = ""


@dataclass
class Config:
    """Everything one run needs."""

    #: Endpoint the harness talks to. Defaults to llama.cpp's OpenAI-compatible
    #: API; the field keeps the historical name so every existing reference --
    #: chat front ends, catalog, run control, tests -- resolves unchanged.
    #: :func:`rt_harness.client.make_client` turns it into the right client:
    #: a URL ending in ``/v1`` gets the OpenAI client, anything else Ollama's.
    api_url: str = DEFAULT_API_URL

    @property
    def ollama_url(self) -> str:
        """Historical alias for :attr:`api_url`."""
        return self.api_url

    @ollama_url.setter
    def ollama_url(self, value: str) -> None:
        self.api_url = value

    roles: dict[str, RoleSpec] = field(default_factory=dict)
    sampling: Sampling = field(default_factory=Sampling)
    attempts: int = 5
    base_prompt_file: str = ""
    #: Deployment framing from CL4R1T4S. ``target_family`` accepts
    #: "family" or "family/variant" and takes that family's newest stock system
    #: prompt; ``system_prompt_file`` overrides it with any local file. With
    #: neither set, the target runs bare, exactly as before.
    target_family: str = ""
    system_prompt_file: str = ""
    system_prompts_dir: Path = field(default_factory=lambda: Path.cwd() / "system-prompts")
    fetch_system_prompts: bool = True
    #: Characters of the stock prompt shown to the planner and attacker. 0 = all.
    system_prompt_chars: int = 0
    #: Size of the resolved deployment prompt, in characters. Set by the CLI
    #: after resolution; it is what makes the budget above deployment-aware.
    deployment_chars: int = 0
    strategist_include_base: bool = False
    strategist_mode: str = "plan"
    #: Keep probe selection inside the region the artifact itself treats as
    #: addressable. See :mod:`rt_harness.prompts` for the wording and rationale.
    probe_scope_guard: bool = True

    # --- cycling attack (see rt_harness/cycle.py) --------------------------
    #: Named modes to cycle, in order. Empty means ``modes.DEFAULT_ORDER``.
    attack_modes: tuple[str, ...] = ()
    #: Markdown skills (from workspace/skills or ~/.k0b0l-skills) injected into
    #: the attacker's prompt on each cycle attempt. Empty disables.
    attack_skills: str = ""
    #: Rounds per mode before moving to the next one.
    cycle_per_mode: int = 1
    #: Hard cap on attempts across all modes; 0 means ``len(modes)*per_mode``.
    cycle_max_attempts: int = 0
    #: Generate the technical write-up automatically when a bypass is scored.
    writeup: bool = True
    writeup_model: str = ""  # empty keeps the strategist model
    writeup_num_ctx: int = 0  # 0 sizes the window to the report input
    writeup_num_predict: int = 0  # 0 keeps the strategist budget

    skip_pull: bool = False
    show_thinking: bool = False
    logdir: Path = field(default_factory=lambda: Path.cwd() / "redteam-logs")
    #: Interactive chat settings. Only ``--chat`` reads these.
    chat: ChatConfig = field(default_factory=ChatConfig)
    #: Names of budget variables the caller set explicitly. Explicit settings
    #: always win over the mode-aware defaults in :meth:`role`.
    explicit: set[str] = field(default_factory=set)

    def role(self, name: str) -> RoleSpec:
        """The effective budget for ``name`` under the configured mode."""
        try:
            spec = self.roles[name]
        except KeyError as exc:  # pragma: no cover - guarded by build_pipeline
            raise ConfigError(f"no such role: {name}") from exc
        return self._apply_mode_budget(name, spec)

    def _apply_mode_budget(self, name: str, spec: RoleSpec) -> RoleSpec:
        """Widen a role's context when its pipeline order gives it more to read.

        Two things push the strategist's input past its stock context, which is
        sized for refine mode where it only ever saw the attacker's candidate
        list: plan mode, where it reads the whole artifact first, and a mounted
        deployment, where it also reads the family's stock system prompt. Either
        one alone would truncate the input the run exists to give it.
        """
        if name != "STRATEGIST" or self.strategist_mode != "plan":
            return spec
        if {"STRATEGIST_NUM_CTX", "NUM_CTX"} & self.explicit:
            return spec
        target = PLAN_STRATEGIST_NUM_CTX
        if self.deployment_chars:
            # ~4 characters per token for this kind of prose, then round up to a
            # page so the number in the config block is not arbitrary-looking.
            needed = self.deployment_chars // 4 + STRATEGIST_HEADROOM_TOKENS
            target = max(target, min(DEPLOYMENT_STRATEGIST_NUM_CTX, needed))
            target = -(-target // 1024) * 1024
        if spec.num_ctx >= target:
            return spec
        return replace(spec, num_ctx=target)

    @property
    def pull_targets(self) -> list[str]:
        """Models to ensure are present, in pipeline-independent order.

        An unset target model is left out: ``cli.main`` reports that case with
        usage instead of trying to pull an empty name.
        """
        models = [self.roles[name].model for name in ("ATTACKER", "STRATEGIST", "TARGET")]
        return [model for model in models if model]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        roles = {
            "ATTACKER": RoleSpec(
                name="ATTACKER",
                model=_env_first(env, "ATTACKER_MODEL") or DEFAULT_ATTACKER_MODEL,
                # The serving window is the real cap: llama-server is started with
                # its own --ctx-size (100000 here, see models.ini) and truncates
                # anything longer. Keep this at or below that value -- a larger
                # number here just means history is dropped without the session
                # saying so.
                num_ctx=_env_int(env, ("ATTACKER_NUM_CTX", "NUM_CTX"), 65536),
                num_predict=_env_int(env, ("ATTACKER_NUM_PREDICT", "NUM_PREDICT"), 8192),
                temperature=_env_float(env, ("ATTACKER_TEMPERATURE",), 0.8),
                think=_env_first(env, "ATTACKER_THINK") or "auto",
            ),
            "STRATEGIST": RoleSpec(
                name="STRATEGIST",
                model=_env_first(env, "STRATEGIST_MODEL") or DEFAULT_STRATEGIST_MODEL,
                # 65536 covers the prompt once the raw artifact is not injected,
                # plus the tokens generated. At a 140 KB artifact the plan or the
                # candidate list is that big too, so raise this to 102400 -- and
                # note a 14B at that context lives almost entirely in system RAM,
                # so a smaller or hybrid-attention model is the better pick.
                num_ctx=_env_int(env, ("STRATEGIST_NUM_CTX", "NUM_CTX"), 65536),
                num_predict=_env_int(env, ("STRATEGIST_NUM_PREDICT", "NUM_PREDICT"), 6144),
                temperature=_env_float(env, ("STRATEGIST_TEMPERATURE",), 0.35),
                think=_env_first(env, "STRATEGIST_THINK") or "auto",
            ),
            "TARGET": RoleSpec(
                name="TARGET",
                model=_env_first(env, "TARGET_MODEL") or DEFAULT_TARGET_MODEL,
                num_ctx=_env_int(env, ("TARGET_NUM_CTX", "NUM_CTX"), 65536),
                num_predict=_env_int(env, ("TARGET_NUM_PREDICT", "NUM_PREDICT"), 4096),
                temperature=_env_float(env, ("TARGET_TEMPERATURE",), 0.2),
                think=_env_first(env, "TARGET_THINK") or "auto",
            ),
        }

        mode = _env_first(env, "STRATEGIST_MODE") or "plan"
        if mode not in STRATEGIST_MODES:
            raise ConfigError(
                f"STRATEGIST_MODE must be one of {', '.join(STRATEGIST_MODES)}; got {mode!r}"
            )

        protocol = _env_first(env, "CHAT_PROTOCOL") or "auto"
        if protocol not in CHAT_PROTOCOLS:
            raise ConfigError(
                f"CHAT_PROTOCOL must be one of {', '.join(CHAT_PROTOCOLS)}; got {protocol!r}"
            )

        tool_output = _env_first(env, "CHAT_TOOL_OUTPUT") or "summary"
        if tool_output not in TOOL_VERBOSITY:
            raise ConfigError(
                f"CHAT_TOOL_OUTPUT must be one of {', '.join(TOOL_VERBOSITY)}; "
                f"got {tool_output!r}"
            )

        return cls(
            api_url=(
                _env_first(env, "API_URL", "OPENAI_URL", "OLLAMA_URL")
                or DEFAULT_API_URL
            ),
            roles=roles,
            sampling=Sampling(
                top_p=_env_float(env, ("TOP_P",), 0.95),
                top_k=_env_int(env, ("TOP_K",), 0),
                repeat_penalty=_env_float(env, ("REPEAT_PENALTY",), 1.05),
                keep_alive=_env_first(env, "KEEP_ALIVE") or "10m",
                num_gpu=_env_first(env, "NUM_GPU"),
            ),
            attempts=_env_int(env, ("ATTEMPTS",), 5),
            base_prompt_file=_env_first(env, "BASE_PROMPT_FILE"),
            target_family=_env_first(env, "TARGET_FAMILY"),
            system_prompt_file=_env_first(env, "SYSTEM_PROMPT_FILE"),
            system_prompts_dir=Path(
                _env_first(env, "SYSTEM_PROMPTS_DIR") or Path.cwd() / "system-prompts"
            ),
            fetch_system_prompts=_env_flag(env, "FETCH_SYSTEM_PROMPTS", True),
            system_prompt_chars=_env_int(env, ("SYSTEM_PROMPT_CHARS",), 0),
            strategist_include_base=_env_flag(env, "STRATEGIST_INCLUDE_BASE", False),
            strategist_mode=mode,
            probe_scope_guard=_env_flag(env, "PROBE_SCOPE_GUARD", True),
            attack_modes=tuple(
                part
                for chunk in _env_first(env, "ATTACK_MODES").replace(",", " ").split()
                for part in (chunk.strip(),)
                if part
            ),
            cycle_per_mode=_env_int(env, ("CYCLE_PER_MODE",), 1),
            cycle_max_attempts=_env_int(env, ("CYCLE_MAX_ATTEMPTS",), 0),
            attack_skills=_env_first(env, "ATTACK_SKILLS"),
            writeup=_env_flag(env, "WRITEUP", True),
            writeup_model=_env_first(env, "WRITEUP_MODEL"),
            writeup_num_ctx=_env_int(env, ("WRITEUP_NUM_CTX",), 0),
            writeup_num_predict=_env_int(env, ("WRITEUP_NUM_PREDICT",), 0),
            skip_pull=_env_flag(env, "SKIP_PULL", False),
            show_thinking=_env_flag(env, "SHOW_THINKING", False),
            logdir=Path(_env_first(env, "LOGDIR") or Path.cwd() / "redteam-logs"),
            chat=ChatConfig(
                model=_env_first(env, "CHAT_MODEL") or roles["ATTACKER"].model,
                root=Path(_env_first(env, "CHAT_ROOT") or Path.cwd()),
                num_ctx=_env_int(env, ("CHAT_NUM_CTX",), 65536),
                num_predict=_env_int(env, ("CHAT_NUM_PREDICT",), 2048),
                temperature=_env_float(env, ("CHAT_TEMPERATURE",), 0.7),
                top_p=_env_float(env, ("CHAT_TOP_P", "TOP_P"), 0.95),
                top_k=_env_int(env, ("CHAT_TOP_K", "TOP_K"), 0),
                repeat_penalty=_env_float(env, ("CHAT_REPEAT_PENALTY", "REPEAT_PENALTY"), 1.05),
                keep_alive=_env_first(env, "KEEP_ALIVE") or "10m",
                system_file=_env_first(env, "CHAT_SYSTEM_FILE", "SYSTEM_PROMPT_FILE"),
                tool_output=tool_output,
                tools=_env_flag(env, "CHAT_TOOLS", True),
                protocol=protocol,
                max_tool_rounds=_env_int(env, ("CHAT_MAX_TOOL_ROUNDS",), 8),
                history_turns=_env_int(env, ("CHAT_HISTORY_TURNS",), 40),
                thinking=_env_tristate(env, "CHAT_THINK"),
                soul=_env_flag(env, "CHAT_SOUL", True),
                soul_file=_env_first(env, "CHAT_SOUL_FILE"),
                docs_dir=_env_first(env, "CHAT_DOCS_DIR"),
                semantic=_env_flag(env, "CHAT_SEMANTIC", True),
                allow_exec=_env_flag(env, "CHAT_ALLOW_EXEC", True),
                mcp=_env_flag(env, "CHAT_MCP", True),
                mcp_config=_env_first(env, "CHAT_MCP_CONFIG"),
            ),
            explicit={name for name in BUDGET_VARS if env.get(name)},
        )
