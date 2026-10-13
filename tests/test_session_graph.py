"""Each session maintains its own chat context graph (webui).

Pins the SessionManager contract the webui verbs build on:
- a named load makes THAT file the active document -- turns autosave
  into the named session's own file, never the shared slot alone;
- the scratch slot stays a MIRROR of the live conversation carrying
  the active-session marker, so a restart restores the conversation
  AND pivots back to the named document;
- /clear pivots to the scratch slot (a fresh chat can never write
  over a named document) and the named file keeps its content;
- deleting the ACTIVE named document pivots to scratch (the next
  autosave cannot resurrect the file) and clears the conversation;
- a NAMED load never pivots through its marker (a backup saved out of
  another conversation is its own document).
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# SessionManager only needs ui.session.messages / turns /
# tool_calls_made and a session dir; it imports _session_dir lazily at
# construction, so patch the module it reads from BEFORE import.
import rt_harness.infotools as infotools  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="k0b0l-sess-graph-"))
(TMP / "sessions").mkdir()
infotools._session_dir = lambda: TMP / "sessions"  # type: ignore[assignment]

spec = importlib.util.spec_from_file_location(
    "webui_mod", REPO / "tools" / "webui.py")
webui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webui)


class FakeSession:
    def __init__(self):
        self.messages = [{"role": "system", "content": "sys"}]
        self.turns = 0
        self.tool_calls_made = 0


class FakeUI:
    def __init__(self):
        self.session = FakeSession()


def make_manager():
    ui = FakeUI()
    return webui.SessionManager(ui, tree=None)


def say(mgr, n, word):
    mgr.ui.session.messages.append({"role": "user", "content": word})
    mgr.ui.session.messages.append(
        {"role": "assistant", "content": f"{word} ack"})
    return mgr.autosave()


class PerSessionGraphTest(unittest.TestCase):
    def test_turns_write_into_the_named_document(self):
        mgr = make_manager()
        say(mgr, 1, "hello")
        mgr.save("alpha")
        mgr.load("alpha")
        mgr.adopt_document("alpha")
        say(mgr, 2, "world")
        alpha = json.loads(
            (TMP / "sessions" / "alpha.json").read_text())
        self.assertIn("world", json.dumps(alpha))
        self.assertEqual(alpha["active_session"], "alpha")

    def test_scratch_mirror_stays_current_and_carries_the_marker(self):
        mgr = make_manager()
        say(mgr, 1, "hello")
        mgr.save("alpha")
        mgr.load("alpha")
        mgr.adopt_document("alpha")
        say(mgr, 2, "mirror-me")
        mirror = json.loads(
            (TMP / "sessions" / webui.SessionManager.AUTOSAVE)
            .with_suffix(".json").read_text())
        self.assertIn("mirror-me", json.dumps(mirror))
        self.assertEqual(mirror["active_session"], "alpha")

    def test_clear_pivots_to_scratch_named_file_untouched(self):
        mgr = make_manager()
        say(mgr, 1, "hello")
        mgr.save("alpha")
        mgr.load("alpha")
        mgr.adopt_document("alpha")
        say(mgr, 2, "world")
        before = (TMP / "sessions" / "alpha.json").read_text()
        mgr.start_fresh()
        mgr.ui.session.messages = [
            {"role": "system", "content": "sys"}]
        say(mgr, 3, "fresh")
        self.assertEqual(before,
                         (TMP / "sessions" / "alpha.json").read_text())
        mirror = json.loads(
            (TMP / "sessions" / webui.SessionManager.AUTOSAVE)
            .with_suffix(".json").read_text())
        self.assertIn("fresh", json.dumps(mirror))

    def test_delete_active_named_pivots_and_clears(self):
        mgr = make_manager()
        say(mgr, 1, "hello")
        mgr.save("alpha")
        mgr.load("alpha")
        mgr.adopt_document("alpha")
        self.assertTrue(mgr.drop_active("alpha"))
        self.assertEqual(mgr.active_session,
                         webui.SessionManager.AUTOSAVE)
        # deleting a NON-active name must not pivot
        self.assertFalse(mgr.drop_active("beta"))

    def test_named_load_is_its_own_document(self):
        mgr = make_manager()
        say(mgr, 1, "hello")
        mgr.save("alpha")
        # a backup of alpha saved under another name: loading it must
        # make THE BACKUP the document, not pivot into alpha via any
        # marker its payload carried
        data = json.loads(
            (TMP / "sessions" / "alpha.json").read_text())
        (TMP / "sessions" / "backup.json").write_text(
            json.dumps({**data, "active_session": "alpha"}))
        mgr.load("backup")
        self.assertEqual(mgr.loaded_active, "backup")

    def test_scratch_load_reads_the_marker_for_restart_pivot(self):
        mgr = make_manager()
        say(mgr, 1, "hello")
        mgr.save("alpha")
        mgr.load("alpha")
        mgr.adopt_document("alpha")
        say(mgr, 2, "world")
        # simulate restart: startup loads the scratch slot
        mgr2 = make_manager()
        mgr2.load(webui.SessionManager.AUTOSAVE)
        self.assertEqual(mgr2.loaded_active, "alpha")


if __name__ == "__main__":
    unittest.main(verbosity=2)
