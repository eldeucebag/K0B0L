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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Config


def _apis_path() -> Path:
    return Path.home() / ".k0b0l-apis.json"


def _read_endpoints() -> list[dict[str, str]]:
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
            out.append({"base_url": str(entry["base_url"]), "api_key": str(entry.get("api_key", ""))})
    return out


def known_apis(config: "Config") -> list[str]:
    """Saved endpoints as display strings, current first."""
    urls = [entry["base_url"] for entry in _read_endpoints()]
    if config.ollama_url not in urls:
        urls.insert(0, config.ollama_url)
    return urls


def set_api(config: "Config", url: str, api_key: str = "") -> None:
    """Point the session at ``url`` and remember the endpoint record."""
    config.ollama_url = url
    records = [entry for entry in _read_endpoints() if entry["base_url"] != url]
    records.insert(0, {"base_url": url, "api_key": api_key})
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
