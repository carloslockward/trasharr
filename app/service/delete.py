"""The delete contract.

trasharr never touches media files directly. Deleting an item is an ordered,
atomic-looking sequence through the source services:

    1. VERIFY  — every torrent hosting the item's files (including cross-seed
                 content-siblings) is seeding-complete on every tracker that has
                 a configured requirement. If any is not, REFUSE. This is the
                 safety net that keeps trasharr from ever violating a tracker's
                 rule.
    2. UNMONITOR — set monitored=False on the Sonarr/Radarr item so the arr does
                 not re-grab it after removal.
    3. DELETE FILES — call the arr's delete with deleteFiles=true, removing the
                 media through the arr's normal path (keeps its DB + metadata).
    4. REMOVE — delete every qBit torrent hosting the files (the arr-hash
                 torrent plus its cross-seed content-siblings).

Deletion is performed one item at a time; a failure in a step aborts that item
and is surfaced so the caller can report it — we do not silently continue past a
refused or errored item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .matcher import MediaItem, evaluate_torrent
from ..clients.qbittorrent import QBittorrentClient


@dataclass
class DeletionResult:
    item_title: str
    ok: bool
    message: str
    removed_hashes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


class DeletionRefused(Exception):
    """Raised when an item cannot be safely deleted (seeding not complete)."""


class _ArrClient:
    """Structural min: the delete coordinator uses unmonitor() + delete_files().

    Instance attrs are resolved from the passed arr clients at call time.
    """


def _content_sibling_hashes(qbt: QBittorrentClient, all_torrents: list[dict[str, Any]],
                            primary_hashes: set[str]) -> list[dict[str, Any]]:
    """Return every torrent that shares files with the given primary torrents.

    Matches on ``content_path`` equality first (the common cross-seed/hardlink
    layout: separate torrents, same on-disk content), falling back to exact
    top-level save-path equality. This is the "content match, not name match"
    rule agreed in the design.
    """
    by_hash = {t["hash"]: t for t in all_torrents}
    primary = [by_hash[h] for h in primary_hashes if h in by_hash]
    primary_content = {t.get("content_path") for t in primary if t.get("content_path")}
    primary_save = {t.get("save_path") for t in primary if t.get("save_path")}

    siblings: list[dict[str, Any]] = []
    for t in all_torrents:
        h = t.get("hash")
        if h in primary_hashes:
            continue
        cp = t.get("content_path")
        sp = t.get("save_path")
        if cp and cp in primary_content:
            siblings.append(t)
        elif sp and sp in primary_save:
            siblings.append(t)
    return siblings


class DeleteCoordinator:
    def __init__(self, qbt: QBittorrentClient, sonarr, radarr, config) -> None:
        self.qbt = qbt
        self.sonarr = sonarr
        self.radarr = radarr
        self.config = config

    def _arr_for(self, item: MediaItem):
        return self.radarr if item.arr == "radarr" else self.sonarr

    def _verify_complete(self, item: MediaItem, hosts: list[dict[str, Any]]) -> None:
        """Refuse the delete unless every file-hosting torrent is seed-complete.

        Every host (primary + content-siblings) must satisfy its tracker's
        requirement. A single non-compliant host blocks the whole delete.
        """
        bad: list[str] = []
        for t in hosts:
            ev = evaluate_torrent(self.qbt, t, self.config)
            if not ev.met or t.get("state") in {"error", "missingFiles", "pausedDL"}:
                ratio_req = f"ratio {ev.target_ratio}" if ev.target_ratio > 0 else ""
                time_req = f"{int(ev.target_time_minutes)}min" if ev.target_time_minutes > 0 else ""
                req = " and ".join(filter(None, [ratio_req, time_req])) or "no configured limit"
                bad.append(f"{t.get('name', t.get('hash'))} ({ev.tracker_domain or 'unknown tracker'}, need {req}, "
                           f"have ratio {ev.ratio:.2f} / {ev.seeding_time_seconds // 60}min)")
        if bad:
            raise DeletionRefused("; ".join(bad))

    def delete_item(self, item: MediaItem) -> DeletionResult:
        steps: list[str] = []
        primary_hashes = {t["hash"] for t in item.torrents}
        if not primary_hashes:
            # Watched but no torrent attached (rare) — still let the arr delete.
            primary_hashes = set()

        all_torrents = self.qbt.torrents()
        siblings = _content_sibling_hashes(self.qbt, all_torrents, primary_hashes)
        host_hashes = primary_hashes | {t["hash"] for t in siblings}
        hosts = [t for t in all_torrents if t["hash"] in host_hashes]

        # 1. Verify every host is seed-complete (the safety gate).
        self._verify_complete(item, hosts)

        arr = self._arr_for(item)

        # 2. Unmonitor so the arr doesn't re-grab.
        try:
            arr.unmonitor(item.arr_id)
            steps.append(f"unmonitored {item.arr}:{item.arr_id}")
        except Exception as exc:
            raise RuntimeError(f"failed to unmonitor: {exc}") from exc

        # 3. Delete media files through the arr.
        try:
            arr.delete_files(item.arr_id)
            steps.append(f"deleted files via {item.arr}")
        except Exception as exc:
            raise RuntimeError(f"failed to delete files via {item.arr}: {exc}") from exc

        # 4. Remove every hosting torrent from qBittorrent (with files).
        removed: list[str] = []
        for h in host_hashes:
            try:
                self.qbt.delete_files(h, delete_files=True)
                removed.append(h)
            except Exception as exc:
                steps.append(f"qbit remove {h} failed: {exc}")
        steps.append(f"removed {len(removed)} torrent(s) from qBittorrent")

        return DeletionResult(
            item_title=item.title,
            ok=True,
            message="deleted",
            removed_hashes=removed,
            steps=steps,
        )

    def delete_many(self, items: list[MediaItem]) -> list[DeletionResult]:
        """Delete several items; each is isolated (one failure doesn't stop others)."""
        results: list[DeletionResult] = []
        for item in items:
            try:
                results.append(self.delete_item(item))
            except DeletionRefused as exc:
                results.append(DeletionResult(item.title, ok=False, message=f"refused: {exc}"))
            except Exception as exc:
                results.append(DeletionResult(item.title, ok=False, message=str(exc)))
        return results