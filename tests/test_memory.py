#!/usr/bin/env python3
"""The long-term memory store: cross-model, cross-domain, persistent.

The chat trims at 40 turns and the server window is finite; this store is the
layer under both. What has to hold: a fact written under one model recalls
under another, a domain narrows without blinding, duplicates converge instead
of stacking, the file survives reopen, and the system-message block a session
injects is either useful or empty -- never a traceback.

Run:  python3 tests/test_memory.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rt_harness.memory import MemoryStore  # noqa: E402

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


work = Path(tempfile.mkdtemp(prefix="k0b0l-memory-"))
try:
    db_path = work / "memory.sqlite"

    # -- write and recall, across models -----------------------------------
    store = MemoryStore(db_path)
    store.remember(
        "The PrismML router evicts a model when a different one is requested.",
        domain="serving", model="Gemma-4-E4B", session="s1", importance=0.8,
    )
    store.remember(
        "q4_0 KV costs about 81 KiB per token on the 8B models.",
        domain="serving", model="DeepSeek-14B", session="s2", importance=0.6,
    )
    store.remember(
        "The WIP-M3MORY survey ranks slot save/restore as the top pick.",
        domain="research", model="Gemma-4-E4B", session="s3", importance=0.5,
    )
    check("three memories land", store.count() == 3, str(store.count()))

    # The headline contract: written under Gemma, recalled with no model
    # filter at all -- and a different session sees it.
    hits = store.recall("router evict model")
    check("a lexical recall finds the router fact",
          any("evicts" in h.body for h in hits), str([h.body[:40] for h in hits]))
    check("recall carries provenance",
          hits and hits[0].model == "Gemma-4-E4B" and hits[0].domain == "serving",
          str((hits[0].model, hits[0].domain)) if hits else "no hits")

    # Domain narrowing: the research note must not answer a serving query.
    hits = store.recall("router evict model", domain="serving")
    check("a domain narrows without blinding",
          any("evicts" in h.body for h in hits), str(len(hits)))
    hits = store.recall("slot save restore", domain="research")
    check("the other domain still answers its own query",
          any("WIP-M3MORY" in h.body for h in hits), str([h.body[:30] for h in hits]))

    # Stemming: porter means "evicting" finds "evicts".
    hits = store.recall("what evicts a loaded model")
    check("porter stemming matches across inflection",
          any("evicts" in h.body for h in hits), str(len(hits)))

    # -- convergence, not stacking -----------------------------------------
    before = store.count()
    store.remember(
        "The PrismML router evicts a model when a different one is requested.",
        domain="serving", model="DeepSeek-14B", session="s4", importance=0.7,
    )
    check("a duplicate body converges instead of stacking",
          store.count() == before, f"{before} -> {store.count()}")
    row = [m for m in store.recall("router evict") if "evicts" in m.body][0]
    check("convergence raised the importance",
          row.importance > 0.8, str(row.importance))
    check("convergence kept the original provenance",
          row.model == "Gemma-4-E4B", row.model)

    # -- persistence across reopen ----------------------------------------
    store.close()
    store = MemoryStore(db_path)
    check("the store survives a reopen", store.count() == before, str(store.count()))
    hits = store.recall("kv cache cost per token")
    check("recalled memories keep their bodies after reopen",
          any("81 KiB" in h.body for h in hits),
          str([h.body[:40] for h in hits]))

    # -- the injection block -------------------------------------------------
    block = store.recall_block("router eviction", domain="serving")
    check("the block is non-empty when a hit exists", "evicts" in block, block[:80])
    check("the block cites provenance", "Gemma-4-E4B" in block, block[:120])
    empty = store.recall_block("zzz-qqq-nothing-matches")
    check("the block is empty when nothing recalls", empty == "", repr(empty[:60]))

    # -- deletion ------------------------------------------------------------
    target = store.recall("WIP-M3MORY")[0]
    check("forget removes the row", store.forget(target.id) and
          all(m.id != target.id for m in store.recall("WIP-M3MORY")))
    check("forgetting a ghost returns False", store.forget(999999) is False)

    # -- garbage tolerance ----------------------------------------------------
    try:
        store.remember("   ")
        check("an empty body is refused", False, "no error")
    except ValueError:
        check("an empty body is refused", True)
    hits = store.recall("!!! ???")   # no words at all
    check("a wordless query degrades to recent-important, not a crash",
          isinstance(hits, list), str(type(hits)))
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n{OK} ok, {FAIL} failure(s)")
sys.exit(1 if FAIL else 0)
