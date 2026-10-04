"""Drive the chat engine against the real abliterated model."""
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Scratch space lives beside these tests, never in /tmp, so a
# checkout is self-contained. Override the server or model with
# CHAT_TEST_URL / CHAT_TEST_MODEL.
SCRATCH = Path(__file__).resolve().parent / '.scratch'
sys.path.insert(0, str(REPO))

from rt_harness.chat import ChatHooks, ChatSession  # noqa: E402
from rt_harness.client import OllamaClient  # noqa: E402
from rt_harness.config import ChatConfig  # noqa: E402

MODEL = os.environ.get("CHAT_TEST_MODEL", "hf.co/mradermacher/Gemma-4-E4B-Abliterated-Uncensored-i1-GGUF:i1-Q4_K_M")
URL = os.environ.get("CHAT_TEST_URL", "http://127.0.0.1:11436")
ROOT = SCRATCH / "engine"

shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True)
target = ROOT / "app.py"
target.write_text("def add(a, b):\n    return a - b\n")

fails: list[str] = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


class Recorder(ChatHooks):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.results: list[tuple[str, bool, str]] = []
        self.deltas: list[str] = []
        self.notices: list[str] = []
        self.errors: list[str] = []
        self.stats: list = []
        self.turn_ends = 0

    def delta(self, text):
        self.deltas.append(text)

    def tool_call(self, name, arguments):
        self.calls.append((name, dict(arguments)))

    def tool_result(self, name, result):
        self.results.append((name, result.ok, result.text))

    def notice(self, text):
        self.notices.append(text)

    def error(self, text):
        self.errors.append(text)

    def round_end(self, stats):
        self.stats.append(stats)
        print(
            f"    [round] {stats.seconds:.1f}s prompt={stats.prompt_tokens} "
            f"out={stats.output_tokens} tools={stats.tool_calls} reason={stats.done_reason}"
        )

    def turn_end(self):
        self.turn_ends += 1


client = OllamaClient(URL, timeout=1800)

# -- protocol resolution without running the model -------------------------
base = ChatConfig(model=MODEL, root=ROOT, num_ctx=8192, num_predict=512)
auto = ChatSession(client, base, hooks=ChatHooks())
check("auto resolves to native for the abliterated Gemma", auto.protocol == "native", auto.protocol)
check("tools are on by default", auto.uses_tools())
check("the system prompt names the workspace root", str(ROOT) in auto.system_prompt())
check("native mode does not spell out the text protocol",
      "```tool" not in auto.system_prompt())
check("the system prompt starts the history", auto.messages[0]["role"] == "system")

import dataclasses  # noqa: E402

text_mode = ChatSession(client, dataclasses.replace(base, protocol="text"), hooks=ChatHooks())
check("text mode spells out the protocol", "```tool" in text_mode.system_prompt())
check("text mode lists every tool",
      all(name in text_mode.system_prompt() for name in ("read_file", "edit_file", "write_file",
                                                         "list_files", "search_files")))
off = ChatSession(client, dataclasses.replace(base, tools=False), hooks=ChatHooks())
check("tools can be turned off", not off.uses_tools())
check("with tools off the prompt offers no tools at all",
      "edit_file" not in off.system_prompt() and "```tool" not in off.system_prompt())
check("the header describes the session", any(k == "tools" for k, _ in off.describe()),
      str(off.describe()))

# -- the real round trip ---------------------------------------------------
rec = Recorder()
session = ChatSession(
    client,
    dataclasses.replace(base, temperature=0.2, max_tool_rounds=6),
    hooks=rec,
)
print("\n--- sending to", MODEL, "---")
started = time.time()
reply = session.send(
    "app.py defines add(a, b). It is deliberately broken: it must return the sum of "
    "a and b. Read the file, then fix it with an edit. Reply with one short "
    "sentence when the file is correct."
)
elapsed = time.time() - started
print("--- reply ---")
print(reply)
print("--- end ---\n")

check("the turn produced no engine errors", not rec.errors, str(rec.errors)[:200])
check("the model called at least one tool", bool(rec.calls), str([c[0] for c in rec.calls]))
names = [name for name, _ in rec.calls]
check("it read the file first", names and names[0] == "read_file", str(names))
check("it edited the file", "edit_file" in names, str(names))
check("every call produced a result", len(rec.results) == len(rec.calls),
      f"{len(rec.calls)} calls / {len(rec.results)} results")
check("all tool results succeeded", all(ok for _, ok, _ in rec.results),
      str([(n, t[:60]) for n, ok, t in rec.results if not ok]))
check("the file on disk is actually fixed", "return a + b" in target.read_text(),
      target.read_text().replace("\n", " | "))
check("the loop terminated without hitting the round cap",
      not any("round" in n.lower() and "cap" in n.lower() for n in rec.notices), str(rec.notices))
check("stats were reported for each generation", len(rec.stats) >= 1, f"{len(rec.stats)} stats")
check("exactly one turn_end for the whole turn", rec.turn_ends == 1, f"{rec.turn_ends}")
if rec.stats:
    stats = rec.stats[0]
    check("the turn used a prompt", stats.prompt_tokens > 0, str(stats.prompt_tokens))
    check("the turn produced output", stats.output_tokens > 0, str(stats.output_tokens))
    check("the first generation finished by stopping", stats.done_reason == "stop",
          stats.done_reason)
    check("round stats account for every tool call",
          sum(s.tool_calls for s in rec.stats) == len(rec.calls),
          f"{sum(s.tool_calls for s in rec.stats)} vs {len(rec.calls)}")
check("history holds the whole exchange", len(session.messages) >= 5,
      f"{len(session.messages)} messages: {[m['role'] for m in session.messages]}")
check("a tool message is in the history", any(m["role"] == "tool" for m in session.messages))
check("the reply is prose, not tool markup", "edit_file(" not in reply and "```" not in reply,
      repr(reply[:120]))
check("it was not instant (a real generation happened)", elapsed > 1.0, f"{elapsed:.1f}s")

# -- follow-up turn keeps working -----------------------------------------
rec2 = Recorder()
session.hooks = rec2
reply2 = session.send("Now add a docstring to add() explaining what it returns.")
check("a second turn works", not rec2.errors, str(rec2.errors)[:200])
# At num_predict=512 on a post-edit history the model sometimes spends the whole
# budget before emitting visible prose. That is allowed; being *silent* is not.
# The contract: a reply, or a notice explaining why there isn't one.
if reply2.strip():
    check("the second turn produced a reply", True, repr(reply2[:120]))
else:
    explained = [n for n in rec2.notices if "no visible prose" in n or "budget" in n]
    check(
        "an empty reply is explained rather than silent",
        bool(explained),
        f"reply='' notices={rec2.notices}",
    )
    print(f"       (turn 2 was empty and said why: {explained[0][:90]!r})" if explained else "")
check("the second turn fired exactly one turn_end", rec2.turn_ends == 1, f"{rec2.turn_ends}")
check("history grew", len(session.messages) > 5, f"{len(session.messages)} messages")

# -- reset ----------------------------------------------------------------
session.reset()
check("reset keeps only the system message", len(session.messages) == 1, str(len(session.messages)))
check("reset clears the turn counter", session.turns == 0, str(session.turns))
check("reset restores the system prompt", session.messages[0]["content"] == session.system_prompt())

print()
print(f"tool calls: {names}")
print(f"elapsed: {elapsed:.1f}s, messages: {len(session.messages)}")
if fails:
    print(f"{len(fails)} failure(s):")
    for f in fails:
        print(f"  - {f}")
else:
    print("ALL CHAT CHECKS PASS")
sys.exit(1 if fails else 0)
