"""Soul tests: is SOUL.md loaded by default, and does it stay out of the way?

No model, no terminal, no Textual -- this is the lookup order and the system
message block, which is where a standing prompt can go wrong quietly.

Fixtures are written into a temp directory by this test itself: the explicit
file case uses a name of its own, the root case uses the lowercase ``soul.md``
spelling the loader also accepts. Nothing here touches the repository's own
SOUL.md.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import rt_harness.soul as soul_module  # noqa: E402
from rt_harness.config import ChatConfig  # noqa: E402
from rt_harness.soul import (  # noqa: E402
    DEFAULT_SOUL_PATH,
    SOUL_INTRO,
    load_soul,
    soul_block,
)

failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}{': ' + detail if detail else ''}")
        failed.append(name)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    explicit = write(work / "custom-soul.md", "You are terse.")
    root = work / "root"
    root.mkdir()
    write(root / "soul.md", "You are the project's.")
    empty = write(work / "empty-soul.md", "   \n")

    # -- default is on ---------------------------------------------------
    default = ChatConfig()
    check("a fresh config loads the soul", default.soul is True)
    check("nothing is configured to override it", default.soul_file == "")
    check(
        "the shipped soul is looked for at the repo root",
        DEFAULT_SOUL_PATH.name == "SOUL.md" and DEFAULT_SOUL_PATH.parent == REPO,
        str(DEFAULT_SOUL_PATH),
    )

    # -- off -------------------------------------------------------------
    off = load_soul(ChatConfig(soul=False), root)
    check("disabled reports itself as disabled", off.source == "disabled")
    check("disabled loads no text", not off.loaded and off.text == "")
    check("disabled contributes nothing to the prompt", soul_block(off) == "")
    check("disabled says so out loud", "disabled" in off.describe())

    # -- explicit file wins ----------------------------------------------
    chosen = load_soul(ChatConfig(soul_file=str(explicit)), root)
    check("an explicit soul file is used", chosen.loaded and chosen.path == explicit)
    check("its text comes through", "terse" in chosen.text)
    check("it is attributed", f"from {explicit}" in soul_block(chosen) or "from" in soul_block(chosen))
    check("the intro explains what the block is", SOUL_INTRO.split(",")[0] in soul_block(chosen))

    # -- then the session root -------------------------------------------
    rooted = load_soul(ChatConfig(), root)
    check("a soul next to the work is used", rooted.path == root / "soul.md")
    check("and it is the one that speaks", "project's" in rooted.text)

    # -- explicit beats root ---------------------------------------------
    both = load_soul(ChatConfig(soul_file=str(explicit)), root)
    check("an explicit file outranks the root", both.path == explicit)

    # -- an empty file falls through, rather than stopping the search ------
    blank = load_soul(ChatConfig(soul_file=str(empty)), root)
    check(
        "an empty file is not a soul, so the search continues",
        blank.loaded and blank.path == root / "soul.md",
        f"got {blank.source} {blank.path}",
    )

    # -- a missing file is not an error ----------------------------------
    # Nothing anywhere means the shipped soul has to be out of the way as
    # well: with a SOUL.md at the repo root, the default candidate always
    # resolves, and "missing" would never be tested.
    shipped_path = soul_module.DEFAULT_SOUL_PATH
    soul_module.DEFAULT_SOUL_PATH = work / "no-such-soul.md"
    try:
        missing = load_soul(ChatConfig(soul_file=str(work / "nope.md")), work / "nowhere")
        check("a missing soul is not fatal", missing.source == "missing" and not missing.loaded)
        check("a missing soul adds no block", soul_block(missing) == "")
        check(
            "a missing soul with the default on still reports what it looked for",
            "no file" in missing.describe(),
        )
    finally:
        soul_module.DEFAULT_SOUL_PATH = shipped_path

    # -- and the harness ships one, so a bare config finds it -------------
    shipped = load_soul(ChatConfig())
    check(
        "a bare config finds the shipped soul",
        shipped.loaded and shipped.path == shipped_path,
        f"got {shipped.source} {shipped.path}",
    )
    check("the shipped soul is more than a placeholder", len(shipped.text) > 200)
    check("a missing explicit file falls through to the shipped soul",
          load_soul(ChatConfig(soul_file=str(work / "typo.md"))).path == shipped_path)

# -- and it really is the first thing the model reads --------------------
from rt_harness.chat import ChatSession  # noqa: E402
from rt_harness.client import OllamaClient  # noqa: E402

# Nothing listens on port 9; no call is made to build a system message.
client = OllamaClient("http://127.0.0.1:9")
with tempfile.TemporaryDirectory() as tmp2:
    live = Path(tmp2)
    (live / "soul.md").write_text("You are terse.\n", encoding="utf-8")
    mine = live / "mine.md"
    mine.write_text("You are terse and you keep a project's history in mind.\n", encoding="utf-8")

    session = ChatSession(client, ChatConfig(protocol="text", root=live, soul_file=str(mine)))
    prompt = session.system_prompt()
    check("the soul leads the system message", prompt.startswith(SOUL_INTRO.split(",")[0]))
    check("the soul's own text is in the message", "project's history" in prompt)

    nearby = ChatSession(client, ChatConfig(protocol="text", root=live)).system_prompt()
    check("a soul beside the work is what the session reads", "terse" in nearby)

    quiet = ChatSession(
        client, ChatConfig(protocol="text", root=live, soul=False)
    ).system_prompt()
    check("--no-soul leaves the soul out", "terse" not in quiet)
    check("a session with no soul is still a session", len(quiet) > 0)

with tempfile.TemporaryDirectory() as tmp3:
    lonely = Path(tmp3)
    bare = ChatSession(
        client, ChatConfig(protocol="text", root=lonely, soul_file=str(lonely / "none.md"))
    ).system_prompt()
    check("a missing soul does not stop the message", len(bare) > 0 and "none.md" not in bare)

print()
if failed:
    print(f"{len(failed)} failed: {', '.join(failed)}")
    sys.exit(1)
print("all soul checks passed")
