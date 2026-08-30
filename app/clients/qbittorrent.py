"""qBittorrent Web API v2 client.

Two authentication modes:

* API key (qBittorrent >= 5.2.0). A key generated in the WebUI (Web UI ->
  API Key) is sent as ``Authorization: Bearer <key>`` on every request.
  Stateless: no login round-trip, no password stored, and rotating the key
  in qBittorrent immediately invalidates the old one. This is the preferred
  mode. API keys cannot be used to fetch WebUI/static assets or the auth
  endpoints, which trasharr does not need.
* Username/password (older qBittorrent). A login posts credentials and the
  returned SID cookie authorizes subsequent calls. trasharr logs in lazily
  and refreshes the session once if a call comes back 403.

If an API key is configured it takes precedence over username/password.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ApiError, BaseClient, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class QBittorrentClient(BaseClient):
    def __init__(self, base_url: str, username: str = "", password: str = "", api_key: str = "") -> None:
        super().__init__(base_url)
        self.username = username
        self.password = password
        self.api_key = api_key.strip()
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @property
    def uses_api_key(self) -> bool:
        return bool(self.api_key)

    def login(self) -> bool:
        """Cookie login; only used when no API key is configured."""
        if self.uses_api_key:
            return True
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
        if self.uses_api_key:
            # Stateless: the Bearer header is already set; retry is pointless.
            return self._request(method, path, **kwargs)
        try:
            return self._request(method, path, **kwargs)
        except ApiError as exc:
            # Cookie auth: retry once on a 403 from an expired session.
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