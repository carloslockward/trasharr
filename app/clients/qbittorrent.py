"""qBittorrent Web API v2 client."""

from __future__ import annotations

import logging
from typing import Any

from .base import ApiError, BaseClient, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class QBittorrentClient(BaseClient):
    """Thin wrapper around the qBittorrent Web UI API.

    qBittorrent uses cookie-based auth: a login posts credentials and the
    returned SID cookie authorizes subsequent calls. This client logs in
    lazily and refreshes the session if a call comes back 403.
    """

    def __init__(self, base_url: str, username: str = "", password: str = "") -> None:
        super().__init__(base_url)
        self.username = username
        self.password = password

    def login(self) -> bool:
        data = {"username": self.username, "password": self.password}
        resp = self.session.post(self._url("/api/v2/auth/login"), data=data, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 403:
            # IP banned after repeated failed login attempts.
            return False
        ok = resp.ok and resp.text == "Ok."
        if ok:
            # SID cookie is stored in the session automatically.
            self.session.cookies.get("SID")
        return ok

    def _get(self, path: str, **kwargs) -> Any:
        return self._authorized("GET", path, **kwargs).json()

    def _post(self, path: str, **kwargs) -> Any:
        self._authorized("POST", path, **kwargs)
        return None

    def _authorized(self, method: str, path: str, **kwargs) -> Any:
        try:
            return self._request(method, path, **kwargs)
        except ApiError as exc:
            # Retry once on an expired session before propagating.
            if "403" in str(exc):
                self.session.cookies.clear()
                if self.login():
                    return self._request(method, path, **kwargs)
            raise

    # --- torrents ----------------------------------------------------------

    def torrents(self, **params) -> list[dict[str, Any]]:
        return self._get("/api/v2/torrents/info", params=params)

    def torrent_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        return self._get("/api/v2/torrents/files", params={"hash": torrent_hash})

    def torrent_trackers(self, torrent_hash: str) -> list[dict[str, Any]]:
        return self._get("/api/v2/torrents/trackers", params={"hash": torrent_hash})

    def set_limit(self, torrent_hash: str, ratio: float, seeding_time: int) -> None:
        """Set per-torrent share limits (0/negative values mean "no limit").

        Kept as an explicit escape hatch: trasharr normally expects the stack
        to be configured to seed forever so it can compute safety itself.
        """
        ratio_param = float(ratio) if float(ratio) > 0 else -1
        time_param = int(seeding_time) if int(seeding_time) > 0 else -1
        self._post(
            "/api/v2/torrents/setShareLimits",
            data={"hashes": torrent_hash, "ratioLimit": ratio_param, "seedingTimeLimit": time_param},
        )

    def delete_files(self, torrent_hash: str, delete_files: bool = True) -> None:
        """Remove a torrent, optionally deleting its files."""
        data = {"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"}
        self._post("/api/v2/torrents/delete", data=data)