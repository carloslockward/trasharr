"""Radarr API v3 client."""

from __future__ import annotations

from typing import Any

from .arr import ArrrClient


class RadarrClient(ArrrClient):
    def _resource(self) -> str:
        return "movie"

    def movies(self) -> list[dict[str, Any]]:
        return self.list_all()

    # ---- file-level operations (per movie file, unlike the item-level base) ----

    def media_files(self, movie_id: int) -> list[dict[str, Any]]:
        """Movie files of a movie (records carry ``id`` and ``path``)."""
        return self.get("/api/v3/moviefile", params={"movieId": movie_id}).json()

    def delete_media_file(self, file_id: int) -> None:
        """Delete ONE movie file (Radarr removes the DB record; disk file too)."""
        self.delete(f"/api/v3/moviefile/{file_id}")
