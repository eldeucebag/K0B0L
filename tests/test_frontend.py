"""Front-end tests: does tool output land in the chat rather than the shell?"""
import io
import os
import shutil
import subprocess
import sys
import contextlib
from pathlib import Path

# Scratch space lives beside these tests, never in /tmp, so a
# checkout is self-contained. Override the server or model with
# CHAT_TEST_URL / CHAT_TEST_MODEL.
SCRATCH = Path(__file__).resolve().parent / '.scratch'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rt_harness.client import OllamaClient  # noqa: E402
from rt_harness.config import Config  # noqa: E402
from rt_harness.tui import PlainChat  # noqa: E402

URL = os.environ.get("CHAT_TEST_URL", "http://127.0.0.1:11436")
MODEL = os.environ.get("CHAT_TEST_MODEL", "hf.co/mradermacher/Gemma-4-E4B-Abliterated-Uncensored-i1-GGUF:i1-Q4_K_M")
ROOT = SCRATCH / "ui"
REPO = Path(__file__).resolve().parents[1]

fails: list[str] = []
def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(label)

shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True)
(ROOT / "greeting.py").write_text(
    "def greet(name):\n    return f'hi {name}'\n\n\nprint(greet('world'))\n"
)

config = Config.from_env({
    **os.environ,
    "OLLAMA_URL": URL,
    "CHAT_MODEL": MODEL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_CTX": "8192",
    "CHAT_NUM_PREDICT": "400",
    "CHAT_PROTOCOL": "native",
})
client = OllamaClient(URL, timeout=1800)

# -- part A: the chat renderer owns tool output, not the shell ----------------
script = [
    "/help",
    "/roots",                      # unknown command
    "/read greeting.py",
    "Read greeting.py and tell me, in one short sentence, what it prints.",
    "/history",
    "/exit",
]
feed = iter(script)
def input_fn(prompt):
    try:
        return next(feed)
    except StopIteration:
        raise EOFError from None

chat_out, shell_out = io.StringIO(), io.StringIO()
ui = PlainChat(client, config, tool_output="summary", show_thinking=False, out=chat_out)
with contextlib.redirect_stdout(shell_out):
    rc = ui.loop(input_fn)
chat_text = chat_out.getvalue()

check("the chat loop exited cleanly on /exit", rc == 0, str(rc))
check("the chat renderer wrote nothing to the shell", shell_out.getvalue() == "",
      repr(shell_out.getvalue()[:200]))
check("/help lists the commands", "/tools" in chat_text and "/exit" in chat_text)
check("an unknown command is reported in the chat", "unknown command" in chat_text)
check("/read renders file content into the chat",
      "def greet(name):" in chat_text and "1|def greet(name):" in chat_text)
check("the model asked for a tool, shown in the chat", "⚙ read_file" in chat_text,
      chat_text[-400:])
check("the tool result body appears in the chat", "return f'hi {name}'" in chat_text)
check("the tool result is labelled with its status", "read_file → ok" in chat_text)
check("/history reports the session", "tool calls" in chat_text)
check("the model's prose appeared", "hi world" in chat_text or "greet" in chat_text.lower(),
      chat_text[-600:])

# -- part B: --tools off suppresses the body, not the call -------------------
script2 = ["/tools off", "What does greeting.py contain? Answer in one word.", "/exit"]
feed2 = iter(script2)
def input_fn2(prompt):
    try:
        return next(feed2)
    except StopIteration:
        raise EOFError from None

out2 = io.StringIO()
ui2 = PlainChat(client, config, tool_output="summary", show_thinking=False, out=out2)
ui2.loop(input_fn2)
text2 = out2.getvalue()
check("/tools off is acknowledged", "tool output → chat: off" in text2)
check("with output off the call is still shown", "⚙ read_file" in text2, text2[-400:])
if "read_file" in text2:
    after = text2.split("read_file")[-1]
    check("with output off the file body is not echoed", "def greet" not in after,
          after[:200])

# -- part C: the real CLI, piped --------------------------------------------
env = {
    **os.environ,
    "OLLAMA_URL": URL,
    "CHAT_MODEL": MODEL,
    "CHAT_ROOT": str(ROOT),
    "CHAT_NUM_CTX": "8192",
    "CHAT_NUM_PREDICT": "300",
    "CHAT_TOOL_OUTPUT": "full",
}
proc = subprocess.run(
    [sys.executable, "thinlizzy.py", "--chat", "--chat-plain"],
    cwd=REPO, env=env, input="List the files in the workspace.\n/exit\n",
    capture_output=True, text=True, timeout=900,
)
check("pipelines: --chat --chat-plain exits 0", proc.returncode == 0,
      f"rc={proc.returncode} err={proc.stderr[-400:]}")
check("pipelines: no traceback", "Traceback" not in proc.stderr, proc.stderr[-400:])
combined = proc.stdout + proc.stderr
check("pipelines: the model used a tool", "⚙" in combined, combined[-500:])
check("pipelines: tool output marker rendered", "list_files" in combined or "read_file" in combined,
      combined[-500:])

print()
print(f"A: {len(chat_text)} chars of chat, shell bytes: {len(shell_out.getvalue())}")
if fails:
    print(f"{len(fails)} failure(s):")
    for f in fails:
        print(f"  - {f}")
else:
    print("ALL TUI CHECKS PASS")
sys.exit(1 if fails else 0)
