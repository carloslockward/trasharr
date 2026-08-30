# trasharr

trasharr is a small web app for the media-automation stack (Sonarr, Radarr, Prowlarr,
qBittorrent, Jellyfin) that lets you safely reclaim disk space by deleting content you
have already watched **and** that has met its tracker's seeding requirements.

The problem trasharr solves: private trackers require a minimum seed ratio / seed time,
and you want to keep seeding past the minimum to maintain an overall ratio above 1.0.
That means qBittorrent is configured to seed forever and never stop on its own. trasharr
is then the tool that decides what is safe to delete: it keeps only content that is both
**watched** (Jellyfin) and **seeding-complete** (qBittorrent ratio/time met against that
tracker's limits).

## Features

- Lists movies and series that are watched in Jellyfin.
- Shows each item's current qBittorrent ratio, seed time, and per-tracker seeding
  requirement — with a clear "seeding complete" badge.
- One-click delete removes an item everywhere it lives:
  1. Unmonitors the item in Sonarr/Radarr (so it is not re-grabbed)
  2. Deletes the media files through the arr (keeps its database and metadata consistent)
  3. Removes the torrent(s) from qBittorrent, including any cross-seed copies sharing the
     same files (`cross-seed` tag or same content)
- Matching is exact, not fuzzy: the arr grab history records the qBittorrent torrent hash,
  so trasharr maps torrents to media items by `downloadId`, never by title guessing.
- Per-tracker seeding limits configured in a JSON file (ratio and/or seed time), not
  enforced by Prowlarr or qBittorrent.

## Why?

Private trackers require you to seed for a minimum ratio or time before you can delete.
Meanwhile, watched content sits on disk seeding longer than required — which is good for
your ratio but bad for your free space. trasharr shows you exactly which watched items are
free to delete (seeding requirement met) so you can reclaim space without ever risking a
tracker ratio/time violation.

## Architecture

```
app/
  __init__.py      Flask application factory
  config.py        JSON configuration (endpoints, API keys, per-tracker seed limits)
  clients/
    qbittorrent.py qBittorrent Web UI API v2
    sonarr.py      Sonarr API v3
    radarr.py      Radarr API v3
    prowlarr.py    Prowlarr API v1 (tracker discovery)
    jellyfin.py    Jellyfin API
  service/
    matcher.py     torrent <-> media item matching + seeding-complete evaluation
    delete.py      the delete contract (unmonitor -> arr deleteFiles -> qBit remove)
  web/
    routes.py      Flask routes (index list + settings)
```

## Configuration

trasharr stores all configuration in a JSON file (`config.json` by default, path
overridable via the `TRASHARR_CONFIG` environment variable). It holds:

- qBittorrent, Sonarr, Radarr, Prowlarr, and Jellyfin endpoints + API keys
- Per-tracker seeding limits (ratio and/or seed time), keyed by tracker domain

The `config.json` file holds API keys and seed limits, so it is git-ignored. A
`config.example.json` with placeholder values is provided as a starting point.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # then edit
./run.py                              # dev server at http://localhost:5000
```

## Running with Docker

```bash
docker build -t trasharr .
docker run -d --name trasharr \
  -p 5000:5000 \
  -v /path/to/config.json:/config/config.json:ro \
  trasharr
```

On Unraid, add a container with:

- Repository: `ghcr.io/carloslockward/trasharr`
- Port: `5000`
- Config volume: mount your `config.json` at `/config/config.json`

## License

[Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/)

## Credits

trasharr by @carloslockward

Uses:

- [Flask](https://flask.palletsprojects.com/)
- Sonarr, Radarr, Prowlarr, qBittorrent, Jellyfin APIs

---

#### Contributions and bug reports welcome!