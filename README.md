# trasharr

trasharr is a small web app for the Sonarr + Radarr + qBittorrent media stack that
shows you exactly which movies and shows are safe to delete — and deletes them for
you in one click — so you can reclaim disk space without ever breaking a private
tracker's seeding rules.

## The problem it solves

Private trackers require a minimum seed ratio or seed time before you may stop
seeding. To stay above an overall 1.0 ratio you have qBittorrent set to a minimum 
ammount of time and or ratio, which means watched content can acumulate and quietly 
fill your disk. trasharr keeps score for you: for every item it compares each torrent's 
ratio and seed time against the limits of its tracker, and flags what has paid its dues.

## What you get

- **A poster grid of everything currently on disk**, with a per-tracker progress bar
  on each card showing how close a torrent is to its ratio / seed-time target.
- **Green means safe**: once every torrent on a card has met its tracker's limits,
  the card is marked *safe to delete*. One toggle shows the not-yet-finished ones too.
- **Real disk usage per card** — including hardlinked cross-seed copies, which count
  once, not twice. Selecting cards shows a running "to be freed" total.
- **Leftovers included**: files whose torrent is long gone (a season you deleted
  before trasharr existed) still appear, marked *no torrent*, and can be deleted.
  Stray cross-seed copies that outlived their original show up as orphans.
- **One-click delete that refuses to be unsafe.** Deleting verifies every torrent,
  removes only the exact files that belong to that card, stops the arr from
  re-downloading, and removes the torrents — including their cross-seed copies.
  If anything hasn't met its tracker's rules, the delete is refused (you can force
  it through an explicit extra confirmation).
- **Cross-seed aware**: copies created by the cross-seed tool share the same file on
  disk and were never downloaded from their tracker, so they don't add seeding
  requirements and are deleted together with the original.
- **Dry-run mode**: set `TRASHARR_DRY_RUN=1` and every delete performs its full
  checks and logs exactly what it *would* do — touching nothing. Do this first.
- **Friendly settings page**: per-tracker limits (ratio and/or seed time) with a
  dropdown of trackers auto-discovered from your torrents; browser-cached grid,
  sorting by name / seed time / size, and a hover card with the details behind
  each poster.

## How it works

trasharr reads Sonarr's and Radarr's grab history to map torrents to media by
torrent hash — no title guessing — and physically identifies files on disk
(following hardlinks) so copies of the same data always share one card and one
delete. All configuration lives in a JSON file: service endpoints and API keys,
per-tracker seed limits (met when **either** ratio **or** seed time reaches its
target; unused axis = 0; unconfigured trackers count as complete), and the
cross-seed tag. See `config.example.json` to start.

## Running

Locally:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # then edit
python run.py                        # dev server at http://localhost:5000
```

With Docker:

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

---

#### Contributions and bug reports welcome! trasharr by @carloslockward