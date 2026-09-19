"""The delete contract.

trasharr never touches media files directly. Deleting an item is an ordered,
atomic-looking sequence through the source services:

    1. VERIFY   — every torrent hosting the item's files (including hardlink
                  cross-seed siblings merged into the card) is seeding-complete
                  on every tracker that has a configured requirement. If any is
                  not, REFUSE. This is the safety net that keeps trasharr from
                  ever violating a tracker's rule.
    2. MATCH    — the arr's tracked files (episodefile/moviefile records) are
                  matched against the card's physical files: inode equality
                  first (hardlink-aware), exact basename as fallback. NO match
                  at all -> REFUSE. Item-level deleteFiles=true is never used:
                  it would wipe every file of the series/movie.
    3. DELETE   — delete only the matched arr file records, one by one.
    4. UNMONITOR— only when NO tracked file remains for the media: per-episode
                  (Sonarr PUT /episode/monitor) or movie-level (Radarr). An
                  unmonitored item is never re-grabbed, so deleting the last
                  tracked file cannot trigger a surprise re-download even when
                  untracked copies (other qualities, cross-seed links) remain
                  on disk.
    5. REMOVE   — delete every qBit torrent hosting the files (the arr-hash
                  torrent plus its hardlinked copies).

LEFTOVER FILES — a third, simpler contract for arr-tracked files whose
torrents are long gone (e.g. a season deleted before trasharr existed): the
matcher emits one catch-all card per media listing those files; the contract
deletes exactly those arr files (no verification needed — nothing is seeding,
so no tracker rule can be violated), then unmonitors per the same
"nothing tracked remains" rule. qBittorrent is not involved.

Deletion is performed one item at a time; a failure in a step aborts that item
and is surfaced so the caller can report it — we do not silently continue past
a refused or errored item.

DRY RUN
-------
Set ``TRASHARR_DRY_RUN=1`` to run the *entire* contract without executing any
destructive call. Every step is verified and logged (which arr files would be
deleted, which episodes would be unmonitored, which torrents removed), so a
dry run shows exactly what a real delete would do — including refusals — while
touching nothing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from ..clients.qbittorrent import QBittorrentClient
from .matcher import MediaItem, card_file_identity, evaluate_torrent, match_arr_files

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

        Every host (the grabbed torrent plus its hardlinked copies) must
        satisfy its tracker's requirement. A single non-compliant host blocks the whole delete —
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

    def _delete_leftover_files(self, item: MediaItem) -> DeletionResult:
        """Third contract mode: delete a media's arr-tracked files that no
        torrent covers (one catch-all card per media).

        VERIFY: none — nothing is seeding, so there is no tracker rule to
        violate.  DELETE: exactly the card's leftover files (re-checked
        against the live arr listing; a file that vanished since the index
        is tolerated).  UNMONITOR: only when NO tracked file remains —
        per-episode for Sonarr, movie-level for Radarr.  qBittorrent is
        never touched: there is no torrent to remove.
        """
        steps: list[str] = []
        log = logger.info
        dry = self.dry_run
        prefix = "[DRY RUN] " if dry else ""
        arr = self._arr_for(item)

        log("%sintent to delete %d leftover file(s) of '%s' (%s:%s) — no torrents involved",
            prefix, len(item.leftover_files), item.title, item.arr, item.arr_id)
        try:
            arr_files = arr.media_files(item.arr_id)
        except Exception as exc:
            if "404" in str(exc):
                log("%s%s item id=%s already gone; treating as cleaned", prefix, item.arr, item.arr_id)
                steps.append(f"{item.arr} item already deleted")
                return DeletionResult(item_title=item.title, ok=True, message="already gone", steps=steps)
            raise RuntimeError(f"failed to list {item.arr} files: {exc}") from exc

        wanted_ids = {f["id"] for f in item.leftover_files}
        targets = [f for f in arr_files if int(f.get("id") or 0) in wanted_ids]
        if not targets:
            steps.append("no leftover files remain (already cleaned)")
            return DeletionResult(item_title=item.title, ok=True, message="nothing to delete", steps=steps)

        if dry:
            for f in targets:
                log("%s  would delete leftover file id=%s: %s", prefix, f.get("id"), f.get("path"))
            steps.append(f"[dry-run] would delete {len(targets)} leftover file(s) via {item.arr}")
        else:
            deleted = 0
            for f in targets:
                try:
                    arr.delete_media_file(int(f["id"]))
                    deleted += 1
                    log("%sdeleted leftover file id=%s: %s", prefix, f.get("id"), f.get("path"))
                except Exception as exc:
                    if "404" in str(exc):
                        deleted += 1
                    else:
                        raise RuntimeError(f"failed to delete {item.arr} file {f.get('path')}: {exc}") from exc
            steps.append(f"deleted {deleted} leftover file(s) via {item.arr}")

        target_ids = {int(f.get("id") or 0) for f in targets}
        remaining = [f for f in arr_files if int(f.get("id") or 0) not in target_ids]
        if not remaining:
            if item.arr == "sonarr":
                episodes = arr.episodes(item.arr_id)
                by_file = {e.get("episodeFileId"): e.get("id")
                           for e in episodes if e.get("episodeFileId")}
                episode_ids = sorted({by_file[fid] for fid in target_ids if fid in by_file})
                if dry:
                    log("%swould unmonitor %d sonarr episode(s): %s", prefix, len(episode_ids), episode_ids)
                    steps.append(f"[dry-run] would unmonitor {len(episode_ids)} episode(s)")
                else:
                    arr.set_episode_monitor(episode_ids, False)
                    log("%sunmonitored %d sonarr episode(s) of '%s'", prefix, len(episode_ids), item.title)
                    steps.append(f"unmonitored {len(episode_ids)} episode(s)")
            else:
                if dry:
                    log("%swould unmonitor radarr item id=%s (%s)", prefix, item.arr_id, item.title)
                    steps.append(f"[dry-run] would unmonitor radarr:{item.arr_id}")
                else:
                    arr.unmonitor(item.arr_id)
                    log("%sremoved '%s' from monitoring (radarr, id=%s)", prefix, item.title, item.arr_id)
                    steps.append(f"unmonitored radarr:{item.arr_id}")
        else:
            log("%s%d tracked file(s) remain for '%s' — leaving it monitored", prefix, len(remaining), item.title)
            steps.append(f"{len(remaining)} file(s) remain — kept monitored")

        message = "dry-run: delete verified, nothing was touched" if dry else "deleted"
        return DeletionResult(item_title=item.title, ok=True, message=message, steps=steps)

    def delete_item(self, item: MediaItem, force: bool = False) -> DeletionResult:
        """Delete one file-set card.

        Every card IS one physical file-set (the matcher guarantees this), so
        item.torrents is the complete deletable set. The arr contract is now
        FILE-level, not item-level:

          * match the arr's tracked files against the card's physical files
            (inode first, basename fallback) and delete ONLY those file
            records — never the whole series/movie;
          * refuse when no arr file matches (never fall back to delete-all);
          * unmonitor only when the media has no tracked files left: episode-
            level for Sonarr, movie-level for Radarr. An unmonitored item is
            never re-grabbed, so deleting the last tracked file cannot trigger
            a surprise re-download even if untracked copies remain on disk;
          * orphan/copy cards stay qBittorrent-only.

        Leftover-files cards (arr-tracked files whose torrents are long gone)
        take a third, simpler contract — see _delete_leftover_files.
        """
        # Leftover-files card: arr-tracked files whose torrents are long gone.
        # No qBittorrent step and no verification gate — nothing is seeding.
        if item.leftover_files and not item.torrents:
            return self._delete_leftover_files(item)
        steps: list[str] = []
        log = logger.info
        dry = self.dry_run
        prefix = "[DRY RUN] " if dry else ""

        host_hashes = {t["hash"] for t in item.torrents}
        hosts = list(item.torrents)

        log("%sintent to delete file-set '%s' (%s:%s)%s from storage",
            prefix, item.title, item.arr, item.arr_id,
            " [FORCED — requirements not met]" if force else "")
        if item.orphan or not item.has_arr_authority:
            log("%sfile-set lacks arr authority (cross-seed copy / orphan) — qBittorrent-only delete", prefix)
        log("%sfile-set contains %d torrent(s) at %s", prefix, len(hosts),
            hosts[0].get("content_path") if hosts else "?")

        # 1. Verify every torrent is seed-complete (the safety gate). Runs the
        # same in dry-run mode so refusals can be observed without side
        # effects. `force` (user override) downgrades violations to warnings.
        self._verify_complete(item, hosts, force=force)

        removed: list[str] = []

        # 2+3. Delete the matched arr files, then unmonitor only if nothing
        # tracked remains — only for cards with arr authority.
        if item.has_arr_authority:
            arr = self._arr_for(item)
            try:
                arr_files = arr.media_files(item.arr_id)
            except Exception as exc:
                if "404" in str(exc):
                    log("%s%s item id=%s already gone; files handled elsewhere", prefix, item.arr, item.arr_id)
                    steps.append(f"{item.arr} item already deleted")
                    arr_files = None
                else:
                    raise RuntimeError(f"failed to list {item.arr} files: {exc}") from exc

            if arr_files is not None:
                identity = card_file_identity(item.torrents, self.qbt)
                matched = match_arr_files(arr_files, identity)
                if not matched:
                    # The arr tracks a DIFFERENT file of this media (e.g.
                    # Radarr imported another quality/release than the card's
                    # torrents). Deleting this card must NOT touch the arr's
                    # file or monitoring — only its own torrents go. This is a
                    # clean partial delete, not a refusal.
                    _ci, card_names, card_sizes = identity
                    logger.info("no %s file matched for '%s' — the arr tracks a different "
                                "file of this media; leaving arr files + monitoring alone",
                                item.arr, item.title)
                    for f in arr_files:
                        logger.info("  arr file id=%s size=%s path=%s (untracked-by-this-card)",
                                    f.get("id"), f.get("size"), f.get("path"))
                    logger.info("  card basenames: %s", sorted(card_names))
                    logger.info("  card sizes: %s", sorted(card_sizes))
                    steps.append(f"{item.arr} tracks a different file — left alone")
                elif matched:
                    log("%smatched %d/%d %s file(s): %s", prefix, len(matched), len(arr_files),
                        item.arr, ", ".join(os.path.basename(f.get("path") or "") for f in matched))

                if dry:
                    if matched:
                        for f in matched:
                            log("%s  would delete file id=%s: %s", prefix, f.get("id"), f.get("path"))
                        steps.append(f"[dry-run] would delete {len(matched)} file(s) via {item.arr}")
                elif matched:
                    deleted_ids: list[int] = []
                    for f in matched:
                        try:
                            arr.delete_media_file(int(f["id"]))
                            deleted_ids.append(int(f["id"]))
                            log("%sdeleted file id=%s: %s", prefix, f["id"], f.get("path"))
                        except Exception as exc:
                            if "404" in str(exc):
                                log("%sfile id=%s already gone", prefix, f.get("id"))
                                deleted_ids.append(int(f["id"]))
                            else:
                                raise RuntimeError(f"failed to delete {item.arr} file {f.get('path')}: {exc}") from exc
                    steps.append(f"deleted {len(deleted_ids)} file(s) via {item.arr}")

                matched_ids = {int(f["id"]) for f in matched}
                remaining = [f for f in arr_files if int(f.get("id") or 0) not in matched_ids]

                # Unmonitor ONLY when nothing tracked remains AND this card
                # actually matched arr files (a card the arr doesn't track
                # never changes monitoring). Untracked leftovers (e.g. the
                # other quality) do not keep the item monitored — an
                # unmonitored item is never re-grabbed.
                if not matched:
                    pass  # arr tracks another file; leave monitoring as is
                elif not remaining:
                    if item.arr == "sonarr":
                        try:
                            episodes = arr.episodes(item.arr_id)
                        except Exception as exc:
                            raise RuntimeError(f"failed to list sonarr episodes: {exc}") from exc
                        by_file = {e.get("episodeFileId"): e.get("id")
                                   for e in episodes if e.get("episodeFileId")}
                        episode_ids = sorted({by_file[fid] for fid in matched_ids if fid in by_file})
                        if dry:
                            log("%swould unmonitor %d sonarr episode(s): %s", prefix,
                                len(episode_ids), episode_ids)
                            steps.append(f"[dry-run] would unmonitor {len(episode_ids)} episode(s)")
                        else:
                            arr.set_episode_monitor(episode_ids, False)
                            log("%sunmonitored %d sonarr episode(s): %s", prefix, len(episode_ids), episode_ids)
                            steps.append(f"unmonitored {len(episode_ids)} episode(s)")
                    else:
                        if dry:
                            log("%swould unmonitor radarr item id=%s (%s)", prefix, item.arr_id, item.title)
                            steps.append(f"[dry-run] would unmonitor radarr:{item.arr_id}")
                        else:
                            arr.unmonitor(item.arr_id)
                            log("%sremoved '%s' from monitoring (radarr, id=%s)", prefix, item.title, item.arr_id)
                            steps.append(f"unmonitored radarr:{item.arr_id}")
                else:
                    log("%s%d tracked file(s) remain for '%s' — leaving it monitored", prefix,
                        len(remaining), item.title)
                    steps.append(f"{len(remaining)} file(s) remain — kept monitored")

        # 4. Remove every torrent in the file-set from qBittorrent (with files).
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
