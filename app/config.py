"""JSON-backed configuration for trasharr.

All runtime configuration (endpoints, API keys, and per-tracker seeding limits)
lives in a single JSON file so it can be edited by an admin or through the web
settings page. The path is overridable with the ``TRASHARR_CONFIG`` environment
variable and defaults to ``config.json`` in the working directory.

API keys are stored verbatim, so this file must be kept out of version control
(see .gitignore) and mounted read-only where possible in a container.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.json"

# A tracker's seeding requirement is met when either its ratio OR its seed
# time goal is reached (matching how private trackers state the rule).
DEFAULT_SERVICE_FIELDS = {
    "enabled": False,
    "base_url": "",
    "api_key": "",
}

DEFAULT_CONFIG = {
    "qbittorrent": {
        "enabled": False,
        "base_url": "",
        "username": "",
        "password": "",
    },
    "sonarr": dict(DEFAULT_SERVICE_FIELDS),
    "radarr": dict(DEFAULT_SERVICE_FIELDS),
    "jellyfin": dict(DEFAULT_SERVICE_FIELDS),
    "prowlarr": dict(DEFAULT_SERVICE_FIELDS),
    # Trackers are keyed by their domain (as reported on qBittorrent torrents).
    # target_ratio and target_seed_time_minutes are the minimums trasharr
    # requires before deeming a torrent seeding-complete on that tracker.
    # A value of 0 means "no requirement" for that axis.
    "trackers": {},
    # Whether only watched & seeding-complete items are shown by default.
    "show_only_safe": True,
    # qBittorrent tag that marks cross-seed copies (default from the project).
    "cross_seed_tag": "cross-seed",
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` into a deep copy of ``base``, recursing into dicts."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Loads, holds, and persists the trasharr configuration."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.environ.get("TRASHARR_CONFIG", DEFAULT_CONFIG_PATH))
        self.data: dict[str, Any] = _deep_merge(DEFAULT_CONFIG, {})
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                # Merge over the default so newly added keys appear even in
                # older config files.
                self.data = _deep_merge(DEFAULT_CONFIG, loaded)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read config at %s (%s); using defaults.", self.path, exc)
                self.data = _deep_merge(DEFAULT_CONFIG, {})
        else:
            logger.info("No config at %s; using defaults.", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    # --- service accessors -------------------------------------------------

    def service(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    def is_enabled(self, name: str) -> bool:
        return bool(self.service(name).get("enabled"))

    # --- tracker helpers ---------------------------------------------------

    def tracker_domains(self) -> list[str]:
        return sorted(self.data["trackers"].keys())

    def tracker_requirement(self, domain: str) -> dict[str, float]:
        """Return the configured (target_ratio, target_seed_time) for a tracker."""
        entry = self.data["trackers"].get(domain, {})
        if not isinstance(entry, dict):
            return {"target_ratio": 0.0, "target_seed_time_minutes": 0.0}
        return {
            "target_ratio": float(entry.get("target_ratio", 0) or 0),
            "target_seed_time_minutes": float(entry.get("target_seed_time_minutes", 0) or 0),
        }

    def set_tracker_requirement(self, domain: str, target_ratio=0.0, target_seed_time_minutes=0.0) -> None:
        entry = self.data["trackers"].setdefault(domain, {})
        entry["target_ratio"] = target_ratio
        entry["target_seed_time_minutes"] = target_seed_time_minutes
        self.save()

    def remove_tracker(self, domain: str) -> None:
        self.data["trackers"].pop(domain, None)
        self.save()

    def cross_seed_tag(self) -> str:
        return str(self.data.get("cross_seed_tag", "cross-seed")) or "cross-seed"