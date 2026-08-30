"""Service layer: matching, safety evaluation, and the delete contract."""

from .matcher import build_index, evaluate_torrent, MediaItem, SeedEvaluation
from .delete import DeleteCoordinator, DeletionResult, DeletionRefused
from .library import load_items, make_clients

__all__ = [
    "build_index",
    "evaluate_torrent",
    "MediaItem",
    "SeedEvaluation",
    "DeleteCoordinator",
    "DeletionResult",
    "DeletionRefused",
    "load_items",
    "make_clients",
]