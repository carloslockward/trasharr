"""Trasharr Flask application factory."""

from __future__ import annotations

import logging
import os

from flask import Flask

from .config import Config

logger = logging.getLogger(__name__)

CONFIG_DIR = os.environ.get("TRASHARR_CONFIG_DIR", os.getcwd())


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