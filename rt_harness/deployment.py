"""Stock system prompts for a named model family, from CL4R1T4S.

A model is not evaluated as a bare checkpoint; it is evaluated as deployed, and
the stock system prompt is the largest part of that deployment. This module
binds a family name to the system prompt that family ships, so the harness can
mount a target *under* that prompt and let the planner and attacker see the
framing they are actually working against.

Source: https://github.com/elder-plinius/CL4R1T4S. The catalogue in
``data/cl4r1t4s.json`` is generated from that repository's tree by
``tools/gen_catalog.py`` and then pinned, so a run is reproducible without
network access. Ordering inside a family is a filename heuristic (an explicit
date first, then a version, with tool/function/skill tables demoted) and is
recorded per entry as ``latest`` / ``latest_reason``; it is a convenience, not a
claim about what a vendor currently serves. Name a variant explicitly when it
matters.

Only the local Ollama instance is ever called. Naming a family selects which
deployment prompt a *local* model is asked to operate under -- it does not
route anything to a vendor API, and this client has no way to reach one.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).parent / "data" / "cl4r1t4s.json"

#: Convenience names that are not a family directory in the source repository.
FAMILY_ALIASES = {
    "claude": "anthropic",
    "chatgpt": "openai",
    "gpt": "openai",
    "codex": "openai",
    "gemini": "google",
    "bard": "google",
    "grok": "xai",
    "llama": "meta",
    "whatsapp": "meta",
    "kimi": "moonshot",
    "v0": "vercel-v0",
    "vercel": "vercel-v0",
    "zcode": "zai",
    "droid": "factory",
    "leo": "brave",
    "lechat": "mistral",
}


class DeploymentError(RuntimeError):
    """Raised when a requested family or variant cannot be resolved or read."""


@dataclass(frozen=True)
class StockPrompt:
    """One stock system prompt, resolved and read into memory."""

    family: str
    variant: str
    repo_path: str
    text: str
    source_url: str
    local_path: Path
    sha256: str
    from_cache: bool
    latest_reason: str | None = None

    @property
    def label(self) -> str:
        return f"{self.family}/{self.variant}"

    @property
    def chars(self) -> int:
        return len(self.text)

    def provenance(self) -> dict[str, Any]:
        """Everything needed to reproduce which prompt this run used."""
        return {
            "source": "CL4R1T4S",
            "family": self.family,
            "variant": self.variant,
            "repo_path": self.repo_path,
            "source_url": self.source_url,
            "sha256": self.sha256,
            "chars": self.chars,
            "from_cache": self.from_cache,
            "selected_because": self.latest_reason,
        }


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    """The pinned catalogue. Cached; the file does not change during a run."""
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - ships with the package
        raise DeploymentError(
            f"catalogue missing at {CATALOG_PATH}; regenerate it with "
            f"tools/gen_catalog.py"
        ) from exc
    except ValueError as exc:  # pragma: no cover
        raise DeploymentError(f"catalogue at {CATALOG_PATH} is not valid JSON: {exc}") from exc


def _family_entries() -> dict[str, list[dict[str, Any]]]:
    return catalog()["families"]


def families() -> list[str]:
    return sorted(_family_entries())


def entries(family: str) -> list[dict[str, Any]]:
    """Variants for ``family``, newest first, or an error listing what exists."""
    key = FAMILY_ALIASES.get(family.lower().strip(), family.lower().strip())
    try:
        return _family_entries()[key]
    except KeyError as exc:
        raise DeploymentError(
            f"unknown family {family!r}. Known families: {', '.join(families())}. "
            f"Aliases: {', '.join(sorted(FAMILY_ALIASES))}."
        ) from exc


def resolve(spec: str) -> tuple[str, dict[str, Any]]:
    """Resolve ``family`` or ``family/variant-query`` to a catalogue entry.

    A bare family takes its newest entry. A variant query matches case
    insensitively, preferring an exact stem match and then a substring, and
    always preferring the newest of several matches.
    """
    spec = spec.strip()
    if not spec:
        raise DeploymentError("empty family specification")

    head, _, query = spec.partition("/")
    found = entries(head)
    family = FAMILY_ALIASES.get(head.lower().strip(), head.lower().strip())

    if not query:
        return family, found[0]

    needle = query.lower().strip()
    exact = [e for e in found if Path(e["variant"]).stem.lower() == needle]
    partial = [e for e in found if needle in e["variant"].lower()]
    for candidates in (exact, partial):
        if candidates:
            return family, candidates[0]

    raise DeploymentError(
        f"no variant of {family!r} matches {query!r}. Available:\n"
        + "\n".join(f"  {e['variant']}" for e in found)
    )


def local_path(directory: Path, family: str, variant: str) -> Path:
    """Where a variant is cached, with spaces folded out of the path."""
    safe = "/".join(part.replace(" ", "-") for part in Path(variant).parts)
    return Path(directory) / family / safe


def source_url(entry: dict[str, Any]) -> str:
    base = catalog()["raw_base"]
    quoted = urllib.parse.quote(entry["repo_path"], safe="/")
    return f"{base}/{quoted}"


def fetch(entry: dict[str, Any], family: str, directory: Path, timeout: float = 60.0) -> Path:
    """Download one variant into the local cache and return its path."""
    destination = local_path(directory, family, entry["variant"])
    url = source_url(entry)
    request = urllib.request.Request(url, headers={"User-Agent": "rt-harness"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise DeploymentError(f"HTTP {exc.code} fetching {url}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DeploymentError(
            f"could not fetch {url}: {exc}. Pass an explicit --system-prompt-file "
            f"to use a local copy instead."
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def load(
    spec: str,
    *,
    directory: Path,
    allow_fetch: bool = True,
) -> StockPrompt:
    """Resolve ``spec`` and read its text, fetching it if the cache is cold."""
    family, entry = resolve(spec)
    path = local_path(directory, family, entry["variant"])
    from_cache = path.is_file()

    if not from_cache:
        if not allow_fetch:
            raise DeploymentError(
                f"{family}/{entry['variant']} is not in the local cache at {path}, "
                f"and fetching is disabled (FETCH_SYSTEM_PROMPTS=0). Run "
                f"`thinlizzy.py --sync-prompts {family}` or unset that flag."
            )
        path = fetch(entry, family, directory)

    text = read(path)
    return StockPrompt(
        family=family,
        variant=entry["variant"],
        repo_path=entry["repo_path"],
        text=text,
        source_url=source_url(entry),
        local_path=path,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        from_cache=from_cache,
        latest_reason=entry.get("latest_reason"),
    )


def read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise DeploymentError(f"could not read system prompt at {path}: {exc}") from exc


def load_file(path: Path) -> StockPrompt:
    """Use an arbitrary local file as the deployment prompt."""
    path = Path(path)
    if not path.is_file():
        raise DeploymentError(f"system prompt file not found: {path}")
    text = read(path)
    return StockPrompt(
        family="local",
        variant=path.name,
        repo_path=str(path),
        text=text,
        source_url=f"file://{path}",
        local_path=path,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        from_cache=True,
        latest_reason="explicit --system-prompt-file",
    )


def sync(directory: Path, only: str | None = None) -> list[Path]:
    """Fetch every variant (or one family) into the cache. Returns written paths."""
    written: list[Path] = []
    targets = [FAMILY_ALIASES.get(only.lower().strip(), only.lower().strip())] if only else families()
    for family in targets:
        for entry in entries(family):
            written.append(fetch(entry, family, directory))
    return written


def describe() -> str:
    """A human-readable table of families and their newest variant."""
    lines = [
        f"{'FAMILY':24s} {'NEWEST VARIANT':44s} SELECTED BECAUSE",
        "-" * 92,
    ]
    for family in families():
        entry = _family_entries()[family][0]
        viable = len(_family_entries()[family])
        lines.append(
            f"{family:24s} {entry['variant']:44s} {entry.get('latest_reason', '?')}"
            f"   ({viable} variant{'s' if viable != 1 else ''})"
        )
    lines.append("")
    lines.append(f"source: {catalog()['source']}  ref: {catalog()['ref']}")
    lines.append(catalog()["note"])
    return "\n".join(lines)


@dataclass
class Deployment:
    """The deployment a target runs under, plus how it was chosen."""

    text: str
    provenance: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    @property
    def active(self) -> bool:
        return bool(self.text.strip())
