"""API clients for the services trasharr talks to."""

from .prowlarr import ProwlarrClient
from .qbittorrent import QBittorrentClient
from .radarr import RadarrClient
from .sonarr import SonarrClient

__all__ = [
    "ProwlarrClient",
    "QBittorrentClient",
    "RadarrClient",
    "SonarrClient",
]
