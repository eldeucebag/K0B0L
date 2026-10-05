"""Skills: portable procedural memory as directories with a SKILL.md.

The format is the agentskills.io convention the ecosystem is converging on: a
directory per skill whose ``SKILL.md`` carries YAML frontmatter (``name``,
``description``, and optionally ``trigger`` keywords) with the procedure as
the Markdown body. Scripts, reference docs, and templates can sit beside the
SKILL.md as first-class resources.

Two behaviours distinguish this from a prompt blob:

* **Progressive disclosure.** The chat's system message carries only each
  skill's ``name`` and ``description`` — one line each. The body is loaded
  on demand: by the model calling ``load_skill``, or by the operator's
  ``/skill`` command. Twenty skills cost twenty lines until one is wanted.
* **Keyword triggers.** Frontmatter may declare ``trigger: keywords``; when a
  turn's text matches, the engine activates that skill for the turn and says
  so in the transcript, so the operator can see a doctrine firing.

Precedence follows the ecosystem rule: workspace skills override profile
skills of the same name, first match wins — a repo can carry its own version
of a global skill without editing the global file.

Back-compat: a bare ``.md`` file in the skills directories is still a skill
(a title-less one keeps its filename as the name); the old flat format loads
with no frontmatter and no trigger, exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: Heading used on the block that gets added to a chat's system prompt.
SKILL_INTRO = "Active skill(s):\n"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_yaml(text: str) -> dict[str, Any]:
    """Frontmatter as a dict. pyyaml when present; a tiny parser otherwise.

    The fallback understands the flat ``key: value`` and ``key:`` list shapes
    skills actually use, which keeps a bare workspace free of new deps; pyyaml
    gives full fidelity when the chat stack is installed anyway.
    """
    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except ImportError:
        out: dict[str, Any] = {}
        current_list: str | None = None
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- ") and current_list:
                out[current_list].append(stripped[2:].strip().strip("'\""))
                continue
            key, _, value = stripped.partition(":")
            key, value = key.strip(), value.strip()
            if value == "":
                out[key] = []
                current_list = key
            else:
                out[key] = value.strip("'\"")
                current_list = None
        return out


@dataclass(frozen=True)
class Skill:
    name: str
    body: str
    path: Path
    description: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)
    resources: tuple[Path, ...] = field(default_factory=tuple)
    #: Skills this one builds on, by name (the graph edges). An edge is a
    #: claim about procedure order -- "validation builds on safe-changes" --
    #: kept in the frontmatter so it travels with the skill and any editor
    #: that edits the file edits the graph.
    requires: tuple[str, ...] = field(default_factory=tuple)

    def index_line(self) -> str:
        """The one line the system message carries until the skill loads."""
        label = self.name or self.path.stem
        if self.description:
            line = f"- {label}: {self.description}"
        else:
            line = f"- {label}"
        if self.requires:
            line += f" (requires {', '.join(self.requires)})"
        return line

    def matches(self, text: str) -> bool:
        """Do this skill's trigger keywords appear in ``text``?"""
        if not self.keywords:
            return False
        lowered = text.lower()
        return any(str(word).lower() in lowered for word in self.keywords)


def _profile_dir() -> Path:
    from .themes import migrate_state

    path = migrate_state(Path.home() / ".k0b0l-skills")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _workspace_dir(config_root: Path) -> Path:
    directory = config_root / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _load_skill_md(path: Path) -> Skill | None:
    """One directory-shaped skill: SKILL.md + frontmatter + resources."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        meta = _parse_yaml(match.group(1))
        body = text[match.end():]
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    keywords: tuple[str, ...] = ()
    trigger = meta.get("trigger")
    if isinstance(trigger, dict):
        raw = trigger.get("keywords") or ()
        if isinstance(raw, str):
            raw = [raw]
        keywords = tuple(str(word).strip() for word in raw if str(word).strip())
    elif isinstance(trigger, list):
        keywords = tuple(str(word).strip() for word in trigger if str(word).strip())
    requires: tuple[str, ...] = ()
    raw_requires = meta.get("requires") or ()
    if isinstance(raw_requires, str):
        raw_requires = [raw_requires]
    if isinstance(raw_requires, (list, tuple)):
        requires = tuple(str(name).strip() for name in raw_requires
                         if str(name).strip())
    resources = tuple(
        sorted(p for p in path.parent.iterdir()
               if p.is_file() and p.name != "SKILL.md")
    ) if path.parent.is_dir() else ()
    return Skill(
        name=name or _first_heading(body) or path.parent.name,
        body=body.strip(),
        path=path,
        description=description,
        keywords=keywords,
        resources=resources,
        requires=requires,
    )


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("# ").strip()
    return ""


def _load_flat_md(path: Path) -> Skill | None:
    """The legacy shape: one .md file, heading title, no frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return Skill(name=_first_heading(text) or path.stem, body=text.strip(), path=path)


def list_skills(config_root: Path) -> list[Skill]:
    """All skills, workspace before profile, first match winning by name.

    Both directories keep working with the flat format: a bare .md is a skill,
    and a directory is one only when it holds a SKILL.md.
    """
    found: dict[str, Skill] = {}
    order: list[str] = []
    for directory in (_workspace_dir(config_root), _profile_dir()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            skill: Skill | None = None
            if path.is_dir():
                candidate = path / "SKILL.md"
                if candidate.is_file():
                    skill = _load_skill_md(candidate)
            elif path.suffix.lower() == ".md":
                skill = _load_flat_md(path)
            if skill is None or not skill.body.strip():
                continue
            key = (skill.name or path.stem).lower()
            if key not in found:  # workspace wins; profile never overrides
                found[key] = skill
                order.append(key)
    return [found[key] for key in order]


def skills_index(skills: Iterable[Skill]) -> str:
    """The progressive-disclosure block for the system message."""
    lines = [SKILL_INTRO.rstrip(), ""]
    for skill in skills:
        lines.append(skill.index_line())
    lines.append("")
    lines.append(
        "A skill's body loads with load_skill(name) (or /skill name): call it "
        "before working in its area. Skills with keywords activate on their "
        "own when a turn mentions them."
    )
    return "\n".join(lines)


def triggered_skills(skills: Iterable[Skill], text: str) -> list[Skill]:
    """Skills whose keyword triggers match this turn's text, in list order."""
    return [skill for skill in skills if skill.matches(text)]


def read_skill(skills: Iterable[Skill], name: str) -> Skill | None:
    """Find one skill by name (or directory/filename stem)."""
    lowered = name.lower().strip()
    for skill in skills:
        if skill.name.lower() == lowered or skill.path.stem.lower() == lowered:
            return skill
        if skill.path.parent.name.lower() == lowered:
            return skill
    return None


def resolve_chain(skills: Iterable[Skill], name: str,
                  loaded: set[str] | None = None) -> list[Skill]:
    """One skill plus its un-met requires, in dependency order, cycles cut.

    The graph walk behind both load_skill and trigger cascades: the named
    skill's prerequisites load *before* it (a procedure that builds on
    another reads the foundation first), each prerequisite expanded the
    same way. A cycle -- a requires b requires a -- is a broken skill, not
    a hang: the second visit is skipped, and the chain still returns what
    it could resolve.

    Missing names are skipped silently here: the caller knows what it asked
    for and reports the edges it could not follow better than a list of
    strings could.
    """
    pool = list(skills)
    loaded = loaded if loaded is not None else set()
    out: list[Skill] = []

    def walk(skill: Skill) -> None:
        key = (skill.name or skill.path.stem).lower()
        if key in loaded:
            return
        loaded.add(key)
        for needed in skill.requires:
            found = read_skill(pool, needed)
            if found is not None:
                walk(found)
        out.append(skill)

    target = read_skill(pool, name)
    if target is not None:
        walk(target)
    return out
