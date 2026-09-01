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

DRY RUN
-------
Set ``TRASHARR_DRY_RUN=1`` to run the *entire* contract without executing any
destructive call. Every step is verified and logged (including which cross-seed
siblings would be removed), so a dry run shows exactly what a real delete would
do — including refusals — while touching nothing.
"""

from __future__ import annotations

import logging
import os

from dataclasses import dataclass, field
from typing import Any

from .matcher import MediaItem, evaluate_torrent
from ..clients.qbittorrent import QBittorrentClient

logger = logging.getLogger(__name__)


def dry_run_enabled() -> bool:
    return os.environ.get("TRASHARR_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class DeletionResult:
    item_title: str
    ok: bool
    message: str
    removed_hashes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


class DeletionRefused(Exception):
    """Raised when an item cannot be safely deleted (seeding not complete)."""


def _content_sibling_hashes(qbt: QBittorrentClient, all_torrents: list[dict[str, Any]],
                            primary_hashes: set[str]) -> list[dict[str, Any]]:
    """Return every torrent that shares *content* with the given primary torrents.

    Matches on ``content_path`` equality only (the cross-seed/hardlink layout:
    separate torrents, same on-disk content). Deliberately NOT save_path: two
    unrelated season packs dropped into the same download directory share a
    save path but no content, and removing them together would delete other
    people's torrents.
    """
    by_hash = {t["hash"]: t for t in all_torrents}
    primary = [by_hash[h] for h in primary_hashes if h in by_hash]
    primary_content = {t.get("content_path") for t in primary if t.get("content_path")}

    siblings: list[dict[str, Any]] = []
    for t in all_torrents:
        h = t.get("hash")
        if h in primary_hashes:
            continue
        cp = t.get("content_path")
        if cp and cp in primary_content:
            siblings.append(t)
    return siblings


class DeleteCoordinator:
    def __init__(self, qbt: QBittorrentClient, sonarr, radarr, config) -> None:
        self.qbt = qbt
        self.sonarr = sonarr
        self.radarr = radarr
        self.config = config
        self.dry_run = dry_run_enabled()

    def _arr_for(self, item: MediaItem):
        return self.radarr if item.arr == "radarr" else self.sonarr

    def _verify_complete(self, item: MediaItem, hosts: list[dict[str, Any]], force: bool = False) -> None:
        """Refuse the delete unless every file-hosting torrent is seed-complete.

        Every host (primary + content-siblings) must satisfy its tracker's
        requirement. A single non-compliant host blocks the whole delete —
        unless ``force`` is set (the UI's explicit "requirements not met, I'm
        sure" override), in which case violations are logged but not blocking.
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
            logger.info("full refusal list for '%s' (%d torrent(s) not meeting targets):", item.title, len(bad))
            for entry in bad:
                logger.info("  refused: %s", entry)
            if force:
                logger.warning("FORCED delete of '%s' with %d torrent(s) not meeting their targets — proceeding by user override.",
                               item.title, len(bad))
                return
            # Keep the message readable: name the first few offenders, summarize the rest.
            max_shown = 5
            shown = bad[:max_shown]
            extra = len(bad) - len(shown)
            summary = "; ".join(shown)
            if extra > 0:
                summary += f"; …and {extra} more torrent(s) not meeting their targets (see server log for the full list)"
            raise DeletionRefused(summary)

    def delete_item(self, item: MediaItem, force: bool = False) -> DeletionResult:
        steps: list[str] = []
        log = logger.info
        dry = self.dry_run
        prefix = "[DRY RUN] " if dry else ""

        primary_hashes = {t["hash"] for t in item.torrents}

        all_torrents = self.qbt.torrents()
        siblings = _content_sibling_hashes(self.qbt, all_torrents, primary_hashes)
        host_hashes = primary_hashes | {t["hash"] for t in siblings}
        hosts = [t for t in all_torrents if t["hash"] in host_hashes]

        log("%sintent to delete '%s' (%s:%s, %s)%s from storage",
            prefix, item.title, item.arr, item.arr_id, item.year,
            " [FORCED — requirements not met]" if force else "")
        log("%sfound %d primary torrent(s) for %s", prefix, len(primary_hashes), item.title)
        log("%sfound %d cross-seed sibling torrent(s) for %s", prefix, len(siblings), item.title)
        for t in siblings:
            log("%s  cross-seed sibling: %s (hash %s)", prefix, t.get("name"), t.get("hash"))

        # 1. Verify every host is seed-complete (the safety gate). Runs the same
        # in dry-run mode so refusals can be observed without side effects.
        # `force` (user override) downgrades violations to warnings.
        self._verify_complete(item, hosts, force=force)

        arr = self._arr_for(item)

        # 2. Unmonitor so the arr doesn't re-grab.
        if dry:
            log("%swould unmonitor %s item id=%s (%s)", prefix, item.arr, item.arr_id, item.title)
            steps.append(f"[dry-run] would unmonitor {item.arr}:{item.arr_id}")
        else:
            try:
                arr.unmonitor(item.arr_id)
                log("%sremoved %s from %s (unmonitored, id=%s)", prefix, item.title, item.arr, item.arr_id)
                steps.append(f"unmonitored {item.arr}:{item.arr_id}")
            except Exception as exc:
                raise RuntimeError(f"failed to unmonitor: {exc}") from exc

        # 3. Delete media files through the arr.
        if dry:
            log("%swould delete storage files for '%s' via %s (deleteFiles=true)", prefix, item.title, item.arr)
            steps.append(f"[dry-run] would delete files via {item.arr}")
        else:
            try:
                arr.delete_files(item.arr_id)
                log("%sdeleted storage files for '%s' via %s", prefix, item.title, item.arr)
                steps.append(f"deleted files via {item.arr}")
            except Exception as exc:
                raise RuntimeError(f"failed to delete files via {item.arr}: {exc}") from exc

        # 4. Remove every hosting torrent from qBittorrent (with files).
        removed: list[str] = []
        if dry:
            log("%swould remove %d torrent(s) from qBittorrent (deleteFiles=true):", prefix, len(host_hashes))
            for t in hosts:
                log("%s  would remove torrent: %s (hash %s)", prefix, t.get("name"), t.get("hash"))
            steps.append(f"[dry-run] would remove {len(host_hashes)} torrent(s) from qBittorrent")
        else:
            for h in host_hashes:
                try:
                    self.qbt.delete_files(h, delete_files=True)
                    removed.append(h)
                except Exception as exc:
                    steps.append(f"qbit remove {h} failed: {exc}")
            log("%sremoved %d torrent(s) from qBittorrent", prefix, len(removed))
            steps.append(f"removed {len(removed)} torrent(s) from qBittorrent")

        message = "dry-run: delete verified, nothing was touched" if dry else "deleted"
        return DeletionResult(
            item_title=item.title,
            ok=True,
            message=message,
            removed_hashes=removed,
            steps=steps,
        )

    def delete_many(self, items: list[MediaItem], force: bool = False) -> list[DeletionResult]:
        """Delete several items; each is isolated (one failure doesn't stop others)."""
        results: list[DeletionResult] = []
        for item in items:
            try:
                results.append(self.delete_item(item, force=force))
            except DeletionRefused as exc:
                logger.info("refused to delete '%s': %s", item.title, exc)
                results.append(DeletionResult(item.title, ok=False, message=f"refused: {exc}"))
            except Exception as exc:
                results.append(DeletionResult(item.title, ok=False, message=str(exc)))
        return results
