"""Sonarr API v3 client."""

from __future__ import annotations

from typing import Any

from .arr import ArrrClient


class SonarrClient(ArrrClient):
    def _resource(self) -> str:
        return "series"

    def episodes_watched_state(self) -> dict[int, dict[str, Any]]:
        """Not used; watched state is the user's call, not a service's. Kept minimal."""
        return {}

    def series(self) -> list[dict[str, Any]]:
        return self.list_all()