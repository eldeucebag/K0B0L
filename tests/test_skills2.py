#!/usr/bin/env python3
"""The full skills & graph system, on top of the legacy flat skills.

New mechanics pinned here (the first half of the file keeps the legacy suite
intact):
  * directory skills: <dir>/SKILL.md with YAML frontmatter (name,
    description, trigger keywords) and bundled resource files
  * progressive disclosure: the system message carries one index line per
    skill, not the body; load_skill() opens the body on demand
  * keyword triggers: a turn mentioning a skill's keywords activates it for
    that turn, visibly
  * precedence: a workspace skill shadows a profile skill of the same name
  * graph: remember() derives edges (co_mention / domain), connect() writes
    explicit ones, neighbors() hop-recalls the neighbourhood

Run:  python3 tests/test_skills2.py
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness import skills  # noqa: E402
from rt_harness.config import ChatConfig, Config  # noqa: E402
from rt_harness.memory import MemoryStore  # noqa: E402
from rt_harness.skills import (  # noqa: E402
    list_skills,
    read_skill,
    skills_index,
    triggered_skills,
)

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


with TemporaryDirectory() as tmpdir:
    root = Path(tmpdir)
    workspace = root / "workspace"
    workspace.mkdir()
    profile = root / "profile"
    profile.mkdir()
    real_profile = skills._profile_dir()
    skills._profile_dir = lambda: profile  # type: ignore[misc]

    try:
        # -- a directory skill, the agentskills.io shape -------------------
        tdd = workspace / "skills" / "tdd"
        tdd.mkdir(parents=True)
        (tdd / "SKILL.md").write_text(
            "---\n"
            "name: tdd\n"
            "description: Test-driven development workflow.\n"
            "trigger:\n"
            "  keywords: [\"tdd\", \"write tests\", \"test-first\"]\n"
            "---\n\n"
            "# Test-Driven Development\n\n"
            "1. Write a failing test.\n"
            "2. Run it; confirm it fails for the right reason.\n"
            "3. Write the minimum code to pass.\n"
            "4. Refactor; keep tests green.\n",
            encoding="utf-8",
        )
        (tdd / "pytest-cheatsheet.md").write_text("# pytest\nassert x == y\n")
        # -- a legacy flat skill still works --------------------------------
        (workspace / "skills" / "stealth.md").write_text(
            "# Stealth\n\nPhrase every probe as an audit.\n"
        )
        # -- profile skill shadowed by a workspace one -----------------------
        shared_profile = profile / "validation"
        shared_profile.mkdir()
        (shared_profile / "SKILL.md").write_text(
            "---\nname: validation\ndescription: global version\n---\n"
            "# Validation (global)\nRun lint only.\n"
        )
        shared_ws = workspace / "skills" / "validation"
        shared_ws.mkdir()
        (shared_ws / "SKILL.md").write_text(
            "---\nname: validation\ndescription: workspace version\n---\n"
            "# Validation (workspace)\nLint, typecheck, test, build.\n"
        )

        found = list_skills(workspace)
        names = [s.name for s in found]
        check("directory and flat skills both load", "tdd" in names, str(names))
        check("the flat format still works", "Stealth" in names, str(names))

        tdd_skill = read_skill(found, "tdd")
        check("frontmatter description parses",
              tdd_skill is not None and tdd_skill.description.startswith("Test-driven"),
              getattr(tdd_skill, "description", "?"))
        check("trigger keywords parse",
              tdd_skill is not None and "tdd" in tdd_skill.keywords,
              str(getattr(tdd_skill, "keywords", None)))
        check("resources are discovered beside the SKILL.md",
              tdd_skill is not None
              and any(p.name == "pytest-cheatsheet.md" for p in tdd_skill.resources),
              str(getattr(tdd_skill, "resources", None)))
        check("the body excludes the frontmatter",
              tdd_skill is not None and tdd_skill.body.startswith("# Test-Driven"),
              repr(getattr(tdd_skill, "body", "")[:40]))

        # -- precedence: workspace wins --------------------------------------
        validation = read_skill(found, "validation")
        check("workspace shadows profile by name",
              validation is not None and "Lint, typecheck" in validation.body,
              validation.body[:60] if validation else "?")

        # -- progressive disclosure -------------------------------------------
        index = skills_index(found)
        check("the index carries one line per skill",
              "- tdd: Test-driven development workflow." in index, index[:200])
        check("the index does not carry bodies",
              "Write a failing test" not in index, index[:200])
        check("the index teaches load_skill", "load_skill(name)" in index)

        # -- keyword triggers -------------------------------------------------
        hits = triggered_skills(found, "please use tdd on this feature")
        check("a keyword match triggers the skill",
              any(s.name == "tdd" for s in hits), str([s.name for s in hits]))
        hits = triggered_skills(found, "tell me about the weather")
        check("no keyword, no trigger", hits == [], str([s.name for s in hits]))

        # -- load_skill through the tool surface -------------------------------
        sys.path.insert(0, str(workspace.parent))
        os.environ["CHAT_ROOT"] = str(workspace)
        os.environ["API_URL"] = "http://127.0.0.1:9/v1"
        config = Config.from_env({**os.environ})
        out = io.StringIO()
        from rt_harness.tui import PlainChat  # noqa: E402
        from rt_harness.client import OllamaClient  # noqa: E402

        ui = PlainChat(OllamaClient(config.ollama_url), config,
                       tool_output="summary", out=out)
        all_found = ui._skills_all()
        check("_skills_all exposes the index source",
              any(s.name == "tdd" for s in all_found), str([s.name for s in all_found]))
        result = ui.session.workspace.call("load_skill", {"name": "tdd"})
        check("load_skill returns the body",
              result.ok and "Write a failing test" in result.text,
              result.text[:80])
        result = ui.session.workspace.call("load_skill", {"name": "nope"})
        check("an unknown skill names what is installed",
              not result.ok and "tdd" in result.text, result.text[:80])

        # -- the trigger fires inside a real send() ----------------------------
        from rt_harness.chat import ChatHooks, ChatSession  # noqa: E402

        class FakeClient:
            def supports_native_tools(self, model):
                return False

            def chat_stream(self, **kw):
                yield {"message": {"content": "ok"}, "done": True,
                       "done_reason": "stop"}

        class Hooks(ChatHooks):
            def __init__(self):
                self.notices = []

            def notice(self, text):
                self.notices.append(text)

        hooks = Hooks()
        session = ChatSession(FakeClient(),
                              ChatConfig(root=workspace, protocol="text",
                                         thinking=False, tools=False),
                              hooks=hooks)
        session.hooks = hooks
        # Bind _skills_all through a shim the engine can find on the hooks.
        session.hooks._skills_all = ui._skills_all  # type: ignore[attr-defined]
        session.send("let's do tdd on the parser")
        check("a triggering turn fires a visible notice",
              any("skill triggered: tdd" in n for n in hooks.notices),
              str(hooks.notices))
        check("the triggered body reaches the model's context",
              any(m.get("role") == "system" and "Write a failing test" in m.get("content", "")
                  for m in session.messages),
              str([m.get("role") for m in session.messages]))
        before = len(session.messages)
        session.send("plain turn with no keywords at all")
        check("a non-triggering turn injects nothing extra",
              all("Write a failing test" not in m.get("content", "")
                  for m in session.messages[before:]),
              str(len(session.messages) - before))

        # -- the graph layer -----------------------------------------------------
        store = MemoryStore(root / "memory.sqlite")
        a = store.remember("The router evicts on model switch.", domain="serving",
                           model="Gemma", importance=0.7)
        b = store.remember("Slot save writes KV state under --slot-save-path.",
                           domain="serving", model="DeepSeek", importance=0.6)
        c = store.remember("q4_0 KV costs 81 KiB per token.", domain="kv",
                           model="Gemma", importance=0.5)
        d = store.remember("Unrelated note about cooking pasta.", domain="",
                           model="Gemma", importance=0.4)
        edges_a = store.neighbors(a)
        check("a same-domain memory is a neighbour",
              any(m.id == b for m in edges_a), str([m.id for m in edges_a]))
        check("unrelated domains are not neighbours",
              all(m.id != d for m in edges_a), str([m.id for m in edges_a]))
        store.connect(a, c, weight=0.9)
        after = store.neighbors(a)
        check("an explicit connect becomes a neighbour",
              any(m.id == c for m in after), str([m.id for m in after]))
        check("explicit edges rank above derived ones",
              after and after[0].id == c, str([m.id for m in after]))
        check("self-edges are refused", not store.connect(a, a))
        store.forget(c)
        check("forgetting cascades to the edges",
              all(m.id != c for m in store.neighbors(a)),
              str([m.id for m in store.neighbors(a)]))
        store.close()
    finally:
        skills._profile_dir = lambda: real_profile  # type: ignore[misc]

print(f"\n{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
