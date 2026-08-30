"""Shared http helpers for trasharr API clients."""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


class ApiError(RuntimeError):
    """Raised when an upstream service returns an error."""


class BaseClient:
    """Small request wrapper; subclasses add typed methods."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "trasharr/1.0"})
        if api_key:
            self.session.headers.update({"X-Api-Key": api_key})

    def _url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        resp = self.session.request(method, self._url(path), **kwargs)
        if not resp.ok:
            raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    def get(self, path: str, **kwargs) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self._request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self._request("DELETE", path, **kwargs)

    def close(self) -> None:
        self.session.close()