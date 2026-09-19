"""Matching qBittorrent torrents to Sonarr/Radarr media items and evaluating
whether their seeding requirement has been met.

Design notes
------------
* Matching is exact. The arr grab history records ``downloadId`` == the
  qBittorrent torrent hash, so a media item's torrents are attached by hash
  with no title guessing.
* trasharr lists every arr media with live torrents; the user decides what is
  safe to clear.
* A torrent's seeding-complete status is computed in trasharr using per-tracker
  targets from config -- because the stack is deliberately configured to "no
  limit, seed forever", qBittorrent never stops a torrent on its own.
* The "met" rule mirrors private trackers: met when ratio >= target OR
  seed time >= target. Set an unused axis to 0. A tracker with no requirement
  configured is treated as complete.
* Orphaned torrents: arr-category torrents (``tv-sonarr`` / ``radarr`` /
  ``cross-seed-link``) whose hash appears in neither arr's grab history and
  whose content no longer maps to any media item. They are grouped one card
  per content_path (identical content = one deletable unit) and deleted from
  qBittorrent only.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ..clients.qbittorrent import QBittorrentClient
from ..clients.radarr import RadarrClient
from ..clients.sonarr import SonarrClient

logger = logging.getLogger(__name__)

TorrentDict = dict[str, Any]

# qBittorrent categories that mean "this torrent belongs to an arr". The
# cross-seed tool copies the arr category under its own tag name.
ARR_CATEGORIES = {"tv-sonarr", "radarr", "cross-seed-link"}


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
    size_bytes: int = 0         # disk usage as reported by the arr (0 = unknown)
    torrents: list[TorrentDict] = field(default_factory=list)
    evaluations: list[SeedEvaluation] = field(default_factory=list)
    # True when the arr's grab history exists but no live torrent remains:
    # nothing is seeding, so the item is trivially safe to delete.
    no_live_torrents: bool = False
    # True for orphaned torrents: arr-category torrents in qBittorrent that no
    # longer belong to any Sonarr/Radarr item. Deleted from qBittorrent only.
    orphan: bool = False
    # Unique id of this file-set card (derived from its torrents' hashes).
    # Multiple file-set cards of the same media share arr:arr_id, so selection
    # keys use this id: <arr>:<arr_id>:<card_id>.
    card_id: str = ""
    # True when this file-set contains a history-hash torrent — i.e. the group
    # the arr actually grabbed. Only such cards may unmonitor/delete via the
    # arr; stray cross-seed copies and orphans are qBittorrent-only.
    has_arr_authority: bool = False
    # Tracked arr files of this media that NO torrent covers (torrent long
    # gone). One catch-all "leftover files" card per media; the delete
    # contract removes exactly these arr files — no qBittorrent step, no
    # verification gate (nothing is seeding).
    leftover_files: list[dict[str, Any]] = field(default_factory=list)

    @property
    def seeding_complete(self) -> bool:
        """Safe only when every matched torrent is seeding-complete."""
        if self.no_live_torrents:
            return True
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


def _torrent_tags(t: TorrentDict) -> list[str]:
    tags = t.get("tags", "")
    if isinstance(tags, str):
        return [x.strip() for x in tags.split(",") if x.strip()]
    return list(tags or [])


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
    # apply — it is always treated as meeting requirements. Filesystem
    # evidence (hardlink detected by build_index and stamped on the dict)
    # takes precedence over the tag.
    is_cross_seed = bool(raw.get("_xseed_fs")) or config.cross_seed_tag() in info.tags

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


def _arr_size_bytes(arr_rec: dict[str, Any]) -> int:
    """Disk usage reported by the arr: Radarr's ``sizeOnDisk`` or Sonarr's
    ``statistics.sizeOnDisk``. 0 when the item has no files on disk."""
    size = arr_rec.get("sizeOnDisk")
    if size is None:
        stats = arr_rec.get("statistics") or {}
        size = stats.get("sizeOnDisk")
    try:
        return int(size or 0)
    except (TypeError, ValueError):
        return 0


def _media_item(
    arr: str,
    arr_rec: dict[str, Any],
    base_url: str,
    matched: list[TorrentDict],
    qbt: QBittorrentClient,
    config,
    seeding_complete: bool = False,
) -> MediaItem:
    item = MediaItem(
        arr=arr,
        arr_id=int(arr_rec.get("id") or 0),
        title=str(arr_rec.get("title") or ""),
        year=_year_of(arr_rec),
        media_type="series" if arr == "sonarr" else "movie",
        image_url=_arr_poster(arr_rec, base_url),
        size_bytes=_arr_size_bytes(arr_rec),
        torrents=matched,
    )
    item.evaluations = [evaluate_torrent(qbt, t, config) for t in matched]
    if seeding_complete:
        item.no_live_torrents = True
    return item


def _norm_release(name: Any) -> str:
    """Normalized release name for cross-seed matching: file extension
    stripped, lowercase, punctuation collapsed. Cross-seed copies carry the
    same release name as the original torrent (often with a trailing
    ``.mkv``), but live under a different content_path — name is the link."""
    s = str(name or "")
    if s.lower().endswith((".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".iso")):
        s = s[: s.rfind(".")]
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _physical_ids(content_path: str) -> set[tuple[int, int]] | None:
    """Physical on-disk identity of a torrent's content: the (st_dev, st_ino)
    pair of every file under ``content_path``.

    Hardlinks (the cross-seed tool's default link method) make two directory
    entries share an inode, so intersecting id sets prove the torrents host
    the *same bytes* — regardless of content_path. Returns None when the path
    is not visible to this process (caller falls back to content_path
    grouping).
    """
    try:
        st = os.stat(content_path)
    except OSError:
        return None
    if os.path.isfile(content_path):  # single-file torrent
        return {(st.st_dev, st.st_ino)}
    ids: set[tuple[int, int]] = set()
    for root, _dirs, files in os.walk(content_path):
        for f in files:
            try:
                fst = os.stat(os.path.join(root, f))
                ids.add((fst.st_dev, fst.st_ino))
            except OSError:
                continue
    return ids


def _union(groups: dict[int, list[TorrentDict]], a: int, b: int,
           parent: dict[int, int]) -> None:
    """Merge groups a and b in a tiny union-find over group keys."""
    ra, rb = a, b
    while parent[ra] != ra:
        ra = parent[ra]
    while parent[rb] != rb:
        rb = parent[rb]
    if ra != rb:
        parent[rb] = ra


def card_file_identity(torrents: list[TorrentDict], qbt: QBittorrentClient,
                       ) -> tuple[set[tuple[int, int]], set[str], set[int]]:
    """Physical identity of a set of torrents' files: {(dev, ino)}, lowercase
    basenames, and file sizes. Inodes drive exact arr-file matching
    (hardlink-safe); basenames cover renamed-but-hardlinked files; sizes cover
    renamed-and-copied files (Radarr renames on import, and with "copy instead
    of hardlink" the inode differs). Sizes are ambiguous alone — the matcher
    only accepts a UNIQUE size match."""
    inodes: set[tuple[int, int]] = set()
    names: set[str] = set()
    sizes: set[int] = set()
    for t in torrents:
        cp = t.get("content_path") or ""
        if not cp:
            continue
        # qBittorrent's own file listing is authoritative for sizes even
        # when the on-disk path isn't statable (different mount view).
        h = t.get("hash")
        if h:
            try:
                for tf in qbt.torrent_files(h):
                    sizes.add(int(tf.get("size") or 0))
            except Exception:
                pass
        try:
            st = os.stat(cp)
        except OSError:
            continue
        if os.path.isfile(cp):
            inodes.add((st.st_dev, st.st_ino))
            names.add(os.path.basename(cp).lower())
            continue
        for root, _dirs, files in os.walk(cp):
            for f in files:
                p = os.path.join(root, f)
                try:
                    fst = os.stat(p)
                    inodes.add((fst.st_dev, fst.st_ino))
                except OSError:
                    pass
                names.add(f.lower())
    return inodes, names, sizes


def match_arr_files(arr_files: list[dict[str, Any]],
                    identity: tuple[set[tuple[int, int]], set[str], set[int]],
                    ) -> list[dict[str, Any]]:
    """Arr file records that belong to the given card identity. Match order:
    inode equality (exact, hardlink-aware) -> exact basename -> unique size.
    A size match is only accepted when exactly ONE arr file has that size
    (otherwise it's ambiguous and we skip it). No other fuzzy matching."""
    inodes, names, sizes = identity
    matched: list[dict[str, Any]] = []
    for f in arr_files:
        p = f.get("path") or ""
        if not p:
            continue
        try:
            st = os.stat(p)
            if (st.st_dev, st.st_ino) in inodes:
                matched.append(f)
                continue
        except OSError:
            pass
        if os.path.basename(p).lower() in names:
            matched.append(f)
            continue
        fsize = int(f.get("size") or 0)
        if fsize > 0 and fsize in sizes:
            same_size = [g for g in arr_files if int(g.get("size") or 0) == fsize]
            if len(same_size) == 1:
                matched.append(f)
    return matched


def _disk_usage_bytes(paths: list[str]) -> int | None:
    """Real on-disk bytes of a set of paths, deduplicated by physical file.

    Walks each path (single file or directory), sums the size of every unique
    (st_dev, st_ino) — hardlinks shared between paths are counted once. Returns
    None when no path is statable (caller falls back to qBittorrent sizes).
    """
    seen: dict[tuple[int, int], int] = {}
    for p in paths:
        if not p:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        if os.path.isfile(p):
            seen[(st.st_dev, st.st_ino)] = st.st_size
            continue
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    fst = os.stat(os.path.join(root, f))
                    seen.setdefault((fst.st_dev, fst.st_ino), fst.st_size)
                except OSError:
                    continue
    if not seen:
        return None
    return sum(seen.values())


def primary_hash(group: list[TorrentDict]) -> str:
    """Stable group id: first torrent's hash (groups are deterministic)."""
    return str(group[0].get("hash") or "").lower()


def build_index(
    qbt: QBittorrentClient,
    config,
    sonarr_history: list[dict[str, Any]],
    radarr_history: list[dict[str, Any]],
    radarr_movies: list[dict[str, Any]],
    sonarr_series: list[dict[str, Any]],
    radarr_base_url: str = "",
    sonarr_base_url: str = "",
    orphan_detection: bool = True,
    sonarr: SonarrClient | None = None,
    radarr: RadarrClient | None = None,
) -> list[MediaItem]:
    """Assemble the grid: one card per content_path (one deletable file-set).

    The unit of the grid is a FILE-SET, not a media item. Every arr-category
    torrent in qBittorrent is grouped by its content_path (empty content_path
    -> grouped by hash); each group becomes one card:

      * If any torrent in the group has a grab-history hash, the group resolves
        to the arr item that grabbed it: title/poster/year/size come from the
        arr record and the delete contract runs the full sequence (unmonitor +
        arr deleteFiles). Duplicates of the same media (original + cross-seed
        copies) legitimately appear as separate cards — separate file-sets.
      * Otherwise the group is an orphan ("Not in Sonarr/Radarr") — e.g. a
        cross-seed copy whose original movie was already removed — and its
        delete contract is qBittorrent-only.

    ``orphan_detection`` must be False when an arr history fetch failed (an
    empty/partial history would mislabel live torrents). In that case the grid
    falls back to the media-item view built from the arr catalogs.
    """
    torrents = qbt.torrents()

    radarr_by_id = {int(m.get("id") or 0): m for m in radarr_movies}
    sonarr_by_id = {int(s.get("id") or 0): s for s in sonarr_series}

    # history: hash -> (arr, record id), AND normalized sourceTitle -> (arr, rid)
    # (the latter resolves cross-seed copies that were never grabbed: their
    # release name equals a history record's sourceTitle).
    hash_owner: dict[str, tuple[str, int]] = {}
    for h in sonarr_history:
        if h.get("seriesId"):
            hash_owner[(h.get("downloadId") or "").lower()] = ("sonarr", int(h["seriesId"]))
    for h in radarr_history:
        if h.get("movieId"):
            hash_owner[(h.get("downloadId") or "").lower()] = ("radarr", int(h["movieId"]))
    name_owner: dict[str, tuple[str, int]] = {}
    for h in sonarr_history:
        if h.get("seriesId"):
            name_owner.setdefault(_norm_release(h.get("sourceTitle")), ("sonarr", int(h["seriesId"])))
    for h in radarr_history:
        if h.get("movieId"):
            name_owner.setdefault(_norm_release(h.get("sourceTitle")), ("radarr", int(h["movieId"])))

    # 1. Group live arr-category torrents into physical file-sets. Base
    # grouping is by content_path; torrents whose on-disk identity
    # ((dev, inode) sets) intersects another torrent's are merged — a
    # hardlinked cross-seed copy IS the same file, so original + copy become
    # one card. When the filesystem isn't visible (paths unmounted), identity
    # is None and we fall back to content_path-only grouping.
    arr_torrents = [t for t in torrents
                    if (t.get("category") or "") in ARR_CATEGORIES]
    groups: dict[int, list[TorrentDict]] = {}
    parent: dict[int, int] = {}
    ids_by_idx: dict[int, set[tuple[int, int]] | None] = {}
    for i, t in enumerate(arr_torrents):
        parent[i] = i
        groups[i] = [t]
        ids_by_idx[i] = _physical_ids(t.get("content_path") or "")
    # index physical ids -> torrent indices that own them, to find overlaps
    inode_owners: dict[tuple[int, int], list[int]] = {}
    for i, ids in ids_by_idx.items():
        if ids:
            for pid in ids:
                inode_owners.setdefault(pid, []).append(i)
    # union torrents sharing a physical file
    for pid, owners in inode_owners.items():
        for other in owners[1:]:
            _union(groups, owners[0], other, parent)
    # content_path fallback links (also handles identity=None torrents)
    by_content: dict[str, int] = {}
    for i, t in enumerate(arr_torrents):
        cp = t.get("content_path") or ""
        key = cp if cp else f"hash:{t['hash'].lower()}"
        if key in by_content:
            _union(groups, by_content[key], i, parent)
        else:
            by_content[key] = i
    # resolve union-find into final groups
    final_groups: dict[int, list[TorrentDict]] = {}
    for i in range(len(arr_torrents)):
        root = i
        while parent[root] != root:
            root = parent[root]
        final_groups.setdefault(root, []).append(arr_torrents[i])
    merged_groups = list(final_groups.values())
    # Mark evidence-based cross-seeds inside each group. A member whose hash
    # the arr grabbed (or, failing that, one sharing its content_path) is the
    # "original"; any other member at a DIFFERENT content_path is a hardlink
    # copy — physically the same bytes, so its tracker's seeding limits don't
    # apply. Evidence is stamped on the torrent dict (_xseed_fs) so the delete
    # path's evaluate_torrent() sees the same verdict. The configured
    # cross_seed_tag remains the fallback when no authority member exists.
    for g in merged_groups:
        auth_members = [t for t in g
                        if hash_owner.get((t.get("hash") or "").lower())]
        auth_paths = {t.get("content_path") for t in auth_members} or None
        tag = config.cross_seed_tag()
        for t in g:
            is_copy = (
                bool(auth_members)
                and t.get("content_path") not in (auth_paths or {None})
            ) or (tag in _torrent_tags(t) and not auth_members)
            if is_copy:
                t["_xseed_fs"] = True
        linked = len(g) > 1 and any("_xseed_fs" in t for t in g)
        if linked:
            logger.info("file-set merged: %d torrent(s) share physical files (%s)",
                        len(g), g[0].get("name"))

    # 2. One card per group, resolved to its media when possible.
    items: list[MediaItem] = []
    for group in merged_groups:
        card_id = primary_hash(group)[:8]
        owner = next(
            (o for o in (hash_owner.get(t["hash"].lower()) for t in group) if o),
            None,
        )
        if owner is not None and orphan_detection:
            arr, rid = owner
            rec = (radarr_by_id if arr == "radarr" else sonarr_by_id).get(rid)
            if rec is None:
                # The arr item was deleted since the grab (e.g. by a previous
                # card's delete). Fall back to the torrent's own name.
                rec = {"title": str(group[0].get("name") or "Unknown")}
            items.append(_file_set_card(arr, rid, rec, group, qbt, config,
                                        radarr_base_url, sonarr_base_url,
                                        card_id=card_id))
            continue

        # No history hash in this group (cross-seed copy or orphan): resolve
        # media by matching the release name against grab-history sourceTitles
        # (cross-seed copies share the original's release name). If still
        # unresolved, the card is an orphan. Resolution is cosmetic — the
        # delete contract stays qBittorrent-only for these cards.
        owner = None
        if orphan_detection:
            owner = next(
                (o for o in (name_owner.get(_norm_release(t.get("name"))) for t in group) if o),
                None,
            )
        if owner is not None:
            arr, rid = owner
            rec = (radarr_by_id if arr == "radarr" else sonarr_by_id).get(rid)
            if rec is not None:
                items.append(_file_set_card(arr, rid, rec, group, qbt, config,
                                            radarr_base_url, sonarr_base_url,
                                            resolved_by_name=True, card_id=card_id))
                continue
        items.append(_orphan_file_set(group, qbt, config))

    # 3. Arr items whose grabbed torrents are all gone: nothing seeding, but
    # the arr still holds the library entry (files may or may not exist).
    if orphan_detection:
        touched: set[tuple[str, int]] = set()
        for t in torrents:
            if (t.get("category") or "") in ARR_CATEGORIES:
                continue
            # skip: only arr-category torrents exist in this stack, but be safe
        for group in groups.values():
            for t in group:
                owner = hash_owner.get(t["hash"].lower())
                if owner:
                    touched.add(owner)
        presented = {(i.arr, i.arr_id) for i in items if not i.orphan}
        for (arr, rid) in sorted(touched):
            if (arr, rid) in presented:
                continue  # a file-set card already carries this item's media
            rec = (radarr_by_id if arr == "radarr" else sonarr_by_id).get(rid)
            if rec is not None:
                items.append(_media_item(arr, rec,
                                         radarr_base_url if arr == "radarr" else sonarr_base_url,
                                         [], qbt, config, seeding_complete=True))
    else:
        # Arr fetch failed: simple catalog fallback (no orphan logic at all).
        for rec in radarr_movies:
            items.append(_media_item("radarr", rec, radarr_base_url, [], qbt, config, seeding_complete=True))
        for rec in sonarr_series:
            items.append(_media_item("sonarr", rec, sonarr_base_url, [], qbt, config, seeding_complete=True))

    # 4. Leftover files: arr-tracked files of a media that NO torrent covers
    # (the download is long gone from qBittorrent — e.g. a season deleted
    # before trasharr existed). One catch-all card per media lists them so
    # they become deletable. Uses the same inode/basename/size matching in
    # reverse: any tracked file no torrent card covers is a leftover. Runs
    # best-effort: an arr file-listing failure skips that media silently.
    if orphan_detection:
        media_cards: dict[tuple[str, int], list[MediaItem]] = {}
        for it in items:
            if not it.orphan and it.arr_id:
                media_cards.setdefault((it.arr, it.arr_id), []).append(it)
        catalogs: dict[str, dict[int, dict[str, Any]]] = {
            "radarr": radarr_by_id, "sonarr": sonarr_by_id}
        clients: dict[str, Any] = {"radarr": radarr, "sonarr": sonarr}
        for (arr, rid), cards in media_cards.items():
            client = clients.get(arr)
            if client is None:
                continue
            rec = catalogs["radarr" if arr == "radarr" else "sonarr"].get(rid)
            if rec is None:
                continue
            try:
                arr_files = client.media_files(int(rid))
            except Exception as exc:
                logger.info("leftover scan skipped for %s:%s (file listing failed: %s)",
                            arr, rid, exc)
                continue
            if not arr_files:
                continue
            identity = card_file_identity(
                [t for c in cards for t in c.torrents], qbt)
            covered_ids = {int(f.get("id") or 0) for f in match_arr_files(arr_files, identity)}
            uncovered = [f for f in arr_files if int(f.get("id") or 0) not in covered_ids]
            if uncovered:
                items.append(_leftover_card(arr, int(rid), rec, uncovered,
                                            radarr_base_url if arr == "radarr" else sonarr_base_url))

    return items


def _leftover_card(
    arr: str,
    arr_id: int,
    rec: dict[str, Any],
    uncovered: list[dict[str, Any]],
    base_url: str,
) -> MediaItem:
    """Catch-all card for a media's arr-tracked files that no torrent covers.

    Nothing is seeding for these files, so the card is trivially safe; the
    delete contract deletes exactly these arr files (then unmonitors if
    nothing tracked remains). One card per media, whatever the file count.
    """
    size = sum(int(f.get("size") or 0) for f in uncovered)
    item = MediaItem(
        arr=arr,
        arr_id=arr_id,
        title=str(rec.get("title") or "Unknown"),
        year=_year_of(rec),
        media_type="series" if arr == "sonarr" else "movie",
        image_url=_arr_poster(rec, base_url),
        size_bytes=size,
        torrents=[],
    )
    item.no_live_torrents = True
    item.has_arr_authority = True
    item.leftover_files = [
        {"id": int(f.get("id") or 0), "path": f.get("path") or "",
         "size": int(f.get("size") or 0)}
        for f in uncovered
    ]
    # selection keys are arr:arr_id:card_id — one leftover card per media
    item.card_id = "leftover"
    return item


def _file_set_card(
    arr: str,
    arr_id: int,
    rec: dict[str, Any],
    group: list[TorrentDict],
    qbt: QBittorrentClient,
    config,
    radarr_base_url: str,
    sonarr_base_url: str,
    resolved_by_name: bool = False,
    card_id: str = "",
) -> MediaItem:
    """A file-set card resolved to the arr item that grabbed it."""
    base_url = radarr_base_url if arr == "radarr" else sonarr_base_url
    # Card size = the physical files this file-set occupies (unique inodes,
    # hardlinked members counted once), NOT the arr's whole-item sizeOnDisk —
    # a series with 15 episode file-sets must not show the series total on
    # every card. Falls back to the largest torrent size when paths are
    # invisible to this process.
    usage = _disk_usage_bytes([t.get("content_path") or "" for t in group])
    size = usage if usage is not None else max(
        (int(t.get("size") or 0) for t in group), default=0)
    item = MediaItem(
        arr=arr,
        arr_id=int(rec.get("id") or 0),
        title=str(rec.get("title") or group[0].get("name") or "Unknown"),
        year=_year_of(rec),
        media_type="series" if arr == "sonarr" else "movie",
        image_url=_arr_poster(rec, base_url),
        size_bytes=size,
        torrents=group,
    )
    item.card_id = card_id
    item.has_arr_authority = not resolved_by_name
    item.evaluations = [evaluate_torrent(qbt, t, config) for t in group]
    return item


def _orphan_file_set(group: list[TorrentDict], qbt: QBittorrentClient, config) -> MediaItem:
    """A file-set with no arr owner: 'Not in Sonarr/Radarr' card."""
    primary = max(group, key=lambda t: len(str(t.get("name") or "")))
    # stable unique id from the group's primary hash so selection keys
    # (orphan:<id>) never collide between orphan cards
    orphan_id = int(primary["hash"][:8], 16)
    # real on-disk usage of this file-set (hardlinks counted once); falls
    # back to the largest torrent size when paths aren't visible
    usage = _disk_usage_bytes([t.get("content_path") or "" for t in group])
    size = usage if usage is not None else max(
        (int(t.get("size") or 0) for t in group), default=0)
    item = MediaItem(
        arr="orphan",
        arr_id=orphan_id,
        title=str(primary.get("name") or primary.get("hash") or "Unknown torrent"),
        year=None,
        media_type="orphan",
        image_url=None,
        size_bytes=size,
        torrents=group,
    )
    item.orphan = True
    item.card_id = primary_hash(group)[:8]
    item.evaluations = [evaluate_torrent(qbt, t, config) for t in group]
    return item
