"""Skills: markdown blocks that get composed into the system prompt.

A `skill file` is plain Markdown whose first heading names the skill (``#``
or `## Some Name`). Selecting it in the chat folds its text into the system
prompt before the default instructions, so the model reads the doctrine
before it reads its tools. Attack loops in `runctl` get the same treatment
via the `--skills` flag, so a "how to phrase probes" skill rides each attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: Heading used on the block that gets added to a chat's system prompt.
SKILL_INTRO = "Active skill(s):\n"


@dataclass(frozen=True)
class Skill:
    name: str
    body: str
    path: Path


def _profile_dir() -> Path:
    from .themes import migrate_state

    path = migrate_state(Path.home() / ".k0b0l-skills")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _workspace_dir(config_root: Path) -> Path:
    directory = config_root / "skills"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_skills(config_root: Path) -> list[Skill]:
    """All markdown skills found in the workspace and profile dirs."""
    found: list[Skill] = []
    for directory in (_workspace_dir(config_root), _profile_dir()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            title = ""
            for line in text.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("# ").strip()
                    break
            found.append(Skill(name=title or path.stem, body=text.strip(), path=path))
    return found


def read_skill(skills: Iterable[Skill], name: str) -> Skill | None:
    lowered = name.lower()
    for skill in skills:
        if skill.name.lower() == lowered or skill.path.stem.lower() == lowered:
            return skill
    return None
