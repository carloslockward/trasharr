"""Matching qBittorrent torrents to Sonarr/Radarr media items and evaluating
whether their seeding requirement has been met.

Design notes
------------
* Matching is exact. The arr grab history records ``downloadId`` == the
  qBittorrent torrent hash, so a media item's torrents are attached by hash
  with no title guessing.
* Watched state is NOT part of the model: trasharr lists every arr item whose
  torrents have met their seeding requirement; the user picks what they have
  watched themselves.
* A torrent's seeding-complete status is computed in trasharr using per-tracker
  targets from config -- because the stack is deliberately configured to "no
  limit, seed forever", qBittorrent never stops a torrent on its own.
* The "met" rule mirrors private trackers: met when ratio >= target OR
  seed time >= target. Set an unused axis to 0. A tracker with no requirement
  configured is treated as complete.
* Tracker domains discovered on live torrents are auto-added to the config
  with blank (0/0) requirements so the user can fill them in via Settings.
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
    is_cross_seed: bool = False

    @property
    def met(self) -> bool:
        # A cross-seed copy was never downloaded from its tracker (the data was
        # grabbed once via the original torrent), so private trackers do not
        # count upload/download against it — its seeding limits don't apply.
        if self.is_cross_seed:
            return True
        ratio_required = self.target_ratio > 0
        time_required = self.target_time_minutes > 0
        if not ratio_required and not time_required:
            return True  # no requirement configured -> treated as complete
        return (ratio_required and self.ratio_met) or (time_required and self.time_met)


@dataclass
class MediaItem:
    """An arr media item with its matched torrents and evaluations."""

    arr: str            # "sonarr" | "radarr"
    arr_id: int
    title: str
    year: int | None = None
    media_type: str = "movie"   # "movie" | "series"
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
        return self.seeding_complete


def _normalize_title(title: Any) -> str:
    """Lowercase and collapse punctuation/whitespace; keep alphanumerics + spaces."""
    s = re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()
    return s


def _year_of(obj: dict[str, Any]) -> int | None:
    y = obj.get("year") or obj.get("ProductionYear") or obj.get("productionYear")
    return int(y) if y else None


def _tracker_domains(qbt: QBittorrentClient, info: TorrentInfo) -> list[str]:
    """Extract tracker hosts a torrent announces to (from qBit truth)."""
    try:
        trackers = qbt.torrent_trackers(info.hash)
    except Exception:
        trackers = []
    domains: list[str] = []
    for t in trackers:
        url = t.get("url") or ""
        if not url or url.startswith("**"):
            continue  # DHT/PeX/LSD pseudo-trackers
        host = url.split("://")[-1].split("/")[0]
        if host and host not in domains:
            domains.append(host)
    return domains


def discovered_tracker_domains(qbt: QBittorrentClient) -> list[str]:
    """Every distinct tracker host currently seen on live qBittorrent torrents.

    Used by the settings page to offer an autopopulated dropdown when adding a
    tracker; nothing is written to the config automatically.
    """
    domains: set[str] = set()
    for t in qbt.torrents():
        domains.update(_tracker_domains(qbt, _torrent_to_info(t)))
    return sorted(domains)


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

    # A tagged cross-seed copy was never downloaded from its tracker (the data
    # was grabbed once via the original torrent), so its seeding limits don't
    # apply — it is always treated as meeting requirements.
    is_cross_seed = config.cross_seed_tag() in info.tags

    return SeedEvaluation(
        torrent_hash=info.hash,
        tracker_domain=domain,
        target_ratio=target_ratio,
        target_time_minutes=target_time,
        ratio=info.ratio,
        seeding_time_seconds=info.seeding_time,
        ratio_met=info.ratio >= target_ratio if target_ratio > 0 else False,
        time_met=info.seeding_time / 60 >= target_time if target_time > 0 else False,
        is_cross_seed=is_cross_seed,
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


def _arr_poster(arr_rec: dict[str, Any], base_url: str) -> str | None:
    """Poster URL for an arr record, made absolute against the arr's base URL.

    The arr's ``images[].url`` is a server-relative path like
    ``/MediaCover/37/poster.jpg``; it only resolves when prefixed with the
    arr's own origin. ``remoteUrl`` (when present) is already absolute.
    """
    images = arr_rec.get("images")
    poster = None
    if isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("coverType") == "poster":
                poster = img
                break
        if poster is None and images and isinstance(images[0], dict):
            poster = images[0]
    if not poster:
        return None
    remote = poster.get("remoteUrl")
    if isinstance(remote, str) and remote:
        return remote
    url = poster.get("url")
    if isinstance(url, str) and url:
        if url.startswith("http"):
            return url
        if base_url:
            return f"{base_url.rstrip('/')}/{url.lstrip('/')}"
    return None


def _media_item(
    arr: str,
    arr_rec: dict[str, Any],
    base_url: str,
    matched: list[TorrentDict],
    qbt: QBittorrentClient,
    config,
) -> MediaItem:
    item = MediaItem(
        arr=arr,
        arr_id=int(arr_rec.get("id") or 0),
        title=str(arr_rec.get("title") or ""),
        year=_year_of(arr_rec),
        media_type="series" if arr == "sonarr" else "movie",
        image_url=_arr_poster(arr_rec, base_url),
        torrents=matched,
    )
    item.evaluations = [evaluate_torrent(qbt, t, config) for t in matched]
    return item


def build_index(
    qbt: QBittorrentClient,
    config,
    sonarr_history: list[dict[str, Any]],
    radarr_history: list[dict[str, Any]],
    radarr_movies: list[dict[str, Any]],
    sonarr_series: list[dict[str, Any]],
    radarr_base_url: str = "",
    sonarr_base_url: str = "",
) -> list[MediaItem]:
    """Assemble media items with their matched torrents + evaluations.

    Steps:
      1. Index live qBittorrent torrents by hash.
      2. Map arr grab-history ``downloadId`` -> torrent hash per arr item.
      3. Build a MediaItem for every arr item that has at least one live
         torrent (watched state is intentionally not considered).
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

    items: list[MediaItem] = []

    for rec in radarr_movies:
        arr_id = int(rec.get("id") or 0)
        hashes = hashes_by_arr.get(("radarr", arr_id), [])
        matched = [torrent_by_hash[h] for h in hashes if h in torrent_by_hash]
        if matched:
            items.append(_media_item("radarr", rec, radarr_base_url, matched, qbt, config))

    for rec in sonarr_series:
        arr_id = int(rec.get("id") or 0)
        hashes = hashes_by_arr.get(("sonarr", arr_id), [])
        matched = [torrent_by_hash[h] for h in hashes if h in torrent_by_hash]
        if matched:
            items.append(_media_item("sonarr", rec, sonarr_base_url, matched, qbt, config))

    # Cross-seed content-siblings are grouped by the delete coordinator
    # (delete.py), which is the final authority on file-set grouping.
    return items
