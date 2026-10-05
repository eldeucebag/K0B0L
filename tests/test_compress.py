#!/usr/bin/env python3
"""Context compression: the summary becomes the history, memory both ways.

Pinned here, no model needed:
  * compress() replaces the conversation with one summary message
  * the DURABLE FACTS tail is written to the memory store (source=compress)
  * earlier memories are recalled into the compacted context
  * a failed compression turn leaves the history untouched
  * the compress_context tool drives the same path and says not to re-call
  * /compact (the command) reports; a fresh session declines politely
  * the summarization turn sees the transcript but not the system message
    (it would spend its budget restating plumbing)
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness.chat import (  # noqa: E402
    ChatHooks,
    ChatSession,
    _compress_transcript,
    _extract_facts,
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


SUMMARY = (
    "The operator asked for a memory system; the assistant built one on "
    "SQLite FTS5 with graph edges. Files: rt_harness/memory.py.\n\n"
    "DURABLE FACTS\n"
    "- The memory store is one SQLite file with FTS5.\n"
    "- Graph edges are derived at write time.\n"
    "- recall returns provenance with every row.\n"
)


class SummaryClient:
    """Answers the summarization turn with a canned summary; records input.

    Conversation turns (a history of real exchanges) answer with a short
    prose line; a turn whose user message is the compression transcript
    answers with the canned summary -- unless ``fail`` is set, in which
    case only the compression turn raises.
    """

    def __init__(self, summary=SUMMARY, fail=False):
        self.seen: list[dict] | None = None
        self.summary = summary
        self.fail = fail

    def supports_native_tools(self, model):
        return False

    def chat_stream(self, *, model, messages, options, tools=None, think=None,
                    keep_alive="10m"):
        compressing = any("CONVERSATION TO SUMMARIZE" in
                          str(m.get("content", "")) for m in messages)
        if not compressing:
            yield {"message": {"content": "understood."}, "done": True,
                    "done_reason": "stop"}
            return
        self.seen = messages
        if self.fail:
            raise OllamaError("server exploded")
        yield {"message": {"content": self.summary}, "done": True,
                "done_reason": "stop"}


class Recorder(ChatHooks):
    def __init__(self):
        self.notices: list[str] = []

    def notice(self, text):
        self.notices.append(text)

    def error(self, text):
        self.notices.append(f"ERROR: {text}")


def make_session(client, root, store_path=None):
    config = ChatConfig(root=root, protocol="text", thinking=False,
                       max_tool_rounds=4)
    session = ChatSession(client, config, hooks=Recorder())
    if store_path is not None:
        from rt_harness.memory import MemoryStore

        # Tests never touch the operator's real store: each session gets
        # its own file, the same way it gets its own workspace.
        session._memory = MemoryStore(store_path)
    return session


with TemporaryDirectory() as tmp:
    root = Path(tmp)
    store_file = root / "memory.sqlite"

    # -- a populated conversation compresses into one message ---------------
    client = SummaryClient()
    session = make_session(client, root, store_file)
    session.send("let us build a memory system")
    session.send("use SQLite FTS5, with graph edges")
    before = len(session.messages)
    report = session.compress()
    check("compress reports the fold",
          report.startswith("compressed"), report)
    check("the history is the system message plus one summary",
          len(session.messages) == 2, str(len(session.messages)))
    summary_msg = session.messages[1]
    check("the summary rides a user message the template can render",
          summary_msg.get("role") == "user", str(summary_msg.get("role")))
    check("the compressed context is framed as the whole prior conversation",
          "CONTEXT COMPRESSED" in summary_msg.get("content", "")
          and "Treat this as the entire prior conversation" in summary_msg.get("content", ""),
          summary_msg.get("content", "")[:60])
    check("the summary body itself is present",
          "SQLite FTS5" in summary_msg.get("content", ""))

    # -- memory: facts written, earlier notes recalled in --------------------
    store = session.memory
    check("the memory store accepted the compression", store is not None)
    if store is not None:
        rows = store.recall("graph edges", domain="session")
        check("durable facts were written to the store",
              len(rows) >= 1 and any("derived at write time" in r.body
                                     for r in rows),
              str([r.body for r in rows]))
        check("compression facts carry source=compress",
              all(r.source == "compress" for r in rows),
              str([r.source for r in rows]))

    # earlier memory recalled into the compacted context
    client2 = SummaryClient()
    session2 = make_session(client2, root, store_file)
    # same store (one home, one file): an earlier session's fact
    session2.remember("An earlier run pinned the border lesson.",
                      domain="session", importance=0.9)
    session2.send("we are working on the memory system again")
    session2.send("what was the earlier lesson?")
    report2 = session2.compress()
    content2 = session2.messages[1].get("content", "")
    check("compress still works with a populated store",
          report2.startswith("compressed"), report2)
    check("earlier memories are recalled into the compacted context",
          "border lesson" in content2, content2[-160:])

    # -- a failed compression leaves the history untouched -------------------
    fail_client = SummaryClient(fail=True)
    session3 = make_session(fail_client, root, store_file)
    session3.send("filler turn one")
    session3.send("filler turn two")
    snapshot = [dict(m) for m in session3.messages]
    report3 = session3.compress()
    check("a failed compression returns empty", report3 == "", repr(report3))
    check("a failed compression leaves the history untouched",
          session3.messages == snapshot)
    check("the failure is noticed, not silent",
          any("compression failed" in n for n in session3.hooks.notices),
          str(session3.hooks.notices))

    # -- nothing to compress --------------------------------------------------
    session4 = make_session(SummaryClient(), root)
    check("a fresh session declines politely",
          "nothing to compress" in session4.compress_command())

    # -- the compress_context tool -------------------------------------------
    from rt_harness.tools import Workspace  # noqa: E402

    session5 = make_session(SummaryClient(), root, store_file)
    session5.send("populate one")
    session5.send("populate two")
    ws = Workspace(root, allow_exec=False, ui=session5)
    result = ws.call("compress_context", {})
    check("the tool compresses through the workspace surface",
          result.ok and result.text.startswith("compressed"), result.text[:80])
    check("the tool says not to re-call",
          "without calling this again" in result.text, result.text[-80:])
    check("the tool-path history is one summary message",
          len(session5.messages) == 2
          and "Your compress_context tool call just completed"
          in session5.messages[1].get("content", ""),
          str(len(session5.messages)))

    ws_fresh = Workspace(root, allow_exec=False,
                         ui=make_session(SummaryClient(), root))
    result = ws_fresh.call("compress_context", {})
    check("the tool declines a fresh session",
          result.ok and "nothing to compress" in result.text, result.text[:80])

    # -- the summarization turn sees the transcript, not the plumbing -------
    client6 = SummaryClient()
    session6 = make_session(client6, root)
    session6.send("alpha turn")
    session6.send("beta turn")
    session6.compress()
    seen = client6.seen or []
    check("the summarizer gets a two-message turn",
          len(seen) == 2, str(len(seen)))
    if seen:
        check("the summarizer's system message is the compression prompt",
              seen[0].get("role") == "system"
              and "summarizing" in seen[0].get("content", ""),
              seen[0].get("content", "")[:40])
        body = seen[1].get("content", "")
        check("the transcript render labels the speakers",
              "USER: alpha turn" in body and "ASSISTANT:" in body,
              body[:60])
        check("the session's own system message is not in the transcript",
              "Your working directory" not in body
              and "soul" not in body.lower(),
              body[:40])

    # -- the helpers ----------------------------------------------------------
    facts = _extract_facts(SUMMARY)
    check("the fact extractor takes only the facts block",
          facts == ["The memory store is one SQLite file with FTS5.",
                    "Graph edges are derived at write time.",
                    "recall returns provenance with every row."],
          str(facts))
    check("the fact extractor caps at eight",
          len(_extract_facts("DURABLE FACTS\n" + "\n".join(
              f"- fact {i}" for i in range(20)))) == 8)
    transcript = _compress_transcript([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "working",
         "tool_calls": [{"function": {"name": "read_file"}}]},
    ])
    check("the transcript render notes native tool calls",
          "[made tool calls: read_file]" in transcript, transcript[-60:])
    transcript2 = _compress_transcript([
        {"role": "user", "content": "TOOL RESULT for read_file -> ok"},
    ])
    check("text-protocol results are labelled TOOL",
          "TOOL: TOOL RESULT" in transcript2, transcript2[:60])

    # -- the command path ------------------------------------------------------
    session7 = make_session(SummaryClient(), root)
    session7.send("one")
    session7.send("two")
    check("compress_command reports the same fold",
          session7.compress_command().startswith("compressed"),
          session7.compress_command())

print()
if fails:
    print(f"{len(fails)} failure(s):")
    for name in fails:
        print(f"  - {name}")
else:
    print(f"ALL {ok_count} COMPRESSION CHECKS PASS")
sys.exit(1 if fails else 0)
