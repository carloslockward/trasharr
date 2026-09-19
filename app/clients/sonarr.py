"""Sonarr API v3 client."""

from __future__ import annotations

from typing import Any

from .arr import ArrrClient


class SonarrClient(ArrrClient):
    def _resource(self) -> str:
        return "series"

    def series(self) -> list[dict[str, Any]]:
        return self.list_all()

    # ---- file-level operations (per-episode, unlike the item-level base) ----

    def episodes(self, series_id: int) -> list[dict[str, Any]]:
        """All episodes of a series; each carries ``episodeFileId`` (0 = none)."""
        return self.get("/api/v3/episode", params={"seriesId": series_id}).json()

    def media_files(self, series_id: int) -> list[dict[str, Any]]:
        """Episode files of a series (records carry ``id`` and ``path``)."""
        return self.get("/api/v3/episodefile", params={"seriesId": series_id}).json()

    def delete_media_file(self, file_id: int) -> None:
        """Delete ONE episode file (Sonarr removes the DB record; disk file too)."""
        self.delete(f"/api/v3/episodefile/{file_id}")

    def set_episode_monitor(self, episode_ids: list[int], monitored: bool) -> None:
        """Monitor/unmonitor specific episodes (never the whole series)."""
        if episode_ids:
            self.put("/api/v3/episode/monitor",
                     json={"episodeIds": episode_ids, "monitored": monitored})
