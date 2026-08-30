"""Prowlarr API v1 client (used only for tracker *discovery*).

Seeding limits are NOT read from Prowlarr: they live in trasharr's config,
because the whole design sets Prowlarr/qBittorrent to "no limit" so nothing
ever stops seeding on its own. Prowlarr is used to enumerate the known
indexers/private trackers so the user can assign per-tracker limits.
"""

from __future__ import annotations

from typing import Any

from .base import BaseClient


class ProwlarrClient(BaseClient):
    def indexers(self) -> list[dict[str, Any]]:
        """Return the configured indexers."""
        return self.get("/api/v1/indexer").json()

    def health(self) -> bool:
        try:
            self.get("/api/v1/system/status")
            return True
        except Exception:
            return False