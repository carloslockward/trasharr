"""Trasharr web routes.

The web UI is small and self-contained:

* ``GET /``            the list page (poster grid, filtering, delete button)
* ``GET /api/library`` JSON of the current watched items + seeding status
* ``POST /api/delete`` the delete action (the only mutating endpoint)
* ``GET /settings``    the settings page (services + per-tracker limits)
* ``POST /settings``   save service configuration
* tracker routes       add/update/remove per-tracker seeding limits

Deleting is destructive, so it is a separate POST to a dedicated endpoint and
every returned item is re-verified server-side before anything is removed
(the DeleteCoordinator refuses if any torrent is not seeding-complete).
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from ..service import (
    DeleteCoordinator,
    DeletionResult,
    load_items,
    make_clients,
)
from ..service.matcher import discovered_tracker_domains

logger = logging.getLogger(__name__)

bp = Blueprint("trasharr", __name__)


def _config():
    return current_app.config["TRASHARR_CONFIG"]


def _to_json(item) -> dict[str, Any]:
    torrents = []
    for torrent, ev in zip(item.torrents, item.evaluations):
        torrents.append(
            {
                "name": torrent.get("name"),
                "hash": torrent.get("hash"),
                "state": torrent.get("state"),
                "ratio": torrent.get("ratio"),
                "seeding_time_seconds": ev.seeding_time_seconds,
                "seeding_time_minutes": ev.seeding_time_seconds // 60,
                "tracker": ev.tracker_domain,
                "target_ratio": ev.target_ratio,
                "target_time_minutes": ev.target_time_minutes,
                "met": ev.met,
                "cross_seed": ev.is_cross_seed,
            }
        )
    return {
        "arr": item.arr,
        "arr_id": item.arr_id,
        "title": item.title,
        "year": item.year,
        "media_type": item.media_type,
        "image_url": item.image_url,
        "size_bytes": item.size_bytes,
        "no_live_torrents": item.no_live_torrents,
        "torrents": torrents,
        "seeding_complete": item.seeding_complete,
        "safe_to_delete": item.safe_to_delete,
    }


@bp.route("/")
def index():
    return render_template("index.html", config=_config())


@bp.route("/api/library")
def api_library():
    config = _config()
    try:
        items, diag = load_items(config)
    except Exception as exc:
        logger.exception("build_index failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "items": [_to_json(i) for i in items], "diagnostics": diag})


@bp.route("/api/delete", methods=["POST"])
def api_delete():
    config = _config()
    payload = request.get_json(silent=True) or {}
    selections = payload.get("items") or []
    force = bool(payload.get("force"))
    if not selections:
        return jsonify({"ok": False, "error": "no items selected"}), 400

    clients = make_clients(config)
    qbt = clients.get("qbt")
    if not qbt:
        return jsonify({"ok": False, "error": "qbittorrent is not enabled"}), 400

    sonarr = clients.get("sonarr")
    radarr = clients.get("radarr")
    if not sonarr or not radarr:
        return jsonify({"ok": False, "error": "sonarr and radarr must be enabled to delete"}), 400

    try:
        # Rebuild the full index so we can look up the real MediaItems server-side
        # rather than trusting the client-supplied ids.
        items, _diag = load_items(config, clients)
        items = {f"{i.arr}:{i.arr_id}": i for i in items}
        selected = [items[f"{s['arr']}:{s['arr_id']}"] for s in selections if f"{s['arr']}:{s['arr_id']}" in items]
    except Exception as exc:
        logger.exception("could not rebuild index for delete")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        for c in clients.values():
            close = getattr(c, "close", None)
            if callable(close):
                close()

    if not selected:
        return jsonify({"ok": False, "error": "none of the selections matched the current index"}), 400

    coord = DeleteCoordinator(qbt, sonarr, radarr, config)
    results: list[DeletionResult] = coord.delete_many(selected, force=force)
    return jsonify(
        {
            "ok": all(r.ok for r in results),
            "dry_run": coord.dry_run,
            "results": [
                {"title": r.item_title, "ok": r.ok, "message": r.message,
                 "steps": r.steps, "removed": r.removed_hashes}
                for r in results
            ],
        }
    )


# --- settings -------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    config = _config()
    if request.method == "POST":
        form = request.form
        # Update each enabled service block.
        for svc in ("qbittorrent", "sonarr", "radarr", "prowlarr"):
            entry = config.data[svc]
            entry["enabled"] = form.get(f"{svc}_enabled") == "on"
            if svc == "qbittorrent":
                entry["base_url"] = form.get("qbittorrent_base_url", "").strip()
                entry["api_key"] = form.get("qbittorrent_api_key", "").strip()
                entry["username"] = form.get("qbittorrent_username", "").strip()
                entry["password"] = form.get("qbittorrent_password", "")
            else:
                entry["base_url"] = form.get(f"{svc}_base_url", "").strip()
                entry["api_key"] = form.get(f"{svc}_api_key", "").strip()
        config.data["cross_seed_tag"] = form.get("cross_seed_tag", "cross-seed").strip() or "cross-seed"
        config.save()
        return redirect(url_for("trasharr.settings"))

    return render_template("settings.html", config=config)


@bp.route("/api/trackers/discovered")
def api_discovered_trackers():
    """Tracker domains currently seen on live torrents (for the settings dropdown)."""
    config = _config()
    clients = make_clients(config)
    qbt = clients.get("qbt")
    if not qbt:
        return jsonify({"ok": False, "error": "qbittorrent is not enabled", "domains": []}), 400
    try:
        qbt.login()
        domains = discovered_tracker_domains(qbt)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "domains": []}), 502
    finally:
        qbt.close()
    return jsonify({"ok": True, "domains": domains})


@bp.route("/settings/trackers", methods=["POST"])
def add_tracker():
    config = _config()
    domain = request.form.get("domain", "").strip().lower()
    if not domain:
        return jsonify({"ok": False, "error": "domain required"}), 400
    try:
        ratio = float(request.form.get("target_ratio", 0) or 0)
        time_min = float(request.form.get("target_seed_time_minutes", 0) or 0)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid numbers"}), 400
    config.set_tracker_requirement(domain, ratio, time_min)
    return redirect(url_for("trasharr.settings"))


@bp.route("/settings/trackers/<domain>/edit", methods=["POST"])
def edit_tracker(domain: str):
    """Update the seeding requirement of an existing tracker (domain stays fixed)."""
    config = _config()
    if domain not in config.data["trackers"]:
        return jsonify({"ok": False, "error": "tracker not configured"}), 404
    try:
        ratio = float(request.form.get("target_ratio", 0) or 0)
        time_min = float(request.form.get("target_seed_time_minutes", 0) or 0)
    except ValueError:
        return jsonify({"ok": False, "error": "invalid numbers"}), 400
    config.set_tracker_requirement(domain, ratio, time_min)
    return redirect(url_for("trasharr.settings"))


@bp.route("/settings/trackers/<domain>/delete", methods=["POST"])
def remove_tracker(domain: str):
    config = _config()
    config.remove_tracker(domain)
    return redirect(url_for("trasharr.settings"))