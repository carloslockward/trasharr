"""API clients for the services trasharr talks to."""

from .qbittorrent import QBittorrentClient
from .sonarr import SonarrClient
from .radarr import RadarrClient
from .prowlarr import ProwlarrClient

__all__ = [
    "QBittorrentClient",
    "SonarrClient",
    "RadarrClient",
    "ProwlarrClient",
]
