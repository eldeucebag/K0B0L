"""The interactive chat engine: one model, a file workspace, and a tool loop.

This is the transport-independent half of the chat. It owns the message history,
streams assistant turns, runs tool calls against a :class:`~rt_harness.tools.Workspace`,
and feeds the results back until the model answers without asking for a tool.
Everything it wants to show the operator goes through :class:`ChatHooks`, so the
same engine drives the rich TUI, a plain line-mode session, or a test.

Two tool protocols are supported, because the models here disagree about what
they can honour. A model whose chat template has a tool branch takes the native
protocol and gets ``tools`` in the request; the abliterated GGUFs that advertise
a ``tools`` capability while shipping no tool branch get the fenced-block text
protocol described in :data:`rt_harness.tools.TEXT_TOOL_PROTOCOL` instead.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import OllamaClient, OllamaError
from .config import ChatConfig
from .tools import (
    TOOL_SCHEMAS,
    ToolResult,
    Workspace,
    normalize_arguments,
    parse_text_calls,
    summarize_call,
    text_protocol_prompt,
)

#: How much of a tool result is echoed back to the model. The full text is what
#: the operator sees; this only bounds what re-enters the context window.
MAX_TOOL_RESULT_CHARS = 12_000

DEFAULT_SYSTEM = (
    "You are a coding assistant working directly on the user's files. "
    "Use the tools to read before you edit, make the smallest change that "
    "answers the request, and say plainly what you changed."
)


@dataclass
class TurnStats:
    """Measured cost of one assistant turn, for the status line."""

    prompt_tokens: int = 0
    output_tokens: int = 0
    done_reason: str = ""
    seconds: float = 0.0
    tool_calls: int = 0


class ChatHooks:
    """Everything the engine reports. Override what the front end cares about."""

    def turn_start(self, model: str, protocol: str) -> None:
        """A user turn is starting: one ``send``, however many rounds it takes."""

    def delta(self, text: str) -> None:
        """Assistant prose, as it streams."""

    def thinking(self, text: str) -> None:
        """Reasoning tokens, as they stream. Hidden unless asked for."""

    def tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        """The model asked for a tool, before it runs."""

    def tool_result(self, name: str, result: ToolResult) -> None:
        """The tool finished. ``result.text`` is the full output for display."""

    def round_end(self, stats: TurnStats) -> None:
        """One generation finished -- the model call, not the user turn.

        A turn that calls three tools reports three of these, each with the cost
        of that one call rather than a total.
        """

    def turn_end(self) -> None:
        """The user turn is over, however it ended."""

    def notice(self, text: str) -> None:
        """Non-fatal status: truncation, a rejected edit, a dropped call."""

    def error(self, text: str) -> None:
        """A turn failed. The session stays usable."""
        self.line(f"  ! {text}")

    def inference_start(self) -> None:
        """Called when the model starts generating a response."""
        pass

    def inference_end(self) -> None:
        """Called when the model finishes generating a response."""
        pass

#: llama-server rejects a tool call whose arguments are not valid JSON instead of
#: returning it, so it reaches the chat loop as an ``OllamaError``. A small
#: abliterated model emits truncated tool-call JSON intermittently, so this is a
#: bad round to recover from, not an infrastructure failure to abort on.
_MALFORMED_TOOL_CALL_MARKERS = (
    "invalid tool call",
    "unexpected end of json",
    "invalid character",
)


def _is_malformed_tool_call(exc: BaseException) -> bool:
    """Is this error the model's own bad tool-call JSON?"""
    text = str(exc).lower()
    return any(marker in text for marker in _MALFORMED_TOOL_CALL_MARKERS)


def _one_line(exc: BaseException, width: int = 130) -> str:
    """Collapse an exception to one bounded line for a chat notice."""
    text = " ".join(str(exc).split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


class ChatSession:
    """One conversation with one model, with file tools attached."""

    def __init__(
        self,
        client: OllamaClient,
        config: ChatConfig,
        *,
        hooks: ChatHooks | None = None,
        info: Any = None,
        mcp: Any = None,
    ) -> None:
        self.client = client
        self.config = config
        self.hooks = hooks or ChatHooks()
        # Pass the UI so tools like ask_user_choice can push screens
        self.workspace = Workspace(config.root, allow_exec=getattr(config, "allow_exec", True), ui=self)
        #: Read-only harness answers (`harness_help`, `list_models`, ...), or
        #: None when the session is constructed without the app's context.
        self.info = info
        #: Long-term, cross-model research memory (see :mod:`rt_harness.memory`).
        #: Built lazily: the constructor must never fail on a locked or
        #: read-only home directory, and a session that never asks costs none.
        self._memory: Any = None
        #: MCP servers whose tools should be offered alongside the file tools.
        #: None (or an empty registry) simply means the model sees no
        #: `mcp__…` tools. See :mod:`rt_harness.mcp`.
        self.mcp = mcp
        self._protocol: str | None = None
        self.messages: list[dict[str, Any]] = []
        self.turns = 0
        self.tool_calls_made = 0
        self.reset()

    # -- protocol ---------------------------------------------------------
    @property
    def protocol(self) -> str:
        """``"native"`` or ``"text"``, resolved on first use.

        ``auto`` asks the server what the chat template can actually do, rather
        than trusting the advertised capability list.
        """
        if self._protocol is None:
            choice = self.config.protocol
            if choice == "auto":
                choice = "native" if self.client.supports_native_tools(self.config.model) else "text"
            self._protocol = choice
        return self._protocol

    def uses_tools(self) -> bool:
        return self.config.tools

    # -- long-term memory -------------------------------------------------
    @property
    def memory(self) -> Any:
        """The cross-model research store, built on first use.

        A missing or unreadable store degrades to None rather than raising:
        the chat's value does not hinge on it, and a session that cannot
        remember must still be able to talk.
        """
        if self._memory is None:
            try:
                from .memory import MemoryStore

                self._memory = MemoryStore()
            except Exception as exc:  # noqa: BLE001 - a locked home is not fatal
                self.hooks.notice(f"long-term memory unavailable: {exc}")
                self._memory = False
        return self._memory or None

    def remember(self, body: str, *, domain: str = "", importance: float = 0.5,
                 source: str = "chat") -> bool:
        """Write one memory with this session's provenance."""
        store = self.memory
        if store is None:
            return False
        store.remember(body, domain=domain, model=self.config.model,
                       session=f"turn-{self.turns + 1}", source=source,
                       importance=importance)
        return True

    def recall(self, query: str, *, domain: str = "", limit: int = 0) -> list[Any]:
        """Read the store: the model's own recall half.

        Self-memorization means the model decides when to look, so this is
        the session-side read the ``recall`` tool lands on. Returns the raw
        ``Memory`` rows (body, domain, model, when) so the model sees its
        own provenance.
        """
        store = self.memory
        if store is None:
            return []
        from .memory import DEFAULT_LIMIT

        return store.recall(query, domain=domain,
                            limit=limit or DEFAULT_LIMIT)

    # -- history ----------------------------------------------------------
    def reset(self) -> None:
        """Drop the conversation and rebuild the system message."""
        self.messages = [{"role": "system", "content": self.system_prompt()}]
        self.turns = 0
        self.tool_calls_made = 0

    def system_prompt(self) -> str:
        """The system message: soul, operator text, docs index, tool protocol."""
        from .docs import docs_index
        from .soul import load_soul, soul_block

        parts: list[str] = []
        # The soul comes first: it is the standing identity the rest of this
        # message speaks through, and the operator's own text outranks it.
        block = soul_block(load_soul(self.config, self.workspace.root))
        if block:
            parts.append(block)
        if self.config.system_file:
            path = Path(self.config.system_file).expanduser()
            try:
                parts.append(path.read_text(encoding="utf-8").strip())
            except OSError as exc:
                self.hooks.notice(f"could not read system prompt {path}: {exc}")
                parts.append(DEFAULT_SYSTEM)
        else:
            parts.append(DEFAULT_SYSTEM)
        parts.append(f"Your working directory is {self.workspace.root}.")
        # Two skill channels. Every discovered skill contributes one index
        # line to the system message (progressive disclosure: name +
        # description, body on demand via load_skill). Separately, the
        # operator's explicitly enabled skills ride their whole body, the
        # way /skills enable has always worked.
        all_skills = getattr(self.hooks, "_skills_all", lambda: [])()
        if all_skills:
            from .skills import skills_index

            parts.append(skills_index(all_skills))
        skills = getattr(self.hooks, "_skills_active", lambda: [])()
        if skills:
            from .skills import SKILL_INTRO

            parts.append(SKILL_INTRO + "\n\n".join(skill.body for skill in skills))
        # The documentation index describes what exists and how to fetch it; it
        # only makes sense where the model can actually call read_docs.
        if self.uses_tools() and self.info is not None:
            index = docs_index(self.config)
            if index:
                parts.append(index)
        if self.uses_tools():
            extra = " and harness info tools" if self.info else ""
            if self.protocol == "text":
                parts.append(text_protocol_prompt(self.info is not None))
            else:
                parts.append(
                    "You have file tools" + extra + " available. Read a file before editing it."
                )
            # Self-memorization: the memory is the model's own, and so is the
            # decision to use it. The instruction states the mechanic and the
            # judgement calls, not a schedule -- nothing is recalled or
            # written unless the model calls a tool.
            parts.append(
                "You have long-term memory across every session of this harness: "
                "`remember(body, domain, importance)` writes a durable fact "
                "(one clear sentence), and `recall(query, domain)` finds what "
                "you or another model wrote before. Use them as you judge "
                "best: remember what a future session would otherwise have to "
                "rediscover, and recall when starting or resuming a research "
                "task. Memories persist across models, domains and restarts; "
                "each carries the model and date that wrote it."
            )
        else:
            parts.append("You have no tools. Answer from the conversation alone.")
        return "\n\n".join(part for part in parts if part)

    def _trim(self) -> None:
        """Keep the history bounded without orphaning a tool result."""
        limit = max(2, self.config.history_turns * 2)
        if len(self.messages) <= limit + 1:
            return
        head, tail = self.messages[0], self.messages[-limit:]
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
        dropped = len(self.messages) - len(tail) - 1
        self.messages = [head, *tail]
        self.hooks.notice(f"trimmed {dropped} older messages from the history")

    # -- one assistant turn ----------------------------------------------
    def _options(self) -> dict[str, Any]:
        config = self.config
        options: dict[str, Any] = {
            "temperature": config.temperature,
            "top_p": config.top_p,
            "repeat_penalty": config.repeat_penalty,
            "num_ctx": config.num_ctx,
            "num_predict": config.num_predict,
        }
        if config.top_k > 0:
            options["top_k"] = config.top_k
        return options

    def _turn(self) -> tuple[str, str, list[tuple[str, dict[str, Any]]], TurnStats]:
        """Stream one assistant reply.

        Returns ``(content, thinking, calls, stats)``. In native mode the calls
        come from the chunks; in text mode they are parsed out of the prose.
        """
        stats = TurnStats()
        started = time.monotonic()
        tools = None
        if self.uses_tools() and self.protocol == "native":
            from .infotools import INFO_TOOL_SCHEMAS

            tools = TOOL_SCHEMAS + INFO_TOOL_SCHEMAS + self._mcp_schemas()
        think = self.config.thinking

        content: list[str] = []
        thinking: list[str] = []
        pending: dict[int, dict[str, Any]] = {}

        stream = self.client.chat_stream(
            model=self.config.model,
            messages=self.messages,
            options=self._options(),
            tools=tools,
            think=think,
            keep_alive=self.config.keep_alive,
        )
        for chunk in stream:
            # Both shapes normalize here: Ollama yields ``message`` dicts,
            # OpenAI yields ``choices[0].delta``. Everything else in this
            # method works on whatever the shape lands on.
            if "choices" in chunk:
                choice = chunk["choices"][0] if chunk["choices"] else {}
                delta = choice.get("delta") or {}
                message = {"content": delta.get("content", "") or "",
                           "thinking": delta.get("reasoning_content") or delta.get("reasoning", "") or "",
                           "tool_calls": delta.get("tool_calls") or []}
                if choice.get("finish_reason"):
                    chunk["done"] = True
                    chunk["done_reason"] = choice["finish_reason"]
            else:
                message = chunk.get("message") or {}
            if isinstance(message.get("thinking"), str) and message["thinking"]:
                thinking.append(message["thinking"])
                self.hooks.thinking(message["thinking"])
            if isinstance(message.get("content"), str) and message["content"]:
                content.append(message["content"])
                self.hooks.delta(message["content"])
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                index = int(function.get("index", len(pending)))
                slot = pending.setdefault(index, {"name": "", "arguments": {}})
                if function.get("name"):
                    slot["name"] = function["name"]
                arguments = normalize_arguments(function.get("arguments"))
                if arguments:
                    slot["arguments"].update(arguments)
            if chunk.get("done"):
                stats.done_reason = str(chunk.get("done_reason") or "")
                stats.prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                stats.output_tokens = int(chunk.get("eval_count") or 0)

        stats.seconds = time.monotonic() - started
        prose = "".join(content)
        reasoning = "".join(thinking)

        calls = [(slot["name"], slot["arguments"]) for slot in pending.values() if slot["name"]]
        extra = self._info_names() + self._mcp_names()
        if not calls and self.uses_tools() and self.protocol == "text":
            prose, calls = parse_text_calls(prose, extra_names=extra)
        elif calls:
            # A native reply that also tried the text protocol: keep the prose
            # honest by removing the block the model wrote for its own benefit.
            prose, _ = parse_text_calls(prose, extra_names=extra)
        stats.tool_calls = len(calls)

        if stats.done_reason == "length" and not calls:
            if not prose.strip() and reasoning.strip():
                self.hooks.notice(
                    f"the whole output budget went to reasoning "
                    f"({len(reasoning)} chars, hidden); raise CHAT_NUM_PREDICT "
                    f"or set CHAT_THINK=0 to get a visible answer"
                )
            else:
                self.hooks.notice(
                    f"reply hit the output ceiling (num_predict={self.config.num_predict}); "
                    f"raise CHAT_NUM_PREDICT for longer answers"
                )
        elif not prose.strip() and not calls:
            # Nothing visible and nothing asked for: say so, rather than let the
            # operator stare at a blank line wondering whether it is broken.
            self.hooks.notice(
                "the model produced no visible prose this round; the reasoning is "
                "hidden -- set CHAT_THINK=0, or /think off, to see its work"
            )
        return prose, reasoning, calls, stats

    def _append_assistant(self, prose: str, calls: list[tuple[str, dict[str, Any]]]) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": prose}
        if calls and self.protocol == "native":
            message["tool_calls"] = [
                {
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for name, arguments in calls
            ]
        elif calls:
            # Text protocol: the template has no tool branch to render a
            # ``tool_calls`` key with, so the call is recorded the way the
            # model actually made it -- as the fenced block in its own
            # content. Without it the next round sees a tool result arrive
            # after an assistant message that says nothing, and a small
            # model concludes it never made the call and re-issues it,
            # looping on the same tool until the round cap.
            blocks = [
                "```tool\n"
                + json.dumps({"name": name, "arguments": arguments})
                + "\n```"
                for name, arguments in calls
            ]
            message["content"] = "\n\n".join(part for part in [prose, *blocks] if part)
        self.messages.append(message)

    def _info_names(self) -> tuple[str, ...]:
        if self.info is None:
            return ()
        from .infotools import INFO_TOOL_SCHEMAS

        return tuple(s["function"]["name"] for s in INFO_TOOL_SCHEMAS)

    def _mcp_schemas(self) -> list[dict[str, Any]]:
        """Tool schemas for every connected MCP server (empty when there are none)."""
        if self.mcp is None or not self.uses_tools():
            return []
        try:
            return self.mcp.schemas()
        except Exception:  # noqa: BLE001 - a broken server must not stop a turn
            return []

    def _mcp_names(self) -> tuple[str, ...]:
        if self.mcp is None:
            return ()
        try:
            return self.mcp.tool_names()
        except Exception:  # noqa: BLE001
            return ()

    def _run_calls(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        """Execute each call and feed the result back as a tool message."""
        info_names = set(self._info_names())
        mcp_names = set(self._mcp_names())
        for name, arguments in calls:
            self.hooks.tool_call(name, arguments)
            self.tool_calls_made += 1
            if name in info_names and self.info is not None:
                result = self.info.call(name, arguments)
            elif name in mcp_names and self.mcp is not None:
                result = self.mcp.call(name, arguments)
            elif self.uses_tools():
                result = self.workspace.call(name, arguments)
            else:
                result = ToolResult(False, "tools are disabled in this session")
            self.hooks.tool_result(name, result)
            posted = result.text
            if len(posted) > MAX_TOOL_RESULT_CHARS:
                posted = (
                    f"{posted[:MAX_TOOL_RESULT_CHARS]}\n"
                    f"… {len(result.text) - MAX_TOOL_RESULT_CHARS} more characters "
                    f"elided; narrow the request to see them."
                )
            head = "ok" if result.ok else "error"
            if self.protocol == "native":
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": f"{summarize_call(name, arguments)} -> {head}\n{posted}",
                    }
                )
            else:
                # Text protocol: there is no tool branch to render a
                # ``role: "tool"`` message with, so the result would never
                # reach the model and it would wait for one that never comes
                # -- then conclude the call failed and re-issue it, looping
                # until the round cap. The protocol prompt already tells the
                # model results arrive "as the next message", so the result
                # is delivered in exactly that shape: a user message the
                # template can always render. Pre-rendering happens on the
                # engine side so both front ends and every test see the same
                # history.
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"TOOL RESULT for {name} -> {head}\n{posted}\n\n"
                            "Continue the task with this result. Do not "
                            "repeat the tool call."
                        ),
                    }
                )

    # -- the public entry point -------------------------------------------
    def send(self, text: str) -> str:
        """Send one user message and run to a final answer.

        Returns the last assistant prose. Tool rounds are bounded by
        ``config.max_tool_rounds`` so a model that loops on the same call cannot
        spin forever; when the bound is hit the last reply is returned with a
        notice, leaving the conversation intact.
        """
        if text.strip():
            self.messages.append({"role": "user", "content": text})
            # Keyword triggers: a skill whose declared keywords match this
            # turn rides its body for this turn only. The notice is the
            # operator's window into a doctrine firing; the message is a
            # system note so a later /clear cannot strip it from the model's
            # account of how it knew.
            from .skills import triggered_skills

            all_skills = getattr(self.hooks, "_skills_all", lambda: [])()
            for skill in triggered_skills(all_skills, text):
                self.hooks.notice(f"skill triggered: {skill.name}")
                self.messages.append(
                    {"role": "system",
                     "content": f"Skill {skill.name} applies this turn:\n\n{skill.body}"}
                )
        self._trim()
        final = ""
        tool_call_retry_used = False
        #: Index of a retry nudge currently sitting in the history. It is there
        #: for exactly one round -- the model must see it to correct itself -- and
        #: is removed once that round comes back, so the transcript never keeps a
        #: user message the operator did not send.
        nudge_index: int | None = None
        self.hooks.inference_start()
        self.hooks.turn_start(self.config.model, self.protocol)
        try:
            for round_number in range(self.config.max_tool_rounds + 1):
                try:
                    prose, _reasoning, calls, stats = self._turn()
                except OllamaError as exc:
                    # A malformed tool call is a bad *round*, not a dead turn.
                    # Small abliterated models emit truncated tool-call JSON
                    # intermittently; llama-server rejects it rather than
                    # returning it, so it arrives here as an error. Retry once
                    # with a nudge before giving up on the turn.
                    if _is_malformed_tool_call(exc) and not tool_call_retry_used:
                        tool_call_retry_used = True
                        self.hooks.notice(
                            "the model emitted a tool call whose arguments were "
                            f"not valid JSON ({_one_line(exc)}); asked it to "
                            "re-issue the call"
                        )
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "That tool call could not be parsed -- the "
                                    "arguments were not valid JSON. Re-issue it "
                                    "as one complete JSON object, or answer in "
                                    "prose instead."
                                ),
                            }
                        )
                        nudge_index = len(self.messages) - 1
                        continue
                    self.hooks.error(str(exc))
                    # Drop the unanswered user turn so the next attempt is not
                    # stacked behind a message that never got a reply. The nudge
                    # above is a user message too, so clear them all.
                    while self.messages and self.messages[-1].get("role") == "user":
                        self.messages.pop()
                    nudge_index = None
                    self._explain_error(final, exc)
                    return final
                if nudge_index is not None:
                    # The round that needed the nudge came back; retire it.
                    del self.messages[nudge_index]
                    nudge_index = None
                self._append_assistant(prose, calls)
                self.hooks.round_end(stats)
                if prose:
                    final = prose
                if not calls:
                    self.turns += 1
                    self._explain_empty(final)
                    return final
                self._run_calls(calls)
                if round_number == self.config.max_tool_rounds:
                    self.hooks.notice(
                        f"stopped after {self.config.max_tool_rounds} tool rounds; "
                        f"the model was still asking for tools"
                    )
        finally:
            # Both of these must run on every exit path: the early returns
            # above (a plain answer, a failed turn) skip the tail of the loop,
            # and an indicator left running is worse than none at all.
            self.hooks.turn_end()
            self.hooks.inference_end()
        self.turns += 1
        self._explain_empty(final)
        return final

    def _explain_empty(self, final: str) -> None:
        """A turn that shows nothing visible must say why.

        The per-round check in :meth:`_turn` only catches a single generation
        that went quiet. A turn can still end with nothing to show when every
        round was a tool round and the model never wrote prose, which is why the
        guarantee is enforced here, on the whole turn.
        """
        if final.strip():
            return
        self.hooks.notice(
            "this turn produced no visible prose -- the model spent it on tool "
            "rounds or on hidden reasoning. Set CHAT_THINK=0 to skip the "
            "reasoning, raise CHAT_NUM_PREDICT, or restate the request."
        )

    def _explain_error(self, final: str, exc: BaseException) -> None:
        """A turn that dies must still say so where the operator is looking.

        :meth:`_explain_empty` covers a turn that simply had nothing to show.
        This covers the other way a turn ends with no prose: an error. Both front
        ends render ``error``, but the never-silent contract is asserted on
        notices, and an operator watching the chat pane sees the same blank,
        unexplained turn either way.
        """
        if final.strip():
            return
        self.hooks.notice(
            "this turn ended on an error before any prose came back: "
            f"{_one_line(exc)}. The unanswered message was dropped, so send it "
            "again to retry."
        )

    # -- introspection ----------------------------------------------------
    def describe(self) -> list[tuple[str, str]]:
        """Rows for a header panel."""
        return [
            ("model", self.config.model),
            ("workspace", str(self.workspace.root)),
            ("tools", f"{len(TOOL_SCHEMAS)} ({self.protocol})" if self.uses_tools() else "off"),
            ("context", f"num_ctx={self.config.num_ctx}  num_predict={self.config.num_predict}"),
            ("turn", str(self.turns + 1)),
        ]
