"""Assemble the library index from the currently-configured services.

This wires the config to live API clients, fetches the data build_index needs,
and returns the list of MediaItems. It is the single entry point the web layer
(and any future CLI) uses to present the "safe to delete" list.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from ..clients import (
    ProwlarrClient,
    QBittorrentClient,
    RadarrClient,
    SonarrClient,
)
from ..config import Config
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
    qbt: QBittorrentClient | None = clients.get("qbt")
    sonarr: SonarrClient | None = clients.get("sonarr")
    radarr: RadarrClient | None = clients.get("radarr")

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
        sonarr_ok = radarr_ok = False

        if sonarr:
            try:
                # Full history (all event types): grab events are pruned by the
                # arr's history cleanup, but import events survive and carry
                # the same downloadId + series/movie id — both are evidence.
                sonarr_history = sonarr.history()
                sonarr_series = sonarr.series()
                sonarr_ok = True
                diag.append(f"Sonarr: {len(sonarr_series)} series, {len(sonarr_history)} history records.")
            except Exception as exc:
                diag.append(f"Sonarr fetch failed: {exc}")
        if radarr:
            try:
                radarr_history = radarr.history()
                radarr_movies = radarr.movies()
                radarr_ok = True
                diag.append(f"Radarr: {len(radarr_movies)} movies, {len(radarr_history)} history records.")
            except Exception as exc:
                diag.append(f"Radarr fetch failed: {exc}")

        # Orphan detection must only run with COMPLETE history: if an arr fetch
        # failed, its missing records would mislabel live torrents as orphans.
        orphan_detection = sonarr_ok and radarr_ok
        if not orphan_detection:
            diag.append("Orphan detection disabled: an arr history fetch failed.")

        items = build_index(
            qbt=qbt,
            config=config,
            sonarr_history=sonarr_history,
            radarr_history=radarr_history,
            radarr_movies=radarr_movies,
            sonarr_series=sonarr_series,
            radarr_base_url=(config.service("radarr").get("base_url") or ""),
            sonarr_base_url=(config.service("sonarr").get("base_url") or ""),
            orphan_detection=orphan_detection,
            sonarr=sonarr if sonarr_ok else None,
            radarr=radarr if radarr_ok else None,
        )
        diag.append(f"Matched {len(items)} item(s) to live torrents.")
        return items, diag
    finally:
        if qbt:
            qbt.close()
