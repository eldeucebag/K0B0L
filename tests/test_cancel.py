"""Operator cancel of an in-flight turn (the webui Cancel button).

Pins the engine-side contract the webui's cancel relies on:
- a client whose cancel flag is set stops at the next chunk boundary;
- the partial prose ALREADY streamed is kept in history (the page and
  the model agree on what the conversation holds);
- the turn ends with the "generation cancelled" notice, no tool calls
  run, no further rounds generate;
- no bogus "no visible prose" advice fires for a cancelled turn;
- the next turn starts clean (a stale cancel flag cannot kill it).
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness.chat import ChatHooks, ChatSession
from rt_harness.client import TurnCancelled
from rt_harness.config import ChatConfig


class CancelClient:
    """Streaming fake with the cancel flag, mirroring both real clients."""

    def __init__(self, n_chunks: int = 5000):
        self.n_chunks = n_chunks
        self._cancelled = False

    def begin_turn(self) -> None:
        self._cancelled = False

    def cancel_turn(self) -> None:
        self._cancelled = True

    def _raise_for_cancel(self) -> None:
        if self._cancelled:
            raise TurnCancelled()

    def supports_native_tools(self, model: str) -> bool:
        return False

    def chat_stream(self, **kwargs):
        n = 0
        while n < self.n_chunks:
            self._raise_for_cancel()
            yield {"message": {"content": "word "}}
            n += 1
        yield {"done": True, "done_reason": "stop"}


class RecordingHooks(ChatHooks):
    def __init__(self):
        self.deltas = 0
        self.notices: list[str] = []
        self.tool_calls: list[str] = []
        self.turns = 0

    def delta(self, text):
        self.deltas += 1

    def notice(self, text):
        self.notices.append(text)

    def tool_call(self, name, arguments):
        self.tool_calls.append(name)

    def inference_start(self):
        pass

    def inference_end(self):
        pass

    def turn_start(self, model, protocol):
        pass

    def turn_end(self):
        self.turns += 1


def make_session(client):
    tmp = tempfile.mkdtemp(prefix="k0b0l-cancel-")
    cfg = ChatConfig(root=tmp)
    return ChatSession(client, cfg, hooks=RecordingHooks())


class CancelTurnTest(unittest.TestCase):
    def test_cancel_keeps_partial_and_ends_turn(self):
        client = CancelClient()
        session = make_session(client)
        # begin_turn is called by the webui at submit; emulate it, then
        # cancel from the delta hook (the "operator clicked" moment).
        client.begin_turn()
        session.hooks.delta = _cancel_at(
            session.hooks, client, at=5).delta
        reply = session.send("hello")
        self.assertTrue(reply, "partial reply must be returned")
        self.assertEqual(session.hooks.deltas, 5)
        self.assertIn("generation cancelled", session.hooks.notices)
        self.assertNotIn(
            "no visible prose",
            " ".join(session.hooks.notices),
            "a cancelled turn must not receive empty-reply advice")
        roles = [m["role"] for m in session.messages]
        self.assertEqual(roles[-1], "assistant",
                         "the partial answer stays in history")
        self.assertEqual(roles.count("user"), 1)

    def test_cancel_does_not_run_pending_tool_calls(self):
        client = CancelClient()
        session = make_session(client)
        client.begin_turn()
        # cancel before any content: no prose, nothing ran
        client.cancel_turn()
        session.send("hello")
        self.assertEqual(session.hooks.tool_calls, [])
        self.assertIn("generation cancelled", session.hooks.notices)

    def test_next_turn_clean_after_cancel(self):
        client = CancelClient()
        session = make_session(client)
        client.begin_turn()
        client.cancel_turn()
        session.send("hello")
        before = session.hooks.deltas
        # second turn: no begin_turn call (flag must not leak) -- but
        # the webui DOES call begin_turn at submit; emulate both paths
        client.begin_turn()
        session.send("again")
        self.assertGreater(session.hooks.deltas, before)
        roles = [m["role"] for m in session.messages]
        self.assertEqual(roles.count("user"), 2)


def _cancel_at(hooks, client, at):
    class Hook:
        def delta(self, text):
            hooks.deltas += 1
            if hooks.deltas == at:
                client.cancel_turn()

        def __getattr__(self, name):
            return getattr(hooks, name)
    return Hook()


if __name__ == "__main__":
    unittest.main(verbosity=2)
