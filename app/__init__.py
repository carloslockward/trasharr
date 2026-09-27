"""Trasharr Flask application factory."""

from __future__ import annotations

import logging
import os

from flask import Flask

from .config import Config

logger = logging.getLogger(__name__)

__version__ = "1.1.1"

CONFIG_DIR = os.environ.get("TRASHARR_CONFIG_DIR", os.getcwd())

# One shared configuration: every module logs via logging.getLogger(__name__)
# under the "trasharr" package, and this parent logger carries the level and
# stream handler for all of them.
def _setup_logging() -> None:
    if logger.handlers:  # module re-import / app factory called twice
        return
    level_name = os.environ.get("TRASHARR_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    if level_name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        logger.warning("unknown LOG_LEVEL %r — falling back to INFO", level_name)

_setup_logging()

def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("TRASHARR_SECRET_KEY") or os.urandom(24).hex()

    # A single shared Config instance lives on the app; both the web UI and
    # future entry points read/write through it.
    from .config import DEFAULT_CONFIG_PATH

    config = Config(os.environ.get("TRASHARR_CONFIG", DEFAULT_CONFIG_PATH))
    app.config["TRASHARR_CONFIG"] = config

    # Tolerate config dir not existing yet.
    config.path.parent.mkdir(parents=True, exist_ok=True)

    from .web.routes import bp

    app.register_blueprint(bp)

    return app