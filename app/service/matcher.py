"""Matching qBittorrent torrents to Sonarr/Radarr media items and evaluating
whether their seeding requirement has been met.

Design notes
------------
* Matching is exact where possible. The arr grab history records ``downloadId``
  == the qBittorrent torrent hash, so once we know a media item's arr id we can
  attach its real torrents with no title guessing.
* The remaining fuzzy step is the *bridge* between Jellyfin (the "watched"
  truth) and the arrs (Sonarr/Radarr), because Jellyfin and the arrs have
  independent IDs. We bridge watched items to arr items by normalized title +
  year. That bridge is the one heuristic in the system and is kept isolated here
  so it can be hardened later (e.g. TheMovieDB/TVDB IDs) without touching the
  delete contract.
* A torrent's seeding-complete status is computed in trasharr using per-tracker
  targets from config -- because the stack is deliberately configured to "no
  limit, seed forever", qBittorrent never stops a torrent on its own.
* The "met" rule mirrors private trackers: met when ratio >= target OR
  seed time >= target. Set an unused axis to 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..clients.qbittorrent import QBittorrentClient

TorrentDict = dict[str, Any]


@dataclass
class TorrentInfo:
    """The subset of a qBittorrent torrent trasharr cares about."""

    hash: str
    name: str
    category: str
    tags: list[str]
    state: str          # e.g. uploading, downloading, pausedUP, forcedUP, error
    ratio: float
    seeding_time: int   # seconds seeded
    size: int
    save_path: str
    content_path: str
    tracker_domains: list[str] = field(default_factory=list)


@dataclass
class SeedEvaluation:
    """Safety assessment for one torrent."""

    torrent_hash: str
    tracker_domain: str | None
    target_ratio: float
    target_time_minutes: float
    ratio: float
    seeding_time_seconds: int
    ratio_met: bool
    time_met: bool

    @property
    def met(self) -> bool:
        ratio_required = self.target_ratio > 0
        time_required = self.target_time_minutes > 0
        if not ratio_required and not time_required:
            return True  # no requirement configured -> treated as complete
        return (ratio_required and self.ratio_met) or (time_required and self.time_met)


@dataclass
class MediaItem:
    """A watched media item with its matched torrents and evaluations."""

    arr: str            # "sonarr" | "radarr"
    arr_id: int
    title: str
    year: int | None = None
    media_type: str = "movie"   # "movie" | "series"
    watched: bool = True
    image_url: str | None = None
    torrents: list[TorrentDict] = field(default_factory=list)
    evaluations: list[SeedEvaluation] = field(default_factory=list)

    @property
    def seeding_complete(self) -> bool:
        """Safe only when every matched torrent is seeding-complete."""
        if not self.torrents:
            return False
        return all(ev.met and state not in {"error", "missingFiles"}
                   for ev, state in zip(self.evaluations, self._states()))

    def _states(self) -> list[str]:
        return [t.get("state", "") for t in self.torrents]

    @property
    def safe_to_delete(self) -> bool:
        return self.watched and self.seeding_complete


def _normalize_title(title: Any) -> str:
    """Lowercase, collapse whitespace/punctuation for fuzzy-but-safe matching."""
    s = re.sub(r"[^a-z0-9]+", " ", str(title or "")).strip().lower()
    return s


def _year_of(obj: dict[str, Any]) -> int | None:
    y = obj.get("year") or obj.get("ProductionYear") or obj.get("productionYear")
    return int(y) if y else None


def _tracker_domains(qbt: QBittorrentClient, info: TorrentInfo) -> list[str]:
    """Extract private-tracker hosts a torrent announces to (from qBit truth)."""
    try:
        trackers = qbt.torrent_trackers(info.hash)
    except Exception:
        trackers = []
    domains: list[str] = []
    for t in trackers:
        url = t.get("url") or ""
        if not url or url.startswith("**"):
            continue  # public/DHT/autodetected
        host = url.split("://")[-1].split("/")[0]
        if host and host not in domains:
            domains.append(host)
    return domains


def evaluate_torrent(qbt: QBittorrentClient, raw: dict[str, Any], config) -> SeedEvaluation:
    info = _torrent_to_info(raw)
    domains = _tracker_domains(qbt, info)
    # Prefer a domain that has configured limits.
    configured = [d for d in domains if d in config.data["trackers"]]
    domain = configured[0] if configured else (domains[0] if domains else None)
    target_ratio = 0.0
    target_time = 0.0
    if domain and domain in config.data["trackers"]:
        req = config.tracker_requirement(domain)
        target_ratio, target_time = req["target_ratio"], req["target_seed_time_minutes"]

    return SeedEvaluation(
        torrent_hash=info.hash,
        tracker_domain=domain,
        target_ratio=target_ratio,
        target_time_minutes=target_time,
        ratio=info.ratio,
        seeding_time_seconds=info.seeding_time,
        ratio_met=info.ratio >= target_ratio if target_ratio > 0 else False,
        time_met=info.seeding_time / 60 >= target_time if target_time > 0 else False,
    )


def _torrent_to_info(raw: dict[str, Any]) -> TorrentInfo:
    tags = raw.get("tags", "")
    return TorrentInfo(
        hash=raw.get("hash", ""),
        name=raw.get("name", ""),
        category=raw.get("category", ""),
        tags=tags.split(",") if isinstance(tags, str) else (tags or []),
        state=raw.get("state", ""),
        ratio=float(raw.get("ratio", 0) or 0),
        seeding_time=int(raw.get("seeding_time", 0) or 0),  # seconds
        size=int(raw.get("size", 0) or 0),
        save_path=raw.get("save_path", ""),
        content_path=raw.get("content_path", ""),
    )


def _build_title_index(entries: list[dict[str, Any]], id_key: str) -> dict[tuple[str, int | None], dict[str, Any]]:
    """Index arr movies/series by (normalized title, year) -> record."""
    index: dict[tuple[str, int | None], dict[str, Any]] = {}
    for e in entries:
        title = _normalize_title(e.get("title"))
        if not title:
            continue
        index[(title, _year_of(e))] = e
    return index


def _bridge_watched(jellyfin_item, arr_by_title: dict[tuple[str, int | None], dict[str, Any]]) -> dict[str, Any] | None:
    """Find the arr record for a Jellyfin watched item (normalized title+year)."""
    title = _normalize_title(jellyfin_item.get("Name") or jellyfin_item.get("name") or "")
    if not title:
        return None
    year = _year_of(jellyfin_item)
    # Exact year first, then any-year fallback (yearly mismatches happen).
    if (title, year) in arr_by_title:
        return arr_by_title[(title, year)]
    return next((rec for (t, _y), rec in arr_by_title.items() if t == title), None)


def build_index(
    qbt: QBittorrentClient,
    config,
    sonarr_history: list[dict[str, Any]],
    radarr_history: list[dict[str, Any]],
    radarr_movies: list[dict[str, Any]],
    sonarr_series: list[dict[str, Any]],
    watched_movies: list[dict[str, Any]],
    watched_series: list[dict[str, Any]],
) -> list[MediaItem]:
    """Assemble watched media items with their matched torrents + evaluations.

    Steps:
      1. Build arr-title index (Radarr movies, Sonarr series).
      2. Bridge each Jellyfin watched item to an arr record by title+year.
      3. Map arr history ``downloadId`` -> torrent hash -> real qBit torrents.
      4. Attach torrents to items and evaluate seeding completeness.
    """
    torrents = qbt.torrents()
    torrent_by_hash: dict[str, dict[str, Any]] = {t["hash"].lower(): t for t in torrents}

    # arr history: item (movieId/seriesId) -> list of torrent hashes grabbed.
    hashes_by_arr: dict[tuple[str, int], list[str]] = {}
    for h in sonarr_history:
        if h.get("seriesId"):
            hashes_by_arr.setdefault(("sonarr", int(h["seriesId"])), []).append((h.get("downloadId") or "").lower())
    for h in radarr_history:
        if h.get("movieId"):
            hashes_by_arr.setdefault(("radarr", int(h["movieId"])), []).append((h.get("downloadId") or "").lower())

    radarr_by_title = _build_title_index(radarr_movies, "id")
    sonarr_by_title = _build_title_index(sonarr_series, "id")

    items: list[MediaItem] = []

    for mov in watched_movies:
        arr_rec = _bridge_watched(mov, radarr_by_title)
        if arr_rec is None:
            continue  # watched in Jellyfin but not in Radarr (e.g. manually added)
        arr_id = int(arr_rec.get("id") or 0)
        hashes = hashes_by_arr.get(("radarr", arr_id), [])
        matched = [torrent_by_hash[h] for h in hashes if h in torrent_by_hash]
        item = MediaItem(
            arr="radarr",
            arr_id=arr_id,
            title=str(arr_rec.get("title") or mov.get("Name") or ""),
            year=_year_of(arr_rec) or _year_of(mov),
            media_type="movie",
            watched=True,
            image_url=_arr_poster(arr_rec),
            torrents=matched,
        )
        item.evaluations = [evaluate_torrent(qbt, t, config) for t in matched]
        items.append(item)

    for ser in watched_series:
        arr_rec = _bridge_watched(ser, sonarr_by_title)
        if arr_rec is None:
            continue
        arr_id = int(arr_rec.get("id") or 0)
        hashes = hashes_by_arr.get(("sonarr", arr_id), [])
        matched = [torrent_by_hash[h] for h in hashes if h in torrent_by_hash]
        item = MediaItem(
            arr="sonarr",
            arr_id=arr_id,
            title=str(arr_rec.get("title") or ser.get("Name") or ""),
            year=_year_of(arr_rec) or _year_of(ser),
            media_type="series",
            watched=True,
            image_url=_arr_poster(arr_rec),
            torrents=matched,
        )
        item.evaluations = [evaluate_torrent(qbt, t, config) for t in matched]
        items.append(item)

    # Cross-seed content-siblings are grouped here when a matched torrent shares
    # files with other torrents. The delete coordinator (delete.py) is the final
    # authority on file-set grouping; this keeps the list view straightforward.
    return items


def _arr_poster(arr_rec: dict[str, Any]) -> str | None:
    """Best-effort poster from the arr record (varies by arr version)."""
    for key in ("remotePoster", "posterPath", "images"):
        val = arr_rec.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val[0].get("url")
    return None