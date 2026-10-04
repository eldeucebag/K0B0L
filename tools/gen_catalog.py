#!/usr/bin/env python3
"""Build the pinned CL4R1T4S catalog from the live repo tree.

Run once (or when refreshing): reads the GitHub tree JSON, extracts the
per-family variant list, derives a newest-first ordering, and writes
rt_harness/data/cl4r1t4s.json. The ordering is a heuristic over the filename --
an explicit date if one is present, else a version number -- so it is recorded
alongside the entry it produced and can be overridden from the CLI.

  python3 tools/gen_catalog.py              # refresh from GitHub
  python3 tools/gen_catalog.py tree.json    # rebuild from a saved tree
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

TREE_URL = "https://api.github.com/repos/elder-plinius/CL4R1T4S/git/trees/main?recursive=1"
DEFAULT_DEST = Path(__file__).resolve().parent.parent / "rt_harness" / "data" / "cl4r1t4s.json"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

NAMED_DATE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-_ ]*(\d{1,2})?[-_ ,]*(\d{2,4})",
    re.I,
)
NUMERIC_DATE = re.compile(r"(\d{1,2})[-_/](\d{1,2})[-_/](\d{2,4})")
VERSION = re.compile(r"(?<![\d.])(\d{1,2})(?:[._](\d{1,2}))?(?![\d.])")

#: Files that ship alongside a system prompt but are not one: tool schemas,
#: function lists, slash commands. They sort below everything else in their
#: family so ``latest`` is a prompt, not a tool table.
SUPPLEMENTAL = re.compile(r"tools|functions|commands|skills|schema", re.I)

SKIP_NAMES = {"license", "readme", "readme.md"}
SKIP_EXTS = {".json"}


def normalise_year(raw: str) -> int:
    year = int(raw)
    return year + 2000 if year < 100 else year


def parse_date(text: str) -> tuple[int, int, int] | None:
    match = NAMED_DATE.search(text)
    if match:
        month = MONTHS[match.group(1)[:3].lower()]
        day = int(match.group(2)) if match.group(2) else 0
        return (normalise_year(match.group(3)), month, day)

    # Numeric dates fight with version numbers ("GPT-4.5_02-27-25" offers both
    # "5_02-27" and "02-27-25"), and those candidates overlap. finditer() would
    # resume after each match and never see the second one, so step the search
    # forward by one character instead.
    best: tuple[int, int] | None = None
    chosen: tuple[int, int, int] | None = None
    position = 0
    while True:
        candidate = NUMERIC_DATE.search(text, position)
        if candidate is None:
            break
        position = candidate.start() + 1
        month, day, year = (int(candidate.group(i)) for i in (1, 2, 3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        score = (1 if len(candidate.group(3)) == 4 else 0, candidate.start())
        if best is None or score > best:
            best = score
            chosen = (normalise_year(candidate.group(3)), month, day)
    return chosen


def parse_version(text: str) -> tuple[int, int]:
    """Largest version-looking token in the name, ignoring date digits."""
    cleaned = NAMED_DATE.sub(" ", text)
    cleaned = NUMERIC_DATE.sub(" ", cleaned)
    cleaned = re.sub(r"\b(19|20)\d{2}\b", " ", cleaned)
    best = (0, 0)
    for match in VERSION.finditer(cleaned):
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        if (major, minor) > best:
            best = (major, minor)
    return best


def family_key(directory: str) -> str:
    return directory.lower().replace(" ", "-")


def fetch_tree() -> dict:
    """Read the repository tree from GitHub, so a refresh needs no curl step."""
    request = urllib.request.Request(
        TREE_URL,
        headers={"User-Agent": "rt-harness", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    dest = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DEST
    tree = json.loads(src.read_text()) if src else fetch_tree()
    if "tree" not in tree:
        print(f"not a tree listing: {str(tree)[:200]}", file=sys.stderr)
        return 1

    families: dict[str, list[dict]] = {}
    for entry in tree["tree"]:
        if entry["type"] != "blob":
            continue
        repo_path = entry["path"]
        parts = repo_path.split("/")
        if len(parts) < 2:
            continue  # README.md, LICENSE
        stem = Path(repo_path).stem
        if stem.lower() in SKIP_NAMES or Path(repo_path).suffix.lower() in SKIP_EXTS:
            continue

        family = family_key(parts[0])
        variant = "/".join(parts[1:])
        date = parse_date(stem)
        version = parse_version(stem)
        families.setdefault(family, []).append(
            {
                "variant": variant,
                "repo_path": repo_path,
                "ext": Path(repo_path).suffix.lower(),
                "date": list(date) if date else None,
                "version": list(version),
                "supplemental": bool(SUPPLEMENTAL.search(stem)),
            }
        )

    for name, entries in families.items():
        # Newest first: a real prompt beats a tool table, then date, then
        # version, then path so the order is total.
        entries.sort(
            key=lambda e: (
                not e["supplemental"],
                tuple(e["date"]) if e["date"] else (0, 0, 0),
                tuple(e["version"]),
                e["repo_path"],
            ),
            reverse=True,
        )
        for index, entry in enumerate(entries):
            if index == 0:
                entry["latest"] = True
                entry["latest_reason"] = (
                    f"date {'-'.join(f'{p:02d}' for p in entry['date'])}"
                    if entry["date"]
                    else f"version {'.'.join(str(p) for p in entry['version'])}"
                    if entry["version"] != [0, 0]
                    else "only candidate"
                )

    catalog = {
        "source": "https://github.com/elder-plinius/CL4R1T4S",
        "ref": "main",
        "raw_base": "https://raw.githubusercontent.com/elder-plinius/CL4R1T4S/main",
        "note": (
            "ordering is derived from the filename (explicit date first, then "
            "version) and is a convenience default, not a claim about which "
            "prompt a vendor currently serves. Pick a variant explicitly when "
            "it matters."
        ),
        "families": {name: families[name] for name in sorted(families)},
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")

    print(f"families: {len(families)}  variants: {sum(len(v) for v in families.values())}")
    for name in sorted(families):
        top = families[name][0]
        print(f"  {name:22s} {top['variant']:42s} ({top['latest_reason']})")
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
