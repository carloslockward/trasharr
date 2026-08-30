"""Shared Sonarr/Radarr API v3 client logic."""

from __future__ import annotations

from typing import Any

from .base import BaseClient


class ArrrClient(BaseClient):
    """Base for Sonarr/Radarr; they share the API shape for the endpoints
    trasharr cares about (grab history, unmonitor, delete with files).
    """

    EVENT_GRAB = 1

    def history(self, event_type: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Fetch grab/download history, paginated.

        Returns raw records. Each grab record carries ``downloadId`` (the
        torrent hash) which is how trasharr matches torrents to media items.
        """
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            params = {"page": page, "pageSize": 100}
            if event_type is not None:
                params["eventType"] = event_type
            data = self.get("/api/v3/history", params=params).json()
            recs = data.get("records") or []
            records.extend(recs)
            if len(recs) < 100 or recs and data.get("totalRecords", 0) <= len(records):
                break
            page += 1
            if page > limit:
                break
        return records

    def series_or_movie(self, item_id: int) -> dict[str, Any]:
        return self.get(f"/api/v3/{self._resource()}/{item_id}").json()

    def list_all(self) -> list[dict[str, Any]]:
        """Return all movies/series from the arr."""
        return self.get(f"/api/v3/{self._resource()}").json()

    def unmonitor(self, item_id: int) -> None:
        """Set monitored=False on the movie/series."""
        item = self.series_or_movie(item_id)
        item["monitored"] = False
        self.put(f"/api/v3/{self._resource()}/{item_id}", json=item)

    def delete_files(self, item_id: int) -> None:
        """Delete the item's files through the arr (keeps DB + metadata intact)."""
        self.delete(f"/api/v3/{self._resource()}/{item_id}", params={"deleteFiles": "true"})

    def put(self, path: str, **kwargs):
        return self._request("PUT", path, **kwargs)

    def _resource(self) -> str:
        raise NotImplementedError