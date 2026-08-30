"""Jellyfin API client (read-only in trasharr).

trasharr never writes to Jellyfin: it is the source of "watched". Deletion is
performed through the arrs, and Jellyfin just rescans the library. Auth is by
API key (X-Emby-Token). The watched/played state is read for Movies and Series
(all episodes watched marks a series played in Jellyfin).
"""

from __future__ import annotations

from typing import Any

from .base import BaseClient


class JellyfinClient(BaseClient):
    def __init__(self, base_url: str, api_key: str = "") -> None:
        # Jellyfin expects the API key in X-Emby-Token, not X-Api-Key.
        super().__init__(base_url)
        if api_key:
            self.session.headers["X-Emby-Token"] = api_key

    def _client_info_header(self) -> str:
        # Required for the AuthenticateByName endpoint; not needed with an API key.
        return "Trasharr, 1.0.0.0, trasharr"

    def users(self) -> list[dict[str, Any]]:
        return self.get("/Users").json()

    def _primary_user_id(self) -> str:
        users = self.users()
        if not users:
            raise RuntimeError("Jellyfin returned no users; check the API key.")
        return users[0]["Id"]

    def watched_items(self, item_type: str) -> list[dict[str, Any]]:
        """Return played items of the given Jellyfin item type (Movie / Series)."""
        user_id = self._primary_user_id()
        params = {
            "Recursive": "true",
            "Limit": "500",
            "IncludeItemTypes": item_type,
            "Filters": "IsPlayed",
            "Fields": "Path,PrimaryImageAspectRatio",
        }
        data = self.get(f"/Users/{user_id}/Items", params=params).json()
        return data.get("Items") or []

    def watched_movies(self) -> list[dict[str, Any]]:
        return self.watched_items("Movie")

    def watched_series(self) -> list[dict[str, Any]]:
        return self.watched_items("Series")