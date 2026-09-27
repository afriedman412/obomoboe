"""obomoboe -- a local read-it-later list with a real archive behind it."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask

from . import archiver, db
from .config import build_config


def create_app(overrides: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(build_config(overrides))
    app.secret_key = os.environ.get("OBOMOBOE_SECRET_KEY", "obomoboe-local")

    # Cap the request body just above the capture limit, so an oversize page
    # is refused by Flask rather than buffered in full.
    app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_CAPTURE_BYTES"] + 1_000_000

    Path(app.config["DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["ARCHIVE_DIR"]).mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DB_PATH"])

    app.teardown_appcontext(db.close_db)

    from .routes import bp
    app.register_blueprint(bp)

    app.jinja_env.filters["humandate"] = _humandate
    app.jinja_env.filters["hostname"] = _hostname

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if app.config.get("START_WORKERS", True):
        archiver.start_workers(app.config["ARCHIVE_WORKERS"])

    return app


def _humandate(value: str | None) -> str:
    """ISO timestamp -> 'Mar 4, 2026'. Passes anything unparseable through."""
    if not value:
        return ""
    from datetime import datetime

    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            return datetime.fromisoformat(candidate).strftime("%b %-d, %Y")
        except ValueError:
            continue
    return str(value)[:10]


def _hostname(url: str | None) -> str:
    from urllib.parse import urlparse

    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
