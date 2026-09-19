"""Service layer: matching, safety evaluation, and the delete contract."""

from .delete import DeleteCoordinator, DeletionRefused, DeletionResult
from .library import load_items, make_clients
from .matcher import MediaItem, SeedEvaluation, build_index, evaluate_torrent

__all__ = [
    "DeleteCoordinator",
    "DeletionRefused",
    "DeletionResult",
    "MediaItem",
    "SeedEvaluation",
    "build_index",
    "evaluate_torrent",
    "load_items",
    "make_clients",
]