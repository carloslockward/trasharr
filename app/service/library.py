"""Assemble the library index from the currently-configured services.

This wires the config to live API clients, fetches the data build_index needs,
and returns the list of MediaItems. It is the single entry point the web layer
(and any future CLI) uses to present the "safe to delete" list.
"""

from __future__ import annotations

import logging

from typing import Optional, TypedDict

from ..config import Config
from ..clients import (
    QBittorrentClient,
    SonarrClient,
    RadarrClient,
    ProwlarrClient,
)
from .matcher import MediaItem, build_index

logger = logging.getLogger(__name__)


class ClientBundle(TypedDict, total=False):
    qbt: QBittorrentClient
    sonarr: SonarrClient
    radarr: RadarrClient
    prowlarr: ProwlarrClient


def make_clients(config: Config) -> ClientBundle:
    """Build API clients from config; disabled services are omitted."""
    q = config.service("qbittorrent")
    s = config.service("sonarr")
    r = config.service("radarr")
    p = config.service("prowlarr")
    bundle: ClientBundle = {}
    if config.is_enabled("qbittorrent"):
        bundle["qbt"] = QBittorrentClient(
            q.get("base_url", ""),
            q.get("username", ""),
            q.get("password", ""),
            q.get("api_key", ""),
        )
    if config.is_enabled("sonarr"):
        bundle["sonarr"] = SonarrClient(s.get("base_url", ""), s.get("api_key", ""))
    if config.is_enabled("radarr"):
        bundle["radarr"] = RadarrClient(r.get("base_url", ""), r.get("api_key", ""))
    if config.is_enabled("prowlarr"):
        bundle["prowlarr"] = ProwlarrClient(p.get("base_url", ""), p.get("api_key", ""))
    return bundle


def load_items(config: Config, clients: ClientBundle | None = None) -> tuple[list[MediaItem], list[str]]:
    """Fetch all arr items + torrents and evaluate seeding status.

    Returns ``(items, diagnostics)`` — diagnostics is a list of human-readable
    strings describing what was fetched (and any per-service errors), so an
    empty result is explainable rather than a silent mystery.
    """
    diag: list[str] = []
    clients = clients or make_clients(config)
    qbt: Optional[QBittorrentClient] = clients.get("qbt")
    sonarr: Optional[SonarrClient] = clients.get("sonarr")
    radarr: Optional[RadarrClient] = clients.get("radarr")

    if not qbt:
        diag.append("qBittorrent is not enabled or has no base URL set.")
    if not qbt or not (sonarr or radarr):
        diag.append("qBittorrent plus at least one arr must be enabled to build the list.")
        if qbt:
            qbt.close()
        return [], diag

    try:
        if qbt:
            qbt.login()

        sonarr_history: list = []
        radarr_history: list = []
        radarr_movies: list = []
        sonarr_series: list = []

        if sonarr:
            try:
                sonarr_history = sonarr.history(event_type=1)
                sonarr_series = sonarr.series()
                diag.append(f"Sonarr: {len(sonarr_series)} series, {len(sonarr_history)} grab records.")
            except Exception as exc:
                diag.append(f"Sonarr fetch failed: {exc}")
        if radarr:
            try:
                radarr_history = radarr.history(event_type=1)
                radarr_movies = radarr.movies()
                diag.append(f"Radarr: {len(radarr_movies)} movies, {len(radarr_history)} grab records.")
            except Exception as exc:
                diag.append(f"Radarr fetch failed: {exc}")

        items = build_index(
            qbt=qbt,
            config=config,
            sonarr_history=sonarr_history,
            radarr_history=radarr_history,
            radarr_movies=radarr_movies,
            sonarr_series=sonarr_series,
            radarr_base_url=(config.service("radarr").get("base_url") or ""),
            sonarr_base_url=(config.service("sonarr").get("base_url") or ""),
        )
        diag.append(f"Matched {len(items)} item(s) to live torrents.")
        return items, diag
    finally:
        if qbt:
            qbt.close()
