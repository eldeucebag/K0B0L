---
name: music_director
description: Expand brief musical ideas into fully specified MiniMax-Music3 structured captions (Global Metadata, Vocal Details, Arrangement) backed by a 1000-template style library with genre routing, then generate via the generate_music tool. Gathers the brief interactively with ask_user_choice when anything is missing.
trigger:
  keywords: [music prompt, music director, song, compose, generate music, caption, lyrics, expand music prompt]
---

# Music Director

Music generation is not just describing a vibe — it is specifying a latent space coordinate across genre, structure, timbre, and prosody. A vague prompt yields generic drift; a disciplined structured caption dictates genre, mood, instrumentation, vocal character, and a section-aware arrangement with technical precision. This skill turns the operator's intent into that caption, backed by a curated library of 1,000 professional reference templates organized into 19 genre families — consulted through progressive disclosure, never scanned wholesale.

The deliverable is a MiniMax Music 3 structured caption (and optionally lyrics) ready for `generate_music`.

## Gathering the brief

Before expanding anything, make sure you hold the three constraints: the **genre/mood** (what it feels like), the **instrumentation/vocals** (who/what performs), and the **structure/lyrics** (how it unfolds). If any is missing, underspecified, or ambiguous, call `ask_user_choice` ONCE with the structured `items` argument — never interrogate through multiple back-and-forth messages.

Exact call shape:

```tool
{"name": "ask_user_choice", "arguments": {"prompt": "the music brief, in one line", "items": [
  {"type": "choice", "key": "genre", "label": "Genre / Mood", "options": ["pop", "rock", "funk", "synthwave", "ambient", "hip-hop", "jazz", "orchestral", "folk", "electronic", "metal", "r&b"], "multiple": false},
  {"type": "choice", "key": "vocal_style", "label": "Vocal style", "options": ["male lead", "female lead", "duet", "choir/backing", "rap/spoken", "instrumental"], "multiple": false},
  {"type": "choice", "key": "energy", "label": "Energy arc", "options": ["steady groove", "build to drop", "quiet-loud-quiet", "intro-verse-chorus-bridge", "progressive"], "multiple": false},
  {"type": "text", "key": "instrumentation", "label": "Key instruments / production notes", "placeholder": "e.g. slap bass, clavinet, 808, analog synth pads, live drums"},
  {"type": "group", "key": "lyrics", "label": "Lyrics", "items": [
    {"type": "choice", "key": "has_lyrics", "label": "Include lyrics?", "options": ["yes, full lyrics", "hook only", "instrumental"], "multiple": false},
    {"type": "text", "key": "theme", "label": "Lyrical theme / story", "placeholder": "e.g. late night drive, neon reflections, missed connection"},
    {"type": "choice", "key": "structure", "label": "Song structure", "options": ["verse-chorus", "verse-pre-chorus-chorus", "AABA", "through-composed", "loop-based"], "multiple": false}
  ]}
]}}
```

Rules for the form:
- Use `type: "choice"` with an `options` list when you can name candidates; `type: "text"` for free-form answers (with a `placeholder` naming what you need); group related axes under `type: "group"`.
- Drop fields whose answers you already hold from the message. Never put field dicts into the flat `options` array.
- Take the operator's answers as binding constraints. If the message already carries all three constraints, skip the form and proceed.

## Build the Music Brief

Extract only supported or reasonably inferred values: macro genre and subgenres, mood and emotional arc, approximate tempo/meter/groove, vocal presence/register/timbre/delivery, core instruments and production texture, section structure, spatial character, explicit exclusions. Classify each internally as `explicit`, `tagged`, `inferred`, or `unspecified`.

Do not invent a precise key, BPM, vocal gender, or production technique when a broader description is sufficient. Preserve an explicit instrumental request — never add vocals. Constraint precedence: (1) explicit user requirements and exclusions, (2) section-local directives from lyric tags, (3) strong implications from the caption, (4) selected reference characteristics, (5) conservative musical defaults. Never silently reverse an explicit vocal gender, instrumental requirement, tempo limit, or prohibited element.

## Consult the style library (progressive disclosure)

The library lives in this skill's directory. Use natural-language reasoning and local text files only — do not scan all templates or build any index of your own.

1. Read [references/genre-router.md](references/genre-router.md) first. It routes to one primary family index (two for an explicit fusion; the general pop/ballad family when only mood is available).
2. Read ONLY the selected family index (e.g. [references/index-hip-hop-rap.md](references/index-hip-hop-rap.md)) — compact style cards, one line per template.
3. Select up to three references with distinct roles: **Foundation** (closest overall identity/groove), **Modifier** (a requested secondary genre, vocal character, or production texture), **Arrangement** (section development, energy contour, transitions).
4. Read only the complete template files those cards name, under [templates/](templates/) (e.g. [templates/hip-hop-trap_0001.txt](templates/hip-hop-trap_0001.txt)).

Use templates safely: Foundation for broad musical identity, Modifier only for its matched dimension, Arrangement only for timeline logic. Do not inherit a template's exact key, BPM, vocalist, or section order; do not copy its sentences or complete structure — synthesize a new caption around the operator's brief.

## Write the structured caption

Render EXACTLY these three top-level headings, in this order:

### Global Metadata
Genre and subgenres, tempo (exact BPM only when explicit or strongly justified; otherwise a range or qualitative tempo), emotional progression, overall sonic and production profile. Key/scale only when explicit or musically useful.

### Vocal Details
For vocal music: lead configuration, timbre, register, delivery, harmony/backing vocals, restrained vocal effects. For instrumental music: state that the piece is instrumental and name the instrument or texture carrying the lead melodic role. Never invent lyrical subject matter or reproduce lyrics.

### Arrangement
A section-by-section timeline (0:00–0:10 Intro, etc. or section names). For every section state what enters, exits, changes, or intensifies — a readable energy arc, not an equipment list. Concrete musical changes over decorative prose. Default 250–450 English words unless the operator requests otherwise.

Do not include a song title, template IDs, reasoning trace, or copied lyric lines. Return JSON only when explicitly requested.

## Write the lyrics (if requested)

Tag sections explicitly: `[verse]`, `[pre-chorus]`, `[chorus]`, `[bridge]`, `[outro]`. Each line on its own. Keep syllabic pacing consistent with the target tempo. Embed the theme as concrete imagery, not abstract emotion. Use bracketed tags only as structural/production directives; never quote or paraphrase the operator's lyric text back.

## Deliver for generation

Hand the operator (or call directly when they asked) `generate_music` with:
- `caption`: the structured caption from above
- `lyrics`: the tagged lyrics block, or empty string for instrumental
- `duration`, `steps`, `seed` when specified

## Iterative refinement

If a generated result drifts, do not just add "more detail." Diagnose the failure — harmonic language, drum groove, vocal timbre, mix balance, structural pacing — and offer ONE `ask_user_choice` form naming the axes of drift rather than guessing. Re-consult the library with the diagnosis in hand. Use `remember` to lock in successful style descriptors for the session.

## Ground rules

- The target genre must permeate instrumentation, vocals, structure, and lyrics — not be tacked on.
- Concrete sonic nouns over abstract emotion ("detuned supersaw pads with slow attack", not "dreamy").
- Structurally contradictory requests embed the tension in the tech specs; never silently drop a constraint.
- Respect the hardware: `duration` max 240s, and the card's KV memory is SHARED between the caption+lyrics and the audio — long prompts shrink the max duration (a 350-word caption+lyrics caps around 170s; the runner clamps automatically and reports it). Ask for 60–120s for full songs; first passes 10–20s iterate fastest. A 120s/50-step run took ~32 min wall.
- Quality knobs: `steps` 30–60 (higher = better flow matching, slower). Long captions are fine — the engine handles them, but they slow prefill.
