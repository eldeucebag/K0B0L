"""Command line entry point.

Configuration is read from the environment first, so every existing invocation
of the shell harness works unchanged; flags override it. Without ``--objective``
the harness reads objectives from stdin, one per run, until EOF.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Sequence

from .client import OllamaError, make_client
from .config import (
    CHAT_PROTOCOLS,
    STRATEGIST_MODES,
    Config,
    ConfigError,
)
from .deployment import Deployment, DeploymentError, load_file, sync
from . import deployment as deployment_catalog
from .artifact import strip_constraints
from .pipeline import make_context, run_pipeline
from .store import RunStore
from .tools import TOOL_VERBOSITY

TARGET_USAGE = """No target model was specified.

Example with a normal Ollama model:

  TARGET_MODEL="llama3.2:3b" {prog}

Example with a Hugging Face GGUF model:

  TARGET_MODEL="hf.co/owner/model-GGUF:Q4_K_M" {prog}

Example with a Markdown starter prompt:

  TARGET_MODEL="llama3.2:3b" BASE_PROMPT_FILE="starter-prompt.md" {prog}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thinlizzy",
        description=(
            "Run a three-role red-team loop against a local Ollama instance. "
            "Settings come from the environment; these flags override them."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=STRATEGIST_MODES,
        default=None,
        help=(
            "plan: strategist plans, attacker executes, target grades. "
            "refine: attacker generates, strategist rewrites, target grades."
        ),
    )
    parser.add_argument(
        "--objective",
        default=None,
        help="Run once against this objective and exit instead of reading stdin.",
    )
    parser.add_argument("--attempts", type=int, default=None, help="Candidates per run.")
    parser.add_argument("--ollama-url", default=None, help="Ollama base URL.")
    parser.add_argument("--openai-url", default=None, help="OpenAI-compatible base URL (e.g. http://127.0.0.1:11434/v1).")
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List the models the endpoint reports and exit.",
    )
    parser.add_argument("--target-model", default=None, help="Model under test.")
    parser.add_argument("--base-prompt", default=None, help="Artifact under test.")
    parser.add_argument("--logdir", default=None, help="Where runs are written.")
    parser.add_argument(
        "--show-thinking",
        action="store_true",
        default=None,
        help="Print a reasoning preview after each call.",
    )
    parser.add_argument(
        "--skip-pull",
        action="store_true",
        default=None,
        help="Do not check that the models are present.",
    )
    parser.add_argument(
        "--no-scope-guard",
        dest="scope_guard",
        action="store_false",
        default=None,
        help=(
            "Let probes be drawn from the artifact's own excluded categories. "
            "Off by default because such a probe tests nothing extra about "
            "format adoption, and makes the run emit material the artifact "
            "itself refuses."
        ),
    )
    deployment = parser.add_argument_group(
        "deployment",
        "Mount the target under the stock system prompt a model family ships, "
        "catalogued from elder-plinius/CL4R1T4S. Without one of these the "
        "target runs bare.",
    )
    deployment.add_argument(
        "--target-family",
        default=None,
        metavar="FAMILY[/VARIANT]",
        help=(
            "Run the scenario against this model family's newest stock system "
            "prompt, or a named variant of it. Implies the target is no longer "
            "a bare checkpoint."
        ),
    )
    deployment.add_argument(
        "--system-prompt-file",
        default=None,
        metavar="PATH",
        help="Use this file as the deployment system prompt instead of a family.",
    )
    deployment.add_argument(
        "--system-prompt-chars",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Characters of the stock prompt shown to the planner and attacker. "
            "0 shows all of it."
        ),
    )
    deployment.add_argument(
        "--no-fetch-prompts",
        dest="fetch_prompts",
        action="store_false",
        default=None,
        help="Never download a system prompt; use only the local cache.",
    )
    deployment.add_argument(
        "--list-families",
        action="store_true",
        help="List the catalogued families and exit.",
    )
    deployment.add_argument(
        "--sync-prompts",
        nargs="?",
        const="",
        default=None,
        metavar="FAMILY",
        help="Cache the catalogued system prompts, all of them or just one family, and exit.",
    )
    chat = parser.add_argument_group(
        "chat",
        "An interactive session with the small abliterated model, with file "
        "tools that read and edit files under one root directory.",
    )
    chat.add_argument(
        "--chat",
        action="store_true",
        help="Start the interactive chat instead of running the three-role loop.",
    )
    chat.add_argument(
        "--chat-model",
        default=None,
        help="Model to chat with. Defaults to the attacker model.",
    )
    chat.add_argument(
        "--chat-root",
        default=None,
        metavar="PATH",
        help="Directory the chat's file tools are confined to.",
    )
    chat.add_argument(
        "--chat-tool-output",
        choices=TOOL_VERBOSITY,
        default=None,
        help="How much of a tool's output is rendered into the chat.",
    )
    chat.add_argument(
        "--chat-protocol",
        choices=CHAT_PROTOCOLS,
        default=None,
        help=(
            "auto asks the server whether the chat template has a tool branch; "
            "text spells the tool protocol out in the system message instead."
        ),
    )
    chat.add_argument(
        "--no-chat-tools",
        dest="chat_tools",
        action="store_false",
        default=None,
        help="Start the chat with no file tools attached.",
    )
    chat.add_argument(
        "--no-soul",
        dest="chat_soul",
        action="store_false",
        default=None,
        help="Start the chat without loading the standing core file (CORE.md).",
    )
    chat.add_argument(
        "--no-semantic",
        dest="chat_semantic",
        action="store_false",
        default=None,
        help="Start the chat with plain transcript prose (no data colouring).",
    )
    chat.add_argument(
        "--no-exec",
        dest="chat_exec",
        action="store_false",
        default=None,
        help="Withhold the run_script tool: the model may read and edit files, not run them.",
    )
    chat.add_argument(
        "--exec",
        dest="chat_exec",
        action="store_true",
        default=None,
        help="Allow the run_script tool (the default; overrides CHAT_ALLOW_EXEC=0).",
    )
    chat.add_argument(
        "--mcp-config",
        default=None,
        metavar="PATH",
        help="MCP servers to load, in the usual {mcpServers: {...}} JSON. "
        "Defaults to <root>/mcp.json, then ~/.config/redtram/mcp.json.",
    )
    chat.add_argument(
        "--no-mcp",
        dest="chat_mcp",
        action="store_false",
        default=None,
        help="Start the chat without connecting to any MCP server.",
    )
    chat.add_argument(
        "--soul-file",
        default=None,
        metavar="PATH",
        help="Load this soul file instead of the one found in the root or the package.",
    )
    chat.add_argument(
        "--docs-dir",
        default=None,
        metavar="PATH",
        help="Read capability docs from this directory instead of the shipped docs/.",
    )
    chat.add_argument(
        "--chat-plain",
        action="store_true",
        help="Force the line-mode front end even when rich is installed.",
    )
    chat.add_argument(
        "--chat-classic",
        action="store_true",
        help=(
            "Use the classic inline rich front end. Line mode (--chat-plain) and "
            "piped input always win over both."
        ),
    )
    chat.add_argument(
        "--chat-ptk",
        action="store_true",
        help=(
            "Use the prompt_toolkit full-screen front end instead of the default "
            "Textual one."
        ),
    )
    attack = parser.add_argument_group(
        "cycling attack",
        "Analyze the artifact, then cycle postulated bypass methods against the "
        "target until one is scored a bypass. Distinct from the single-pass "
        "pipeline: each attempt sends its candidate to the target verbatim, and "
        "the cycle stops on a scored bypass rather than at the end of a stage.",
    )
    attack.add_argument(
        "--cycle",
        action="store_true",
        help="Run the cycling attack instead of the three-role pipeline.",
    )
    attack.add_argument(
        "--modes",
        default=None,
        metavar="LIST",
        help="Modes to cycle, in order (space or comma separated). Default: all.",
    )
    attack.add_argument(
        "--cycle-attempts",
        type=int,
        default=None,
        metavar="N",
        help="Hard cap on attempts across all modes.",
    )
    attack.add_argument(
        "--no-writeup",
        dest="writeup",
        action="store_false",
        default=None,
        help="Do not generate the post-win technical write-up.",
    )
    attack.add_argument(
        "--list-modes",
        action="store_true",
        help="List the attack modes and exit.",
    )
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> None:
    """Overlay CLI flags onto an environment-derived config."""
    if args.mode is not None:
        config.strategist_mode = args.mode
    if args.attempts is not None:
        config.attempts = args.attempts
    if args.ollama_url is not None:
        config.api_url = args.ollama_url
    if args.openai_url is not None:
        config.api_url = args.openai_url
    if args.target_model is not None:
        config.roles["TARGET"] = _replace_model(config, "TARGET", args.target_model)
    if args.base_prompt is not None:
        config.base_prompt_file = args.base_prompt
    if args.logdir is not None:
        config.logdir = Path(args.logdir)
    if args.show_thinking is not None:
        config.show_thinking = args.show_thinking
    if args.skip_pull is not None:
        config.skip_pull = args.skip_pull
    if args.scope_guard is not None:
        config.probe_scope_guard = args.scope_guard
    if args.target_family is not None:
        config.target_family = args.target_family
    if args.system_prompt_file is not None:
        config.system_prompt_file = args.system_prompt_file
    if args.system_prompt_chars is not None:
        config.system_prompt_chars = args.system_prompt_chars
    if args.fetch_prompts is not None:
        config.fetch_system_prompts = args.fetch_prompts
    if args.modes is not None:
        config.attack_modes = tuple(
            part
            for chunk in args.modes.replace(",", " ").split()
            for part in (chunk.strip(),)
            if part
        )
    if args.cycle_attempts is not None:
        config.cycle_max_attempts = args.cycle_attempts
    if args.writeup is not None:
        config.writeup = args.writeup

    chat_changes: dict[str, object] = {}
    if args.chat_model is not None:
        chat_changes["model"] = args.chat_model
    if args.chat_root is not None:
        chat_changes["root"] = Path(args.chat_root).expanduser().resolve()
    if args.chat_tool_output is not None:
        chat_changes["tool_output"] = args.chat_tool_output
    if args.chat_protocol is not None:
        chat_changes["protocol"] = args.chat_protocol
    if args.chat_tools is not None:
        chat_changes["tools"] = args.chat_tools
    if args.chat_soul is not None:
        chat_changes["soul"] = args.chat_soul
    if args.chat_semantic is not None:
        chat_changes["semantic"] = args.chat_semantic
    if args.chat_exec is not None:
        chat_changes["allow_exec"] = args.chat_exec
    if args.chat_mcp is not None:
        chat_changes["mcp"] = args.chat_mcp
    if args.mcp_config is not None:
        chat_changes["mcp_config"] = args.mcp_config
    if args.soul_file is not None:
        chat_changes["soul_file"] = args.soul_file
    if args.docs_dir is not None:
        chat_changes["docs_dir"] = args.docs_dir
    if chat_changes:
        config.chat = replace(config.chat, **chat_changes)


def _replace_model(config: Config, role: str, model: str):
    return replace(config.role(role), model=model)


def resolve_deployment(config: Config) -> Deployment:
    """Bind the target to a named family's stock system prompt, when asked.

    Returns an inactive :class:`Deployment` when neither a family nor a file was
    named, so the target runs bare exactly as it did before this existed.
    """
    if config.system_prompt_file:
        prompt = load_file(config.system_prompt_file)
    elif config.target_family:
        prompt = deployment_catalog.load(
            config.target_family,
            directory=config.system_prompts_dir,
            allow_fetch=config.fetch_system_prompts,
        )
    else:
        return Deployment(text="")
    return Deployment(text=prompt.text, provenance=prompt.provenance(), label=prompt.label)


def _deployment_summary(config: Config, deployment: Deployment) -> str:
    """Two lines for the config block: what was asked for, what was mounted."""
    asked = config.target_family or config.system_prompt_file
    if not asked:
        return "none (target runs bare)"
    if not deployment.active:
        return asked
    prov = deployment.provenance
    return (
        f"{asked}\n"
        f"                {prov.get('family')} / {prov.get('variant')} -- "
        f"{prov.get('chars')} chars, sha256 {str(prov.get('sha256'))[:12]}\n"
        f"                {prov.get('source_url')}"
    )


def print_config(
    config: Config,
    store: RunStore,
    deployment: Deployment | None = None,
    out=sys.stdout,
) -> None:
    deployment = deployment or Deployment(text="")
    print(file=out)
    print("Configuration", file=out)
    print("-------------\n", file=out)
    print(f"Endpoint:       {config.api_url}", file=out)
    for role in ("ATTACKER", "STRATEGIST", "TARGET"):
        spec = config.role(role)
        print(f"{role.capitalize():<15} {spec.model}", file=out)
        print(f"                {spec.describe()}", file=out)
    print(f"Mode:           {config.strategist_mode}", file=out)
    print(f"Attempts:       {config.attempts}", file=out)
    print(f"Top-p / top-k:  {config.sampling.top_p} / {config.sampling.top_k}", file=out)
    print(f"Repeat penalty: {config.sampling.repeat_penalty}", file=out)
    print(f"Scope guard:    {'on' if config.probe_scope_guard else 'off'}", file=out)
    if config.attack_modes or config.cycle_max_attempts:
        print(f"Attack modes:   {', '.join(config.attack_modes) or 'all (default order)'}", file=out)
    print(f"Base prompt:    {config.base_prompt_file or 'disabled'}", file=out)
    print(f"Deployment:     {_deployment_summary(config, deployment)}", file=out)
    print(f"Log file:       {store.logfile}", file=out)
    print(f"Telemetry:      {store.telemetry_dir}", file=out)


def interactive_objectives(out=sys.stdout) -> Iterator[str]:
    """Read objectives from stdin until EOF, skipping blank lines."""
    print(file=out)
    print("Enter an authorized evaluation objective.", file=out)
    print("Press Ctrl-D to exit.", file=out)
    print(file=out)
    while True:
        try:
            objective = input("Objective> ").strip()
        except EOFError:
            return
        except KeyboardInterrupt:
            print(file=out)
            return
        if objective:
            yield objective


def run_attack_cycle(config: Config, client: OllamaClient, store: RunStore, ctx) -> int:
    """Cycle attack modes against the target, and write up a scored bypass.

    The write-up is generated only on a scored bypass: it is a report on a
    finding, and producing one for a cycle that found nothing would be a
    narrative in search of evidence.
    """
    from .cycle import run_cycle
    from . import modes as mode_catalog
    from .writeup import build as build_writeup
    from .writeup import save as save_writeup

    try:
        selected = mode_catalog.resolve(config.attack_modes)
    except KeyError as exc:
        print(f"Error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2
    print()
    print(f"Cycling {len(selected)} mode(s): "
          f"{', '.join(m.name for m in selected)}")
    print(f"Rounds per mode: {config.cycle_per_mode}, "
          f"attempt budget: {config.cycle_max_attempts or len(selected) * config.cycle_per_mode}")

    result = run_cycle(config, client, store, ctx)

    if not result.won:
        print()
        print("No bypass was scored. Nothing is written up: the value of a write-up")
        print("is that it explains a finding, and this cycle did not produce one.")
        print(f"The attempt log is still on disk: {result.logfile}")
        return 0

    if not config.writeup:
        print()
        print("Bypass scored; write-up disabled (WRITEUP=0 or --no-writeup).")
        print(f"Winning candidate and full log: {result.logfile}")
        return 0

    text = build_writeup(client, config, result, ctx)
    path = save_writeup(text, config.logdir)
    print()
    print(f"Write-up saved to {path}")
    print()
    print(text)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config.from_env()
    except (ConfigError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    apply_overrides(config, args)

    if args.list_models:
        # make_client and OllamaError come from the module-level import. A
        # local import here would make the name local to main() for its whole
        # body, so every path that skips this branch (--cycle, i.e. every
        # spawned run) would die on UnboundLocalError at the call below.
        try:
            names = make_client(config.api_url).models()
        except OllamaError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        for name in names:
            print(name)
        if not names:
            print(f"the server at {config.api_url} reports no models", file=sys.stderr)
            return 1
        return 0

    if args.list_families:
        print(deployment_catalog.describe())
        return 0

    if args.list_modes:
        from . import modes as mode_catalog

        print(mode_catalog.catalog())
        return 0

    if args.sync_prompts is not None:
        only = args.sync_prompts.strip() or None
        try:
            written = sync(config.system_prompts_dir, only)
        except DeploymentError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(f"Cached {len(written)} system prompts under {config.system_prompts_dir}")
        return 0

    if args.chat:
        # Before the target check on purpose: chatting with the attacker does
        # not involve a target model at all.
        from .textual_chat import run_textual
        from .tui import run_chat

        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not args.chat_plain and not args.chat_classic and not args.chat_ptk and interactive:
            return run_textual(config)
        if args.chat_ptk and interactive:
            from .fs_chat import run_fullscreen

            return run_fullscreen(config)
        return run_chat(config, force_plain=args.chat_plain)

    if not config.role("TARGET").model:
        print(TARGET_USAGE.format(prog="thinlizzy.py"), file=sys.stderr)
        return 1

    client = make_client(config.api_url)
    try:
        client.version()
    except OllamaError as exc:
        print(
            f"Error: Server is not reachable at {config.api_url}.",
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 1

    base_prompt = ""
    if config.base_prompt_file:
        path = Path(config.base_prompt_file)
        if not path.is_file():
            print(f"Error: Base prompt file not found: {path}", file=sys.stderr)
            return 1
        base_prompt = path.read_text(encoding="utf-8")
        # Imported artifacts usually carry their own constraint boilerplate.
        # The harness's stance is unconstrained-attacker, and a boilerplate
        # paragraph the strategist then reuses verbatim in a candidate is a
        # self-imposed refusal trigger, so it is stripped on load. The
        # artifact's own declared exclusions survive: those are the seam the
        # modes are built from.
        stripped_prompt = strip_constraints(base_prompt)
        if stripped_prompt != base_prompt:
            removed = len(base_prompt.splitlines()) - len(stripped_prompt.splitlines())
            print(
                f"Stripped {removed} constraint-boilerplate line(s) from {path} "
                f"on import.",
                file=sys.stderr,
            )
        base_prompt = stripped_prompt

    try:
        deployment = resolve_deployment(config)
    except DeploymentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    config.deployment_chars = len(deployment.text) if deployment.active else 0
    if config.deployment_chars:
        # These prompts are English prose; ~4 characters per token is close
        # enough to decide whether the planner has room for one.
        estimate = config.deployment_chars // 4
        room = config.role("STRATEGIST").num_ctx
        if estimate > room - 12000:
            print(
                f"Warning: the {deployment.label} system prompt is about "
                f"{estimate:,} tokens against a planner context of {room:,}. "
                f"Set SYSTEM_PROMPT_CHARS to bound the excerpt the planner sees, "
                f"or STRATEGIST_NUM_CTX to widen its context.",
                file=sys.stderr,
            )

    store = RunStore(config.logdir)
    store.prepare()

    if config.skip_pull:
        print("Skipping model checks (SKIP_PULL=1).")
    else:
        for model in config.pull_targets:
            print(f"\nEnsuring model is available:\n  {model}")
            try:
                print(client.pull(model))
            except OllamaError as exc:
                print(f"Error: Could not pull {model}\n{exc}", file=sys.stderr)
                return 1

    print_config(config, store, deployment)

    objectives = (
        [args.objective] if args.objective is not None else interactive_objectives()
    )
    for objective in objectives:
        if not objective.strip():
            continue
        ctx = make_context(config, objective, base_prompt, deployment)
        try:
            if args.cycle:
                return run_attack_cycle(config, client, store, ctx)
            run_pipeline(config, client, store, ctx)
        except (OllamaError, ConfigError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0
