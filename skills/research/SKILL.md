---
name: research
description: Find, verify, and organize knowledge for a research question — breadth first, sources cited, one honest synthesis.
trigger:
  keywords: [research, investigate, find out, sources, literature, survey]
---

# Research

Research is not search — a search finds one answer, research establishes
what is actually known. The distinction that matters: every claim must be
traceable to where it came from, and the deliverable is the map of the
territory, not a list of hits.

## The workflow

1. **Frame the question before touching a tool.** Restate the research
   target as a concrete question with a scope edge. "What memory
   backends exist" is unanswerable; "What long-context memory designs
   shipped in LLM inference servers in the last two years, and what does

   each trade for context length" is research.
2. **Breadth before depth.** Cast wide first — multiple distinct queries
   with different vocabulary, not one query repeated with synonyms.
   Diversify the *kinds* of sources: primary (papers, docs, repos),
   secondary (writeups, posts), negative space (what nobody mentions
   signals what doesn't exist).
3. **Read the source, not the snippet.** A search result is a lead, not a
   finding. Open the actual page/paper and extract what it says in its
   own terms before summarizing. If you can only get the abstract, say
   that — abstract-level findings are weaker findings.
4. **Verify by triangulation.** A claim is established when two
   independent sources agree. The same blog reposted five times is one
   source. Track which claims are (a) single-source, (b) corroborated,
   (c) your inference — and label them.
5. **Write down what fails.** Dead links, paywalls, contradictions between
   sources — these are data. A contradiction between two good sources is
   usually the most interesting finding in the survey.
6. **Synthesize before you list.** The deliverable has three layers:
   the answer to the framed question, the evidence organized by claim
   strength, and the open threads. A survey that only lists sources has
   not finished its job.

## Long runs: remember as you go

Research runs span sessions. Use `remember` at every milestone — not at
the end. Write what was established (with source attribution), what was
ruled out and why, and open questions worth pulling on. `recall` early
in the run and again before synthesis — earlier sessions may already
have the answer you are about to spend an hour re-finding.

Connect findings as one subject when they belong together:
`connect_memories` between facts from the same investigation;
`expand_memory` on any recalled fact before assuming it stands alone.

## Ground rules

- Every claim in the final writeup carries its source name and a marker
  for claim strength (single-source / corroborated / inference).
- Say "not found" plainly when something isn't — an honest gap beats a
  plausible-sounding fabrication.
- Prefer primary sources for load-bearing claims; use secondary ones for
  orientation only.
- The questions you could not answer are part of the deliverable.
