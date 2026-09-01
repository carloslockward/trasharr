# trasharr

trasharr is a small web app for the media-automation stack (Sonarr, Radarr, Prowlarr,
qBittorrent) that lets you safely reclaim disk space by deleting content whose torrents
have met their tracker's seeding requirements.

The problem trasharr solves: private trackers require a minimum seed ratio / seed time,
and you want to keep seeding past the minimum to maintain an overall ratio above 1.0.
That means qBittorrent is configured to seed forever and never stop on its own. trasharr
is then the tool that decides what is safe to delete: it lists every movie/series whose
torrents have met their per-tracker limits — and you pick what you have watched
yourself.

## Features

- Lists every movie/series in Sonarr/Radarr that still has live torrents in qBittorrent,
  with per-tracker progress bars showing ratio / seed time against the tracker's targets.
- One-click delete removes an item everywhere it lives:
  1. Verifies every hosting torrent (including cross-seed copies sharing the same
     content) has met its tracker's requirement — refuses otherwise
  2. Unmonitors the item in Sonarr/Radarr (so it is not re-grabbed)
  3. Deletes the media files through the arr (removes the item from the arr's library)
  4. Removes the torrent(s) from qBittorrent with files
- **Cross-seed awareness**: torrents tagged with the cross-seed tag were never
  downloaded from their tracker (the data was grabbed once via the original torrent),
  so their seeding limits don't apply — they always count as complete.
- **Dry-run mode** (`TRASHARR_DRY_RUN=1`): the entire delete contract runs and logs
  exactly what it *would* do — including which cross-seed siblings would be removed and
  which torrents fail the requirements — without touching anything.
- **Forced deletes**: deleting an item whose requirements are not met triggers an extra
  confirmation and then proceeds by explicit user override (logged on the server).
- Matching is exact, not fuzzy: the arr grab history records the qBittorrent torrent
  hash, so trasharr maps torrents to media items by `downloadId`, never by title
  guessing.
- Per-tracker seeding limits configured in a JSON file (ratio and/or seed time), not
  enforced by Prowlarr or qBittorrent.
- Tracker discovery: the settings page offers a dropdown of every tracker domain seen
  on live torrents, so adding a new tracker is one click.
- Fast UI: the library is cached in the browser (navigating to Settings and back is
  instant), with a refresh button, sort by name or seed time (direction toggle), and a
  hover card showing every torrent behind a show.

## Why?

Private trackers require you to seed for a minimum ratio or time before you can delete.
Meanwhile, content sits on disk seeding longer than required — which is good for your
ratio but bad for your free space. trasharr shows you exactly which items are free to
delete (seeding requirement met) so you can reclaim space without ever risking a tracker
ratio/time violation.

## Architecture

```
app/
  __init__.py      Flask application factory
  config.py        JSON configuration (endpoints, API keys, per-tracker seed limits)
  clients/
    qbittorrent.py qBittorrent Web UI API v2 (API-key or cookie auth)
    sonarr.py      Sonarr API v3
    radarr.py      Radarr API v3
    prowlarr.py    Prowlarr API v1 (tracker discovery; reserved)
  service/
    matcher.py     torrent <-> media item matching + seeding-complete evaluation
    library.py     assembles the library from the configured services
    delete.py      the delete contract (verify -> unmonitor -> arr deleteFiles -> qBit remove)
  web/
    routes.py      Flask routes (index list, API, delete, settings, tracker limits)
  templates/       index (poster grid) + settings pages — no build step, vanilla JS
```

## Configuration

trasharr stores all configuration in a JSON file (`config.json` by default, path
overridable via the `TRASHARR_CONFIG` environment variable). It holds:

- qBittorrent, Sonarr, Radarr, and Prowlarr endpoints + API keys
- Per-tracker seeding limits (ratio and/or seed time), keyed by tracker domain
  (as seen on the torrent, e.g. `tracker.digitalcore.club`)
- The qBittorrent tag that marks cross-seed copies (default `cross-seed`)

A tracker requirement is met when **either** axis is satisfied: ratio >= target OR seed
time >= target. Set an unused axis to 0. A tracker with no configured requirement is
treated as complete.

The `config.json` file holds API keys and seed limits, so it is git-ignored. A
`config.example.json` with placeholder values is provided as a starting point.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # then edit
python run.py                        # dev server at http://localhost:5000
```

### Dry-run mode

Before trusting trasharr with real deletes, exercise the whole flow safely:

```bash
TRASHARR_DRY_RUN=1 python run.py
```

Every delete performs the full verification and logs its complete intent (items,
cross-seed siblings, torrents that fail the gate) but performs no destructive call. The
UI labels the result "DRY RUN — nothing deleted."

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
- Sonarr, Radarr, Prowlarr, qBittorrent APIs

---

#### Contributions and bug reports welcome!
