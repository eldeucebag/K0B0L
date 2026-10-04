#!/usr/bin/env python3
"""Recovery from a malformed tool call, and the never-silent contract on errors.

No model is needed: a fake client raises the exact error llama-server produces
when a model emits tool-call arguments that are not valid JSON. That failure was
seen for real -- ``tests/test_engine.py`` went red with

    FAIL a second turn works
      ['llama-server returned invalid tool call arguments for "write_file":
        unexpected end of JSON input']

and the turn died with an empty reply and no notice. A small abliterated model
emits truncated tool-call JSON intermittently, so the harness has to treat it as
a bad round and retry, rather than as a dead turn.

Run:  python3 tests/test_recovery.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness.chat import (  # noqa: E402
    ChatHooks,
    ChatSession,
    _is_malformed_tool_call,
    _one_line,
)
from rt_harness.client import OllamaError  # noqa: E402
from rt_harness.config import ChatConfig  # noqa: E402

ok_count = 0
fails: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    global ok_count
    if cond:
        ok_count += 1
        print(f"ok   {label}" + (f"  {detail}" if detail else ""))
    else:
        fails.append(label)
        print(f"FAIL {label}  {detail}")


#: Verbatim from the real failure. Do not paraphrase this string: it is the
#: contract with llama-server's wording, and the matcher is substring-based.
REAL_ERROR = (
    'llama-server returned invalid tool call arguments for "write_file": '
    "unexpected end of JSON input"
)


class Recorder(ChatHooks):
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.errors: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    def notice(self, text): self.notices.append(text)
    def error(self, text): self.errors.append(text)
    def tool_call(self, name, arguments): self.calls.append((name, dict(arguments)))


class FakeClient:
    """A client that fails a set number of rounds, then answers.

    ``chat_stream`` is a generator on purpose: the real one raises mid-iteration
    when Ollama reports an error as an NDJSON chunk, so an implementation that
    only guards the call site would pass here while still failing in the field.
    """

    def __init__(self, errors: int = 1, prose: str = "Done.") -> None:
        self.errors = errors
        self.prose = prose
        self.rounds = 0

    def supports_native_tools(self, model: str) -> bool:
        return True

    def chat_stream(self, *, model, messages, options, tools=None, think=None,
                    keep_alive="10m"):
        self.rounds += 1
        if self.rounds <= self.errors:
            raise OllamaError(REAL_ERROR)
        yield {"message": {"content": self.prose}, "done": True, "done_reason": "stop"}


def session(client, root: Path) -> ChatSession:
    config = ChatConfig(root=root, protocol="native", thinking=False)
    return ChatSession(client, config)


# -- the matcher -------------------------------------------------------------
check("the real llama-server error is recognised", _is_malformed_tool_call(OllamaError(REAL_ERROR)))
check(
    "an unrelated transport failure is not",
    not _is_malformed_tool_call(OllamaError("connection refused")),
)
check(
    "a truncated-JSON error is recognised by its own wording",
    _is_malformed_tool_call(OllamaError("unexpected end of JSON input")),
)
check("a long error is collapsed to one line", "\n" not in _one_line(OllamaError("a\nb")))

# -- recovery ----------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "f.py").write_text("x = 1\n", encoding="utf-8")
    rec = Recorder()
    client = FakeClient(errors=1)
    s = session(client, root)
    s.hooks = rec
    reply = s.send("fix f.py")

    check("the malformed round did not end the turn", bool(reply.strip()), repr(reply))
    check("the turn was retried exactly once", client.rounds == 2, f"{client.rounds} rounds")
    check("no error was raised at the operator", not rec.errors, str(rec.errors)[:120])
    check(
        "the retry was explained",
        any("not valid JSON" in n for n in rec.notices),
        str(rec.notices)[:120],
    )
    check("the reply is the retry's prose", reply.strip() == "Done.", repr(reply))
    check(
        "the conversation holds the user turn with no dangling duplicate",
        [m["role"] for m in s.messages] == ["system", "user", "assistant"],
        str([m["role"] for m in s.messages]),
    )

# -- giving up is not silence ------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    rec = Recorder()
    client = FakeClient(errors=99)  # never recovers
    s = session(client, root)
    s.hooks = rec
    reply = s.send("fix f.py")

    check("a turn that cannot recover returns no prose", reply.strip() == "", repr(reply))
    check("it gave up after a single retry", client.rounds == 2, f"{client.rounds} rounds")
    check("the failure was reported as an error", len(rec.errors) == 1, str(rec.errors)[:120])
    check(
        "and it was ALSO explained as a notice, never silent",
        any("ended on an error" in n for n in rec.notices),
        str(rec.notices)[:120],
    )
    check(
        "no unanswered user message was left behind",
        [m["role"] for m in s.messages] == ["system"],
        str([m["role"] for m in s.messages]),
    )

# -- a non-malformed error is not retried ------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    rec = Recorder()

    class Dead(FakeClient):
        def chat_stream(self, **kwargs):
            self.rounds += 1
            raise OllamaError("connection refused")
            yield  # pragma: no cover - generator marker

    client = Dead()
    s = session(client, Path(tmp))
    s.hooks = rec
    reply = s.send("hello")

    check("a dead server is not retried", client.rounds == 1, f"{client.rounds} rounds")
    check("its error still surfaces", len(rec.errors) == 1, str(rec.errors)[:120])
    check(
        "and it is still explained as a notice",
        any("ended on an error" in n for n in rec.notices),
        str(rec.notices)[:120],
    )

print()
if fails:
    print(f"{len(fails)} failure(s):")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"{ok_count} checks passed")
