#!/usr/bin/env python3
"""Self-memorization: the model drives remember/recall, not the harness.

The mechanic under test is behavioural: the model decides when to write and
when to look, entirely through tool calls. So this suite plays the model with
a scripted client -- session one *calls the remember tool* mid-turn, session
two (a fresh ChatSession, a different model name) *calls the recall tool*, and
the recalled row must carry session one's provenance. Nothing is injected by
the harness at any point: if the tools were not wired, both calls would fail.

Also pinned: recall happens only because the model asked -- send() alone must
not touch the store.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness.chat import ChatHooks, ChatSession  # noqa: E402
from rt_harness.config import ChatConfig  # noqa: E402
from rt_harness.tools import Workspace  # noqa: E402

OK = 0
FAIL = 0


def check(label, ok, detail=""):
    global OK, FAIL
    if ok:
        OK += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"FAIL {label}  {detail}")


class Recorder(ChatHooks):
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.results: list[tuple[str, object]] = []

    def tool_call(self, name, arguments) -> None:
        self.calls.append((name, dict(arguments)))

    def tool_result(self, name, result) -> None:
        self.results.append((name, result))

    def notice(self, text) -> None:
        self.notices.append(text)

    def error(self, text) -> None:
        self.notices.append(f"ERROR: {text}")


CALL_REMEMBER = (
    '```tool\n{"name": "remember", "arguments": {'
    '"body": "The PrismML router evicts a model when a different one is requested.", '
    '"domain": "serving", "importance": 0.8}}\n```'
)
PROSE = "Noted to memory."


class ScriptedClient:
    """Replies from a script: tool call first, then prose."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.rounds = 0

    def supports_native_tools(self, model: str) -> bool:
        return False  # text protocol: the call is parsed out of the reply

    def chat_stream(self, *, model, messages, options, tools=None, think=None,
                    keep_alive="10m"):
        self.rounds += 1
        reply = self.replies[min(self.rounds - 1, len(self.replies) - 1)]
        yield {"message": {"content": reply}, "done": True, "done_reason": "stop"}


work = Path(tempfile.mkdtemp(prefix="k0b0l-selfmem-"))

# Redirect the store to the scratch dir so the suite is hermetic.
import os  # noqa: E402

os.environ["K0B0L_MEMORY"] = str(work / "memory.sqlite")

# -- session one: the model remembers, through the tool --------------------
import rt_harness.memory as memory_module  # noqa: E402


class HermeticStore(memory_module.MemoryStore):
    @classmethod
    def default_path(cls) -> Path:
        return Path(os.environ["K0B0L_MEMORY"])


original_init = memory_module.MemoryStore.__init__


def patched_init(self, path=None):
    original_init(self, path or os.environ["K0B0L_MEMORY"])


memory_module.MemoryStore.__init__ = patched_init

hooks1 = Recorder()
config1 = ChatConfig(root=work, protocol="text", thinking=False)
session1 = ChatSession(ScriptedClient([CALL_REMEMBER, PROSE]), config1, hooks=hooks1)
answer1 = session1.send("note this fact about the router for future sessions")
check("session one ends in prose", answer1.strip() == PROSE, repr(answer1[:60]))
check("the model made exactly one remember call",
      [n for n, _ in hooks1.calls] == ["remember"],
      str([n for n, _ in hooks1.calls]))
check("the remember tool reported success",
      any(n == "remember" and r.ok for n, r in hooks1.results),
      str([(n, getattr(r, "ok", None)) for n, r in hooks1.results]))

# -- session two: a different model recalls, through the tool ---------------
CALL_RECALL = (
    '```tool\n{"name": "recall", "arguments": {"query": "router eviction", '
    '"domain": "serving"}}\n```'
)
hooks2 = Recorder()
config2 = ChatConfig(root=work, protocol="text", thinking=False,
                     model="DeepSeek-14B")
session2 = ChatSession(ScriptedClient([CALL_RECALL, PROSE]), config2, hooks=hooks2)
answer2 = session2.send("what do we know about model eviction?")
check("session two ends in prose", answer2.strip() == PROSE, repr(answer2[:60]))

recalled = [r for n, r in hooks2.results if n == "recall"]
check("the recall tool ran", bool(recalled), str(hooks2.calls))
if recalled:
    text = recalled[0].text
    check("the different model got the fact back",
          "evicts a model" in text, text[:120])
    check("the provenance names the writing model",
          "by Gemma" in text, text[-120:])
    check("the recall carries a domain tag", "[serving]" in text, text[:160])

# -- nothing is recalled unless the model asks ------------------------------
hooks3 = Recorder()
session3 = ChatSession(ScriptedClient([PROSE]), ChatConfig(root=work, protocol="text",
                                                           thinking=False),
                       hooks=hooks3)
session3.send("hello, no tools needed")
check("a turn with no recall call touches no memory",
      [n for n, _ in hooks3.calls] == [], str(hooks3.calls))

# -- the system prompt teaches the mechanic ---------------------------------
prompt = session2.system_prompt()
check("the system prompt names remember", "remember(body" in prompt, prompt[-260:])
check("the system prompt names recall", "recall(query" in prompt, "")

# -- bare workspace refuses cleanly ------------------------------------------
bare = Workspace(work)
result = bare.call("remember", {"body": "x"})
check("remember without a session refuses, not crashes",
      not result.ok and "no session" in result.text, result.text)

print(f"\n{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
