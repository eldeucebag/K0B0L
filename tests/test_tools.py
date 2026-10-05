"""Exercise the confined file workspace with no model in the loop."""
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Scratch space lives beside these tests, never in /tmp, so a
# checkout is self-contained. Override the server or model with
# CHAT_TEST_URL / CHAT_TEST_MODEL.
SCRATCH = Path(__file__).resolve().parent / '.scratch'
sys.path.insert(0, str(REPO))

from rt_harness.tools import (  # noqa: E402
    TOOL_NAMES,
    TOOL_SCHEMAS,
    TOOL_VERBOSITY,
    ToolError,
    Workspace,
    normalize_arguments,
    parse_text_calls,
    summarize_call,
    text_protocol_prompt,
)

ROOT = SCRATCH / "tools"
shutil.rmtree(ROOT, ignore_errors=True)
ROOT.mkdir(parents=True)
(ROOT / "app.py").write_text(
    "def greet(name):\n"
    "    return 'hello ' + name\n"
    "\n"
    "print(greet('world'))\n"
    "print('done')\n"
)
(ROOT / "notes.txt").write_text("alpha\nbeta\ngamma\n")
(ROOT / "src").mkdir()
(ROOT / "src" / "util.py").write_text("VALUE = 1\n")

ws = Workspace(ROOT)
fails: list[str] = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def raises(label, fn):
    try:
        fn()
    except ToolError as exc:
        check(label, True, f"-> {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"-> wrong exception {type(exc).__name__}: {exc}")
        return
    check(label, False, "-> no error raised")


# -- schemas ---------------------------------------------------------------
names = sorted(s["function"]["name"] for s in TOOL_SCHEMAS)
check("schemas expose exactly the thirteen file tools",
      names == ["ask_user_choice", "compress_context", "connect_memories", "edit_file", "expand_memory", "list_files", "load_skill", "read_file", "recall", "remember", "run_script", "search_files", "write_file"], str(names))
check("TOOL_NAMES matches the schemas", tuple(names) == tuple(sorted(TOOL_NAMES)))
check("no shell/exec tool exists", not any("shell" in n or "exec" in n or "bash" in n for n in names))
check("verbosity levels", TOOL_VERBOSITY == ("off", "summary", "full"))
check("text protocol prompt lists every tool",
      all(n in text_protocol_prompt() for n in TOOL_NAMES))

# -- read ------------------------------------------------------------------
res = ws.read_file("app.py")
check("read_file succeeds", res.ok)
check("read_file numbers lines", "1|def greet(name):" in res.text, res.text.splitlines()[1])
check("read_file shows the whole file", res.text.rstrip().endswith("5|print('done')"))
check("read_file header names the file and range", "lines 1-5 of 5" in res.text, res.text.splitlines()[0])
out = ws.read_file("app.py", start=2, limit=2).text
check("read_file honours start/limit", "2|    return 'hello ' + name" in out and "print(greet" not in out,
      out.replace("\n", " | "))

# -- confinement -----------------------------------------------------------
raises("relative escape is refused", lambda: ws.read_file("../secrets"))
raises("absolute path outside root is refused", lambda: ws.read_file("/etc/passwd"))
raises("nested escape on write is refused",
       lambda: ws.write_file("src/../../x", "no"))
raises("missing file is an error, not an empty read", lambda: ws.read_file("nope.py"))
raises("reading a directory is refused", lambda: ws.read_file("src"))
raises("start past end of file is refused", lambda: ws.read_file("notes.txt", start=99))
check("nothing was written outside the root", not (SCRATCH / "escaped").exists())

# -- edit ------------------------------------------------------------------
res = ws.edit_file("app.py", "return 'hello ' + name", "return f'hello {name.upper()}'")
check("edit_file reports the change", res.ok and "app.py" in res.text, res.text.strip()[:80])
check("edit_file says how many replacements", "1 replacement(s)" in res.text, res.text.strip()[:80])
check("edit_file wrote to disk", "f'hello {name.upper()}'" in (ROOT / "app.py").read_text())
check("edit_file left the rest of the file alone",
      (ROOT / "app.py").read_text().count("def greet(name):") == 1)
check("edit_file reports the line number", "line 2" in res.text, res.text.strip()[:80])

raises("edit with absent old_string is refused",
       lambda: ws.edit_file("app.py", "zzz-not-here", "y"))
raises("ambiguous edit is refused",
       lambda: ws.edit_file("notes.txt", "a", "A"))
raises("no-op edit is refused", lambda: ws.edit_file("notes.txt", "alpha", "alpha"))

res = ws.edit_file("notes.txt", "a", "@", replace_all=True)
check("replace_all edits every occurrence", (ROOT / "notes.txt").read_text().count("@") == 5,
      (ROOT / "notes.txt").read_text().replace("\n", " "))
check("replace_all reports the count", "5 replacement(s)" in res.text, res.text.strip()[:80])

# -- write -----------------------------------------------------------------
res = ws.write_file("new/thing.py", "x = 1\n")
check("write_file creates parent directories", (ROOT / "new" / "thing.py").read_text() == "x = 1\n")
check("write_file reports creation", "created" in res.text.lower(), res.text.strip()[:60])
res = ws.write_file("new/thing.py", "x = 2\n")
check("write_file overwrites and says so", "overwrote" in res.text.lower(), res.text.strip()[:60])

# -- list / search ---------------------------------------------------------
out = ws.list_files().text
check("list_files marks directories", "src/" in out, out.replace("\n", " ")[:90])
out = ws.list_files(pattern="*.py").text
check("list_files honours a pattern", "app.py" in out and "notes.txt" not in out,
      out.replace("\n", " ")[:90])
out = ws.search_files(r"VALUE", glob="*.py").text
check("search_files finds a match with file:line", "util.py:1: VALUE = 1" in out,
      out.replace("\n", " ")[:90])
out = ws.search_files("greet").text
check("search_files scans the whole tree by default", "app.py" in out, out.replace("\n", " ")[:90])
out = ws.search_files("ZZZ_nothing").text
check("search_files reports emptiness without erroring", "no matches" in out, out.strip()[:70])
raises("invalid regex is refused", lambda: ws.search_files("("))

# -- dispatcher ------------------------------------------------------------
res = ws.call("read_file", {"path": "nope.py"})
check("a tool failure comes back as ok=False, not an exception", not res.ok, res.text.strip()[:60])
res = ws.call("no_such_tool", {})
check("unknown tool is reported", not res.ok and "unknown tool" in res.text, res.text.strip()[:60])
res = ws.call("read_file", {"wrong_arg": 1})
check("missing required argument is reported", not res.ok and "bad arguments" in res.text,
      res.text.strip()[:60])
res = ws.call("read_file", {"path": 42})
check("a non-string path is reported", not res.ok, res.text.strip()[:60])
res = ws.call("read_file", "not-a-dict")
check("non-object arguments are reported", not res.ok, res.text.strip()[:60])
check("summarize_call renders arguments",
      summarize_call("read_file", {"path": "app.py"}) == "read_file(path=app.py)")
check("summarize_call clips long values", "…" in summarize_call("write_file", {"content": "z" * 200}))
check("normalize_arguments accepts an object", normalize_arguments({"a": 1}) == {"a": 1})
check("normalize_arguments parses a JSON string", normalize_arguments('{"a": 1}') == {"a": 1})
check("normalize_arguments survives junk", normalize_arguments("not json") == {})
check("normalize_arguments survives None", normalize_arguments(None) == {})

# -- text protocol ---------------------------------------------------------
text = (
    "Let me look.\n\n"
    "```tool\n"
    '{"name": "read_file", "arguments": {"path": "notes.txt"}}\n'
    "```\n"
    "Reading it now."
)
prose, calls = parse_text_calls(text)
check("text protocol finds the call", len(calls) == 1 and calls[0][0] == "read_file", str(calls))
check("text protocol parses the arguments", calls[0][1] == {"path": "notes.txt"})
check("text protocol strips the block from the prose",
      "```" not in prose and "read_file" not in prose, repr(prose[:60]))
check("text protocol keeps the surrounding prose", "Let me look." in prose and "Reading it now." in prose)
prose, calls = parse_text_calls('<tool_call>{"name": "list_files", "arguments": {}}</tool_call>')
check("tag form is recognised", len(calls) == 1 and calls[0][0] == "list_files", str(calls))
_, calls = parse_text_calls('```tool\n{"name": "read_file", "args": "{\\"path\\": \\"a\\"}"}\n```')
check("string-encoded arguments are parsed", calls and calls[0][1] == {"path": "a"}, str(calls))
check("no calls in ordinary prose", parse_text_calls("nothing here")[1] == [])
prose, calls = parse_text_calls("```tool\n{oh no}\n```")
check("malformed json yields no calls, not a crash", calls == [])
check("a malformed block is left visible in the prose", "oh no" in prose, repr(prose[:40]))
_, calls = parse_text_calls('{"name": "rm_rf", "arguments": {}}')
check("a call to a non-existent tool is ignored", calls == [], str(calls))
_, calls = parse_text_calls('{"name": "write_file", "arguments": {"path": "a", "content": "b"}}')
check("bare (unfenced) JSON is not executed as a tool call", calls == [], str(calls))

print()
if fails:
    print(f"{len(fails)} failure(s):")
    for f in fails:
        print(f"  - {f}")
else:
    print("ALL TOOL CHECKS PASS")
sys.exit(1 if fails else 0)
