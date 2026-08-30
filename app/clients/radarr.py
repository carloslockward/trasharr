"""Radarr API v3 client."""

from __future__ import annotations

from typing import Any

from .arr import ArrrClient


class RadarrClient(ArrrClient):
    def _resource(self) -> str:
        return "movie"

    def movies(self) -> list[dict[str, Any]]:
        return self.list_all()