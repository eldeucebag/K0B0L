"""Docs tests: are the capability documents present, fetchable, and true?

The first half is plumbing -- the index, the lookup, the info tool. The second
half is the part that rots: every `/command` and every ENV_VAR the documents
name has to exist in the code. A document that promises a command that was
renamed is worse than no document, because the model will answer from it.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness.config import ChatConfig, Config  # noqa: E402
from rt_harness.docs import (  # noqa: E402
    DEFAULT_DOCS_DIR,
    DOCS_INTRO,
    docs_dir,
    docs_index,
    list_docs,
    read_doc,
)
from rt_harness.infotools import INFO_TOOL_SCHEMAS, InfoTools  # noqa: E402

failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}{': ' + detail if detail else ''}")
        failed.append(name)


config = ChatConfig()
docs = list_docs(config)

# -- the shipped set ------------------------------------------------------
check("the docs directory is in the repo", DEFAULT_DOCS_DIR == REPO / "docs")
check("the docs directory has documents", len(docs) >= 8, f"{len(docs)} found")
check(
    "README is the human index, not a topic",
    "readme" not in {doc.name.lower() for doc in docs},
)
check(
    "every document has a title and a summary",
    all(doc.title and doc.summary for doc in docs),
    ", ".join(doc.name for doc in docs if not (doc.title and doc.summary)),
)
check(
    "no title is just the filename",
    all(doc.title != doc.name for doc in docs),
    ", ".join(doc.name for doc in docs if doc.title == doc.name),
)

# -- the index the model is given ----------------------------------------
index = docs_index(config)
check("the index explains what to do with it", DOCS_INTRO in index)
check("the index lists every topic", all(f"- {doc.name}:" in index for doc in docs))
check(
    "the index carries summaries, not contents",
    len(index) < sum(len(read_doc(config, doc.name) or "") for doc in docs),
)

# -- fetching -------------------------------------------------------------
runs = read_doc(config, "runs") or ""
check("a topic reads back whole", runs.startswith("# ") and "redteam-logs" in runs)
check("a unique prefix works", read_doc(config, "ru") == read_doc(config, "runs"))
check("case and .md are forgiven", read_doc(config, "RUNS.md") == read_doc(config, "runs"))
check("an exact title works", read_doc(config, "Attack modes") is not None)
check("an unknown topic is None, not a guess", read_doc(config, "no-such-topic") is None)

# -- the info tool --------------------------------------------------------
tools = InfoTools(Config.from_env())
offered = {schema["function"]["name"] for schema in INFO_TOOL_SCHEMAS}
check("read_docs is offered to the model", "read_docs" in offered)
result = tools.call("read_docs", {"name": "runs"})
check("read_docs returns the document", result.ok and "redteam-logs" in result.text)
check("read_docs names the topics it knows", "topics:" in tools.call("read_docs", {"name": "nope"}).text)
check("read_docs needs its argument", not tools.call("read_docs", {}).ok)
check("a no-argument info tool still refuses arguments", not tools.call("harness_help", {"name": "x"}).ok)
check("an unknown info tool is refused", not tools.call("rm_rf", {}).ok)

# -- a configured directory is honoured -----------------------------------
with tempfile.TemporaryDirectory() as tmp:
    elsewhere = Path(tmp)
    (elsewhere / "mine.md").write_text("# mine\n\nOne topic.\n", encoding="utf-8")
    check("an empty directory has no index", docs_index(ChatConfig(docs_dir=str(elsewhere / "gone"))) == "")
    check(
        "CHAT_DOCS_DIR is read from the config",
        docs_dir(ChatConfig(docs_dir=str(elsewhere))) == elsewhere,
    )
    check(
        "the info tools' whole-Config form works too",
        docs_dir(Config.from_env(env={"CHAT_DOCS_DIR": str(elsewhere)})) == elsewhere,
    )
    check(
        "a configured directory replaces the shipped one",
        [doc.name for doc in list_docs(ChatConfig(docs_dir=str(elsewhere)))] == ["mine"],
    )

# -- the part that rots ---------------------------------------------------
source = (REPO / "rt_harness").glob("*.py")
code = "\n".join(path.read_text(encoding="utf-8") for path in source)


def doc(name: str) -> str:
    return read_doc(config, name) or ""


# A command is a backticked `/word` that is not the head of a longer path:
# `/run start` is a command, `/home/jeff/redtram` is a path in prose.
commands = set(re.findall(r"`(/[a-z][a-z-]*)(?![\w/])", "\n".join(doc(d.name) for d in docs)))
unknown = sorted(word for word in commands if word not in code)
check("every command the docs name exists in the code", not unknown, ", ".join(unknown))

envs = set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", doc("config")))
words = {"JSON", "HTTP", "CLI", "SOUL", "README", "URL", "API", "OLLAMA", "REDTEAM"}
envs -= words
missing = sorted(word for word in envs if f'"{word}"' not in code)
check("every env var the docs name exists in the code", not missing, ", ".join(missing))

paths = set(re.findall(r"`(rt_harness/[a-z_]+\.py)`", "\n".join(doc(d.name) for d in docs)))
check("every module the docs name exists", all((REPO / p).is_file() for p in paths), str(sorted(paths)))

suites = set(re.findall(r"`(test_[a-z_]+\.py)`", doc("tests") or ""))
missing = sorted(name for name in suites if not (REPO / "tests" / name).is_file())
check("every suite the docs name exists", not missing, str(missing))

# -- and the model really is handed the index -----------------------------
from rt_harness.chat import ChatSession  # noqa: E402
from rt_harness.client import OllamaClient  # noqa: E402

client = OllamaClient("http://127.0.0.1:9")  # no call is made to build a prompt
info = InfoTools(Config.from_env())
session = ChatSession(client, ChatConfig(protocol="text"), info=info)
prompt = session.system_prompt()
check("the index reaches the system message", "- runs:" in prompt and "read_docs" in prompt)
check("the model is told to fetch, not to guess", "read_docs" in prompt)
tool_less = ChatSession(client, ChatConfig(protocol="text", tools=False), info=info)
check(
    "a session without tools is not told about documents it cannot fetch",
    "- runs:" not in tool_less.system_prompt(),
)

print()
if failed:
    print(f"{len(failed)} failed: {', '.join(failed)}")
    sys.exit(1)
print(f"all docs checks passed ({len(docs)} topics)")
