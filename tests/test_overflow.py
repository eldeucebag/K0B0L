#!/usr/bin/env python3
"""Overflow recovery: a context-window refusal compresses and retries.

The live failure: a big tool result (e.g. read_file on a large module) tips
the prompt past the server's window; llama-server rejects with HTTP 400
exceed_context_size_error *before allocating anything*, and the old engine
dropped the turn entirely -- the model learned to read files 120 chars at
a time rather than lose turns.

Pinned here, no model needed:
  * the marker set matches the real server error (captured from the live
    llama-server, 2026-10-05: 'exceed_context_size_error',
    'request (200016 tokens) exceeds the available context size (102400 ...)')
  * an overflow mid-turn compresses once and retries; the turn survives
  * a second overflow after compression falls to the honest error path
  * a non-overflow error (dead server) never triggers compression
  * the compressed history replaces the oversized one before the retry
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness.chat import (  # noqa: E402
    ChatHooks,
    ChatSession,
    _is_context_overflow,
)
from rt_harness.client import OllamaError  # noqa: E402
from rt_harness.config import ChatConfig  # noqa: E402

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


# -- the marker set matches the real server error ----------------------------
REAL_ERROR = (
    'HTTP 400 from /v1/chat/completions: {"error":{"code":400,'
    '"message":"request (200016 tokens) exceeds the available context '
    'size (102400 tokens), try increasing it",'
    '"type":"exceed_context_size_error","n_prompt_tokens":200016,'
    '"n_ctx":102400}}'
)
check("the real captured server error is recognized",
      _is_context_overflow(OllamaError(REAL_ERROR)))
check("a connection error is not an overflow",
      not _is_context_overflow(
          OllamaError("cannot reach http://127.0.0.1:9/v1/models: refused")))
check("a malformed tool call is not an overflow",
      not _is_context_overflow(
          OllamaError("HTTP 400: invalid tool call: unexpected end of json")))


SUMMARY = (
    "The operator asked to read rt_harness/memory.py; the assistant began "
    "with read_file.\n\nDURABLE FACTS\n"
    "- The memory store lives at ~/.k0b0l-memory.sqlite.\n"
)


class OverflowClient:
    """Refuses prompts that look like the oversized history; summarizes on demand.

    - conversation turns: overflow until the history contains the compressed
      marker, then answer in prose (the retry succeeds)
    - compression turns (CONVERSATION TO SUMMARIZE): return the canned summary
    - optionally, always overflow (for the double-overflow path)
    """

    def __init__(self, *, always_overflow=False):
        self.always_overflow = always_overflow
        self.turns = 0
        self.summaries = 0

    def supports_native_tools(self, model):
        return False

    def chat_stream(self, *, model, messages, options, tools=None, think=None,
                    keep_alive="10m"):
        self.turns += 1
        text = "\n".join(str(m.get("content", "")) for m in messages)
        if "CONVERSATION TO SUMMARIZE" in text:
            self.summaries += 1
            if self.always_overflow:
                raise OllamaError(REAL_ERROR)
            yield {"message": {"content": SUMMARY}, "done": True,
                    "done_reason": "stop"}
            return
        if "CONTEXT COMPRESSED" not in text:
            raise OllamaError(REAL_ERROR)
        yield {"message": {"content": "resumed: the file read can continue"},
                "done": True, "done_reason": "stop"}


class Recorder(ChatHooks):
    def __init__(self):
        self.notices: list[str] = []

    def notice(self, text):
        self.notices.append(text)

    def error(self, text):
        self.notices.append(f"ERROR: {text}")


with TemporaryDirectory() as tmp:
    root = Path(tmp)
    config = ChatConfig(root=root, protocol="text", thinking=False,
                        max_tool_rounds=4)

    # -- an overflow mid-turn compresses once and the turn survives ---------
    client = OverflowClient()
    session = ChatSession(client, config, hooks=Recorder())
    session.send("please read rt_harness/memory.py")
    # seed a heavy history, then force the overflow on the next turn
    session.messages.append({"role": "assistant", "content": "x" * 100})
    session.messages.append({"role": "user", "content": "continue"})
    answer = session.send("continue reading")
    check("the overflow turn survives via compression",
          answer.startswith("resumed"), repr(answer[:60]))
    check("the compression ran exactly once",
          client.summaries == 1, str(client.summaries))
    check("the oversized history was replaced by the summary",
          any("CONTEXT COMPRESSED" in str(m.get("content", ""))
              for m in session.messages))
    check("the overflow was explained to the operator",
          any("exceeded the server's context window" in n
              for n in session.hooks.notices),
          str(session.hooks.notices[:3]))

    # -- a second overflow after compression falls to the honest path --------
    client2 = OverflowClient(always_overflow=True)
    session2 = ChatSession(client2, config, hooks=Recorder())
    session2.send("read the file")
    session2.messages.append({"role": "user", "content": "more"})
    answer2 = session2.send("more")
    check("a compression that cannot fit is reported, not looped",
          answer2 == ""
          and any("compression failed" in n or "ERROR" in n
                  for n in session2.hooks.notices),
          str(session2.hooks.notices[:4]))
    check("the double-overflow path did not spin",
          client2.summaries <= 1, str(client2.summaries))

    # -- a non-overflow error never triggers compression ---------------------
    class DeadClient(OverflowClient):
        def chat_stream(self, **kwargs):
            raise OllamaError("cannot reach http://127.0.0.1:9/v1/models: "
                              "Connection refused")

    dead = DeadClient()
    session3 = ChatSession(dead, config, hooks=Recorder())
    session3.send("hello")
    check("a dead server is an error, not a compression",
          not any("compress" in n for n in session3.hooks.notices),
          str(session3.hooks.notices[:3]))

print()
if fails:
    print(f"{len(fails)} failure(s):")
    for name in fails:
        print(f"  - {name}")
else:
    print(f"ALL {ok_count} OVERFLOW-RECOVERY CHECKS PASS")
sys.exit(1 if fails else 0)
