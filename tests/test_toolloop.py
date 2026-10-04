#!/usr/bin/env python3
"""Text-protocol tool loop: the model must see its own past calls.

The loop reported in the field: a model on the text protocol issues a
``write_file`` call, the harness runs it and appends the tool result, but the
assistant message in the history *drops* the fenced call. The next round then
sees a result arrive after an assistant message that says nothing -- so a small
model concludes it never made the call and issues it again, forever, until the
round cap. The transcript reads as "thinking and tool calls repeat/loop".

No model is needed: this client replays the recorded behaviour -- it answers
with the same call unless the messages it is handed already contain one, which
is exactly the condition a real model's context gives it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness.chat import ChatHooks, ChatSession  # noqa: E402
from rt_harness.config import ChatConfig  # noqa: E402

fails: list[str] = []
ok_count = 0


def check(label: str, cond: bool, detail: object = "") -> None:
    global ok_count
    if cond:
        ok_count += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        fails.append(label)
        print(f"FAIL {label}  {detail}")


CALL = (
    '```tool\n{"name": "write_file", "arguments": '
    '{"path": "game.py", "content": "print(1)"}}\n```'
)
PROSE = "I created game.py for you."


class LoopingClient:
    """Answers with the same call until its history shows one, then prose.

    Two conditions a real model's context gives it, both of which the harness
    previously broke:

    * it re-issues the call until an assistant message in its history shows it
      already made one (the stripped-call loop);
    * it waits forever -- or re-issues -- unless a message in its history
      carries the tool result (the invisible ``role: "tool"`` loop).
    """

    def __init__(self) -> None:
        self.rounds = 0

    def supports_native_tools(self, model: str) -> bool:
        return False  # text protocol

    def chat_stream(self, *, model, messages, options, tools=None, think=None,
                    keep_alive="10m"):
        self.rounds += 1
        text = "\n".join(str(m.get("content", "")) for m in messages)
        seen_call = '"name": "write_file"' in text
        seen_result = "TOOL RESULT" in text or '"role": "tool"' in str(messages)
        if seen_call and seen_result:
            reply = PROSE
        else:
            reply = CALL
        yield {"message": {"content": reply}, "done": True, "done_reason": "stop"}


class Recorder(ChatHooks):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.notices: list[str] = []

    def tool_call(self, name, arguments) -> None:
        self.calls.append((name, dict(arguments)))

    def notice(self, text) -> None:
        self.notices.append(text)

    def error(self, text) -> None:
        self.notices.append(f"ERROR: {text}")


config = ChatConfig(root=Path("."), protocol="text", thinking=False, max_tool_rounds=8)
client = LoopingClient()
hooks = Recorder()
session = ChatSession(client, config, hooks=hooks)
answer = session.send("make a small game")

check("the turn ends in prose, not the round cap", answer.strip() == PROSE,
      repr(answer[:80]))
check("the tool ran exactly once", len(hooks.calls) == 1, f"{len(hooks.calls)} calls")
check("no round-cap notice fired",
      not any("tool rounds" in notice for notice in hooks.notices),
      str(hooks.notices))
check("the client answered on round 2", client.rounds == 2, f"{client.rounds} rounds")

# -- the recorded assistant message carries the call -------------------------
recorded = [
    message for message in session.messages
    if message.get("role") == "assistant"
]
check("an assistant message exists after the call round", bool(recorded))
if recorded:
    body = recorded[0].get("content", "")
    check("the call is recorded in the assistant's own content",
          '"name": "write_file"' in body, repr(body[:120]))
    check("the fence is closed in the recording", body.rstrip().endswith("```"),
          repr(body[-20:]))

# -- the operator-facing render drops the plumbing ---------------------------
from rt_harness.config import Config  # noqa: E402
from rt_harness.textual_chat import TextualChatUI  # noqa: E402


class RenderSpy(TextualChatUI):
    """Record what _write sends to the transcript, without a Textual app."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rendered: list[tuple] = []

    def _dispatch(self, method, *args) -> None:
        self.rendered.append((method, args))


full_config = Config.from_env({"API_URL": "http://127.0.0.1:1/v1",
                               "CHAT_ROOT": str(Path(__file__).parent / ".scratch")})
spy = RenderSpy(client, full_config)
spy.app = object()  # non-None: _write renders through _dispatch
spy._write(f"Here is the code.\n{CALL}\nMore prose.\n")
methods = [entry[0] for entry in spy.rendered]
check("no code block is rendered for a tool-call fence",
      "render_code" not in methods, str(spy.rendered))
check("the prose around the call still renders",
      methods.count("render_line") == 2, str(spy.rendered))

# -- a genuine code fence still renders --------------------------------------
spy2 = RenderSpy(client, full_config)
spy2.app = object()
spy2._write("```python\nprint(2)\n```\n")
check("a real code block still renders as code",
      any(entry[0] == "render_code" and entry[1][0] == "python"
          for entry in spy2.rendered),
      str(spy2.rendered))

# -- the result is recorded where the model can see it -----------------------
text_protocol_rows = [
    (str(m.get("role")), str(m.get("content", ""))) for m in session.messages
]
result_rows = [r for r in text_protocol_rows if "TOOL RESULT" in r[1]]
check("the tool result is recorded as a message", bool(result_rows))
if result_rows:
    check("on the text protocol the result rides a user message the template can render",
          result_rows[0][0] == "user", result_rows[0][0])
    check("the result carries the tool's own text",
          "created" in result_rows[0][1] or "overwrote" in result_rows[0][1]
          or "print(1)" in result_rows[0][1],
          repr(result_rows[0][1][:120]))
check("no role-tool message is left stranded on the text protocol",
      not any(m.get("role") == "tool" for m in session.messages),
      str([m.get("role") for m in session.messages]))

print()
if fails:
    print(f"{len(fails)} failure(s):")
    for name in fails:
        print(f"  - {name}")
else:
    print(f"ALL {ok_count} TOOL-LOOP CHECKS PASS")
sys.exit(1 if fails else 0)
