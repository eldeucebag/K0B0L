"""Live catalogs the chat menu can populate from.

Everything here is a probe of something the harness can already answer: the
known Ollama server, the attack-mode catalog, the deployment cache, and the
run's configured roles. The menus ask these once per open so a model pulled
mid-session shows up without a restart.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config


def _apis_path() -> Path:
    return Path.home() / ".k0b0l-apis.json"


def _read_endpoints() -> list[dict[str, Any]]:
    from .themes import migrate_state

    try:
        raw = json.loads(migrate_state(_apis_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, str):
            out.append({"base_url": entry, "api_key": ""})
        elif isinstance(entry, dict) and entry.get("base_url"):
            record = {"base_url": str(entry["base_url"]),
                      "api_key": str(entry.get("api_key", ""))}
            # The saved-default marker rides along in the record; anything
            # else (old files, hand edits) simply has no default.
            if entry.get("default"):
                record["default"] = True
            out.append(record)
    return out


def default_api() -> str:
    """The operator's saved default endpoint, or '' when none is saved.

    Startup precedence: an explicit API_URL env wins (scripts and operators
    pin deliberately), then this saved default, then the localhost default.
    The endpoint answered at the dead-localhost prompt lands here, so it
    is used on every subsequent load.
    """
    for entry in _read_endpoints():
        if entry.get("default"):
            return entry["base_url"]
    return ""


def set_default_api(url: str, api_key: str = "") -> bool:
    """Remember ``url`` as the endpoint used on subsequent loads.

    The record is created or promoted to the front of the list and marked
    ``default``; every other record's marker is cleared (one default).
    Returns False when the file cannot be written.
    """
    url = (url or "").strip()
    if not url:
        return False
    records = [dict(entry) for entry in _read_endpoints()
              if entry["base_url"] != url]
    for entry in records:
        entry.pop("default", None)
    records.insert(0, {"base_url": url, "api_key": api_key, "default": True})
    try:
        _apis_path().write_text(json.dumps(records, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


def known_apis(config: "Config") -> list[str]:
    """Saved endpoints as display strings, current first."""
    urls = [entry["base_url"] for entry in _read_endpoints()]
    if config.ollama_url not in urls:
        urls.insert(0, config.ollama_url)
    return urls


def set_api(config: "Config", url: str, api_key: str = "") -> None:
    """Point the session at ``url`` and remember the endpoint record.

    Selecting an endpoint in-session is a stronger signal than the saved
    default, so the chosen record also becomes the default for subsequent
    loads.
    """
    config.ollama_url = url
    records = [dict(entry) for entry in _read_endpoints() if entry["base_url"] != url]
    for entry in records:
        entry.pop("default", None)
    records.insert(0, {"base_url": url, "api_key": api_key, "default": True})
    try:
        _apis_path().write_text(json.dumps(records, indent=2), encoding="utf-8")
    except OSError:
        pass


def known_models(config: "Config", timeout: float = 2.0) -> list[str]:
    """Model ids from the configured endpoint, whether it speaks Ollama's /api
    or OpenAI's /v1."""
    from .client import OllamaClient, OpenAIClient

    base = config.ollama_url.rstrip("/")
    if base.endswith("/v1"):
        try:
            return OpenAIClient(config.ollama_url).models()
        except Exception:  # noqa: BLE001
            return []
    url = base + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, KeyError):
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def mode_choices() -> list[tuple[str, str]]:
    """``(name, one-line)`` for every attack mode, in catalog order."""
    from . import modes

    return [(m.name, m.one_line) for m in modes.MODES]


def deployment_choices() -> list[str]:
    """Families in the pinned CL4R1T4S catalog (generated, offline)."""
    from . import deployment

    return deployment.families()
