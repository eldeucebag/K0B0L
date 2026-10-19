---
name: music_director
description: Expand brief musical ideas into fully specified MiniMax-Music3 generation specs. Gathers the brief interactively with ask_user_choice when anything is missing. Outputs caption + lyrics ready for generate_music.
trigger:
  keywords: [music prompt, music director, song, compose, generate music, caption, lyrics]
---

# Music Director

Music generation is not just describing a vibe — it is specifying a latent space coordinate across genre, structure, timbre, and prosody. A vague prompt yields generic drift; a disciplined spec dictates genre, mood, instrumentation, vocal character, and lyrical architecture with technical precision. The deliverable is a synthesis-ready spec (caption + lyrics), not a creative writing exercise.

## Gathering the brief

Before expanding anything, make sure you hold all three constraints: the **genre/mood** (what it feels like), the **instrumentation/vocals** (who/what performs), and the **structure/lyrics** (how it unfolds). If any is missing, underspecified, or ambiguous, call `ask_user_choice` ONCE with the structured `items` argument — never interrogate through multiple back-and-forth messages.

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
- Use `type: "choice"` with an `options` list when you can name candidates (genre families, vocal types, structures)
- Use `type: "text"` when the answer is free-form (instrumentation specifics, lyrical themes) — with a `placeholder` naming what you need
- Group related axes under `type: "group"` (lyrics has sub-axes)
- Drop fields whose answers you already hold from the message
- Never put field dicts into the flat `options` array — that renders as one choice list
- Take the operator's answers as binding constraints. If the message already carries all three constraints, skip the form and proceed.

## The workflow

1. **Ingest the constraints.** Take the genre/mood, instrumentation/vocals, and structure/lyrics from the message or the answered form. Treat the genre not as a decorative label, but as the foundational physics of the entire piece — it dictates harmonic language, rhythmic feel, timbral palette, and structural expectations.

2. **Expand the sonic identity.** Detail the core instrumentation, timbral descriptors, production techniques, and mix character. Infuse the arrangement, sound design, and processing directly with the requested genre's conventions (e.g., "sidechained sub-bass, FM synth leads, gated reverb on snare" for synthwave; "clean Stratocaster, tube amp breakup, spring reverb" for surf rock).

3. **Expand the vocal character (if not instrumental).** Detail voice type, range, phrasing, articulation, backing vocal arrangement, lyrical delivery style (melismatic, staccato, conversational, shouted). Ensure the vocal writing adheres to the genre's prosodic norms.

4. **Define the structural architecture.** Dictate the formal sections with measure counts or time allocations, harmonic rhythm, energy contour, transition techniques (filter sweeps, drum fills, breakdowns), and hook placement. For lyrical songs, specify the rhyme scheme density, syllable-per-measure pacing, and where the title/hook lands.

5. **Write the lyrics (if requested).** Tag sections explicitly: `[verse]`, `[pre-chorus]`, `[chorus]`, `[bridge]`, `[outro]`. Each line on its own. Keep syllabic pacing consistent with the target tempo/feel. Embed the theme as concrete imagery, not abstract emotion.

6. **Output the synthesis spec.** Deliver EXACTLY two fields ready for `generate_music`:
   - `caption`: a single paragraph describing the genre, mood, instrumentation, vocal style, production character, and structural arc
   - `lyrics`: the tagged lyrics block (or empty string for instrumental)

No conversational filler, no "Here is your spec" framing.

## Iterative refinement

Music synthesis is a feedback loop. If a generated result drifts from the target genre, do not just add "more detail." Diagnose the failure: was the harmonic language wrong? Was the drum pattern too generic? Was the vocal timbre mismatched? If the operator's correction is ambiguous, offer ONE `ask_user_choice` form (same `items` shape as above) naming the axes of drift you diagnosed — e.g. `{"type": "choice", "key": "drift_axis", "label": "What drifted?", "options": ["genre markers", "vocal timbre", "drum groove", "harmonic language", "mix balance", "structural pacing"], "multiple": true}` — rather than guessing. Adjust the technical specifications and genre constraints in the next iteration to explicitly correct the observed drift. Use `remember` to lock in successful genre descriptors for the current session.

## Ground rules

- Output ONLY the final spec (caption + lyrics). No "Here is your spec" or "I have expanded this."
- The target genre must permeate the instrumentation, vocals, structure, and lyrics — not just be tacked on at the end.
- Prioritize concrete sonic nouns and adjectives over abstract emotional concepts (e.g., "detuned supersaw pads with slow attack" instead of "dreamy").
- If a requested combination is structurally contradictory (e.g., "death metal with fingerpicked acoustic guitar"), embed the tension into the technical specs rather than silently ignoring one constraint.
- Never generate the music yourself unless the operator asks: your deliverable is the spec. The operator generates from the dialog.
- Respect the hardware ceiling: `duration` max 240s (~4 min AR budget on this GPU). Keep first passes short (10-20s) for fast iteration.
- Quality knobs: `steps` 30-60 (higher = better flow matching, slower). Default 30 is a good starting point.