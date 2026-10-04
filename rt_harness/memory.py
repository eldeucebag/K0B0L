"""Long-term memory for research runs: one SQLite file, shared by every model.

The harness's context is bounded twice over: the server's window and the chat's
own ``history_turns`` trim. Anything older than the trim is gone, and a second
session -- another model, another domain, another week -- starts blank. This
module is the persistent layer under both: observations extracted from
transcripts are written once, with provenance (model, session, domain, time),
and recalled lexically on demand into whichever session asks.

Deliberately SQLite-only, FTS5 for search, stdlib everywhere: the survey's
conclusion was that the separate vector database is disappearing, and a harness
that already refuses a shell tool is not going to grow a database daemon. The
tiering follows the same logic: FTS5 is tier 1 and answers most recalls in
about a millisecond; there is no tier 2 yet because nothing here needs one --
when recall quality demands embeddings, ``recall()`` is the seam they slot
into, not a rewrite of the callers.

Model independence is the point: the record carries which model wrote it, but
nothing in the schema or the query path keys on the model, so a fact learned
under Gemma is recalled under DeepSeek. Domains are free-form tags; "research
run" is just whatever string the session was tagged with.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Wall-clock ceiling on one recall's FTS work; a research store that takes
#: seconds to answer takes longer than the model call it feeds.
DEFAULT_LIMIT = 12
#: How many results one recall may return.
MAX_RECALL_ROWS = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    body        TEXT NOT NULL,
    domain      TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    session     TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    importance  REAL NOT NULL DEFAULT 0.5,
    created     REAL NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    body, domain,
    content='memories', content_rowid='id', tokenize='porter unicode61'
);
-- Keep the FTS index in step with the table it mirrors.
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, body, domain)
    VALUES (new.id, new.body, new.domain);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body, domain)
    VALUES ('delete', old.id, old.body, old.domain);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, body, domain)
    VALUES ('delete', old.id, old.body, old.domain);
    INSERT INTO memories_fts(rowid, body, domain)
    VALUES (new.id, new.body, new.domain);
END;
CREATE INDEX IF NOT EXISTS memories_domain ON memories(domain);
CREATE INDEX IF NOT EXISTS memories_created ON memories(created);
"""


def _fts_query(text: str) -> str:
    """A query FTS5 accepts, built from the words the operator typed.

    Bare words are ORed, not ANDed: a recall is narrowing by ranking, not by
    elimination. A long natural question ("what did we find about the router
    evicting models?") carries filler words that would kill an AND, while
    BM25 already down-ranks rows matching only one word. Punctuation is
    dropped rather than escaped -- FTS5's query syntax is a small language
    and a stray ``-`` or quote in it is a syntax error, so the safe
    translation of that question is ``what* OR find* OR router* OR ...``.
    """
    words = re.findall(r"[A-Za-z0-9_]{3,}", text.lower())
    if not words:
        return ""
    return " OR ".join(f"{word}*" for word in words[:8])


@dataclass
class Memory:
    """One recalled row, with its provenance intact."""

    id: int
    body: str
    domain: str = ""
    model: str = ""
    session: str = ""
    source: str = ""
    importance: float = 0.5
    created: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class MemoryStore:
    """The persistent research memory. One file, no server, no deps."""

    def __init__(self, path: str | Path | None = None) -> None:
        #: Where it lives: the chat's own state directory, next to the theme
        #: and sessions, so it follows the app (and its rename migration)
        #: rather than living in some workspace that comes and goes.
        if path is None:
            path = Path.home() / ".k0b0l-memory.sqlite"
        self.path = Path(path)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- writes ------------------------------------------------------------
    def remember(
        self,
        body: str,
        *,
        domain: str = "",
        model: str = "",
        session: str = "",
        source: str = "",
        importance: float = 0.5,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Write one memory. Duplicate bodies in the same domain are merged.

        A research run re-derives the same facts; stacking them verbatim is
        how a memory store bloats into its own noise. Merging raises the
        existing row's importance instead of adding a twin, which is the
        convergence behaviour the survey called the deciding feature.
        """
        body = (body or "").strip()
        if not body:
            raise ValueError("a memory needs a body")
        existing = self._db.execute(
            "SELECT id, importance FROM memories WHERE body = ? AND domain = ?",
            (body, domain),
        ).fetchone()
        if existing is not None:
            self._db.execute(
                "UPDATE memories SET importance = MAX(importance, ?), "
                "meta = ? WHERE id = ?",
                (min(1.0, max(existing["importance"], importance) + 0.1),
                 json.dumps(meta or {}), existing["id"]),
            )
            self._db.commit()
            return int(existing["id"])
        cursor = self._db.execute(
            "INSERT INTO memories (body, domain, model, session, source,"
            " importance, created, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (body, domain, model, session, source, importance,
             time.time(), json.dumps(meta or {})),
        )
        self._db.commit()
        return int(cursor.lastrowid or 0)

    def forget(self, memory_id: int) -> bool:
        """Delete one row. Returns whether it existed."""
        cursor = self._db.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        self._db.commit()
        return cursor.rowcount > 0

    # -- reads -------------------------------------------------------------
    def recall(
        self,
        query: str,
        *,
        domain: str = "",
        limit: int = DEFAULT_LIMIT,
    ) -> list[Memory]:
        """Lexical recall, ranked by match, importance, and recency.

        FTS5 does the finding (porter-stemmed BM25); the ranking layers the
        signals a research run cares about -- how often a fact was re-derived,
        and how recently it was written. An empty query returns the most
        important recent memories, which is what a fresh session wants
        injected before it starts work.
        """
        limit = max(1, min(limit, MAX_RECALL_ROWS))
        terms = _fts_query(query)
        if terms and domain:
            sql = (
                "SELECT m.* FROM memories m JOIN memories_fts f ON f.rowid = m.id"
                " WHERE memories_fts MATCH ? AND m.domain = ?"
                " ORDER BY bm25(memories_fts), m.importance DESC, m.created DESC"
                " LIMIT ?"
            )
            rows = self._db.execute(sql, (terms, domain, limit)).fetchall()
        elif terms:
            sql = (
                "SELECT m.* FROM memories m JOIN memories_fts f ON f.rowid = m.id"
                " WHERE memories_fts MATCH ?"
                " ORDER BY bm25(memories_fts), m.importance DESC, m.created DESC"
                " LIMIT ?"
            )
            rows = self._db.execute(sql, (terms, limit)).fetchall()
        elif domain:
            sql = (
                "SELECT m.* FROM memories m WHERE m.domain = ?"
                " ORDER BY m.importance DESC, m.created DESC LIMIT ?"
            )
            rows = self._db.execute(sql, (domain, limit)).fetchall()
        else:
            sql = (
                "SELECT m.* FROM memories m"
                " ORDER BY m.importance DESC, m.created DESC LIMIT ?"
            )
            rows = self._db.execute(sql, (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row: sqlite3.Row) -> Memory:
        try:
            meta = json.loads(row["meta"] or "{}")
        except ValueError:
            meta = {}
        return Memory(
            id=int(row["id"]), body=str(row["body"]), domain=str(row["domain"]),
            model=str(row["model"]), session=str(row["session"]),
            source=str(row["source"]), importance=float(row["importance"]),
            created=float(row["created"]), meta=meta,
        )

    def count(self, domain: str = "") -> int:
        if domain:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE domain = ?", (domain,)
            ).fetchone()
        else:
            row = self._db.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"])

    def close(self) -> None:
        self._db.close()

    # -- chat integration ----------------------------------------------------
    def recall_block(self, query: str, *, domain: str = "", limit: int = DEFAULT_LIMIT) -> str:
        """Memories as a system-message block, or '' when nothing recalled.

        Written for :meth:`rt_harness.chat.ChatSession.system_prompt` to call
        ahead of a turn: the block is small (a handful of one-line facts), and
        every line carries its provenance so the model can cite where a fact
        came from instead of asserting it from nowhere.
        """
        found = self.recall(query, domain=domain, limit=limit)
        if not found:
            return ""
        lines = ["Earlier notes that may be relevant:"]
        for memory in found:
            stamp = time.strftime("%Y-%m-%d", time.localtime(memory.created))
            who = f" by {memory.model}" if memory.model else ""
            lines.append(f"- {memory.body} ({stamp}{who})")
        return "\n".join(lines)
