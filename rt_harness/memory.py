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

-- The graph layer: typed, weighted edges between memories. An edge is a
-- claim that two facts belong in the same thought -- written explicitly
-- (connect()) or derived (shared domain, co-mention of the other's body).
-- Hop-recall walks it: Tier 1 (FTS5) finds the entry points, this table
-- expands the neighbourhood, the survey's short-circuit stays in the caller.
CREATE TABLE IF NOT EXISTS memory_edges (
    from_id     INTEGER NOT NULL,
    to_id       INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'related',
    weight      REAL NOT NULL DEFAULT 0.5,
    origin      TEXT NOT NULL DEFAULT 'derived',
    created     REAL NOT NULL,
    PRIMARY KEY (from_id, to_id, kind),
    FOREIGN KEY (from_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (to_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_edges_from ON memory_edges(from_id);
CREATE INDEX IF NOT EXISTS memory_edges_to ON memory_edges(to_id);
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
        # CASCADE deletes on the edge table need the pragma, per connection.
        self._db.execute("PRAGMA foreign_keys = ON")
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
        new_id = int(cursor.lastrowid or 0)
        self._db.commit()
        # Graph edges are derived at write time, not on every read: the
        # co-mention scan is O(store) once per new fact, which is the cheap
        # end of the trade.
        self._derive_edges(new_id, body, domain)
        return new_id

    def forget(self, memory_id: int) -> bool:
        """Delete one row. Returns whether it existed."""
        cursor = self._db.execute(
            "DELETE FROM memories WHERE id = ?", (memory_id,)
        )
        self._db.commit()
        return cursor.rowcount > 0

    # -- graph -------------------------------------------------------------
    def connect(self, from_id: int, to_id: int, *, kind: str = "related",
                weight: float = 0.8, origin: str = "explicit") -> bool:
        """Link two memories. The edge is the claim they belong together.

        The model's own judgement, recorded: an explicit edge survives even
        after both bodies drift, and hop-recall uses it exactly as surely as
        a derived one. Refuses self-edges; idempotent on re-assert (the
        stronger weight and explicit origin win).
        """
        if from_id == to_id:
            return False
        for a, b in ((from_id, to_id), (to_id, from_id)):
            self._db.execute(
                "INSERT INTO memory_edges (from_id, to_id, kind, weight,"
                " origin, created) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (from_id, to_id, kind) DO UPDATE SET"
                " weight = MAX(weight, excluded.weight),"
                " origin = CASE WHEN excluded.origin = 'explicit'"
                "   THEN 'explicit' ELSE origin END",
                (a, b, kind, min(1.0, max(0.0, weight)), origin, time.time()),
            )
        self._db.commit()
        return True

    def _derive_edges(self, memory_id: int, body: str, domain: str) -> None:
        """Cheap structural links for a fresh memory, written at remember() time.

        Two facts in the same domain get a weak 'domain' edge (they share a
        scope, which is a real but weak claim), and any older memory whose
        body shares distinctive words with this one gets a stronger
        'co_mention' edge. Both are marked derived, so an explicit connect()
        can outrank them without deleting them.
        """
        rows = self._db.execute(
            "SELECT id, body, domain FROM memories WHERE id != ?", (memory_id,)
        ).fetchall()
        words = {w for w in re.findall(r"[a-z0-9]{4,}", body.lower())}
        for row in rows:
            old_words = {w for w in re.findall(r"[a-z0-9]{4,}", row["body"].lower())}
            overlap = len(words & old_words)
            if overlap >= 2:
                weight = min(0.9, 0.3 + 0.1 * overlap)
                kind = "co_mention"
            elif domain and row["domain"] == domain:
                weight, kind = 0.3, "domain"
            else:
                continue
            for a, b in ((memory_id, row["id"]), (row["id"], memory_id)):
                self._db.execute(
                    "INSERT OR REPLACE INTO memory_edges (from_id, to_id, kind,"
                    " weight, origin, created) VALUES (?, ?, ?, ?, 'derived', ?)",
                    (a, b, kind, weight, time.time()),
                )
        self._db.commit()

    def neighbors(self, memory_id: int, *, hops: int = 1,
                  limit: int = 8) -> list[Memory]:
        """Hop-recall: the memory's graph neighbourhood, nearest first.

        One hop is the useful radius (the survey's Tier 2): the entry point's
        direct neighbours, ranked by edge weight then the same
        importance/recency ranking as lexical recall. Two hops are offered for
        the rare chain (A -> B -> C) but capped, because a walk past two hops
        on a co-mention graph is mostly noise.
        """
        seen = {memory_id}
        frontier = [memory_id]
        collected: list[tuple[float, sqlite3.Row]] = []
        for _hop in range(max(1, min(hops, 2))):
            next_frontier: list[int] = []
            for source in frontier:
                rows = self._db.execute(
                    "SELECT m.*, e.weight AS hop_weight FROM memory_edges e"
                    " JOIN memories m ON m.id = e.to_id"
                    " WHERE e.from_id = ? ORDER BY e.weight DESC,"
                    " m.importance DESC, m.created DESC",
                    (source,),
                ).fetchall()
                for row in rows:
                    if row["id"] in seen:
                        continue
                    seen.add(int(row["id"]))
                    next_frontier.append(int(row["id"]))
                    collected.append((float(row["hop_weight"]), row))
            frontier = next_frontier
            if not frontier:
                break
        collected.sort(key=lambda pair: (-pair[0], -pair[1]["importance"],
                                         -pair[1]["created"]))
        return [self._row(row) for _w, row in collected[:limit]]

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
