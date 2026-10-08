"""Configuration, all overridable by OBOMOBOE_* environment variables."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def default_data_dir() -> Path:
    """data/ next to the code when run from a checkout. The packaged app's
    code sits inside the bundle, which is replaced on every update, so it
    keeps its data where each platform expects an app to."""
    if not getattr(sys, "frozen", False):
        return BASE_DIR / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obomoboe"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / "obomoboe"
    xdg = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(xdg) / "obomoboe"


def build_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    data_dir = Path(os.environ.get("OBOMOBOE_DATA_DIR", default_data_dir()))
    config: dict[str, Any] = {
        "DATA_DIR": data_dir,
        "DB_PATH": data_dir / "obomoboe.db",
        "ARCHIVE_DIR": data_dir / "archives",
        "USER_AGENT": os.environ.get("OBOMOBOE_USER_AGENT", DEFAULT_USER_AGENT),
        "FETCH_TIMEOUT": _env_int("OBOMOBOE_FETCH_TIMEOUT", 25),
        # Snapshot images alongside the text so the archive survives link rot.
        "ARCHIVE_IMAGES": _env_bool("OBOMOBOE_ARCHIVE_IMAGES", True),
        "MAX_IMAGES": _env_int("OBOMOBOE_MAX_IMAGES", 25),
        "MAX_IMAGE_BYTES": _env_int("OBOMOBOE_MAX_IMAGE_BYTES", 5_000_000),
        # Below this word count a "successful" fetch is treated as a paywall stub.
        "MIN_WORDS": _env_int("OBOMOBOE_MIN_WORDS", 200),
        "ARCHIVE_PH_ENABLED": _env_bool("OBOMOBOE_ARCHIVE_PH", True),
        "ARCHIVE_PH_HOSTS": [
            h.strip()
            for h in os.environ.get(
                "OBOMOBOE_ARCHIVE_PH_HOSTS", "archive.ph,archive.today,archive.is"
            ).split(",")
            if h.strip()
        ],
        "ARCHIVE_WORKERS": _env_int("OBOMOBOE_WORKERS", 2),
        # Tagging. Everything here is overridable from the settings page;
        # these are the defaults it starts from.
        "TAG_SUGGESTIONS": _env_int("OBOMOBOE_TAG_SUGGESTIONS", 6),
        # off | suggest | apply -- see db.TAG_MODES.
        "TAG_MODE": os.environ.get("OBOMOBOE_TAG_MODE", "suggest"),
        "LLM_TAGS_ENABLED": _env_bool("OBOMOBOE_LLM_TAGS", True),
        "TAG_MODEL": os.environ.get("OBOMOBOE_TAG_MODEL", "claude-opus-5"),
        "LLM_TAG_CHARS": _env_int("OBOMOBOE_LLM_TAG_CHARS", 6000),
        # Read from the environment if it is there; the settings page can set
        # it instead, and that takes precedence.
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        # Upper bound on a page captured by the bookmarklet. Rendered DOMs
        # run large; this is a guard against a runaway, not a target.
        "MAX_CAPTURE_BYTES": _env_int("OBOMOBOE_MAX_CAPTURE_BYTES", 12_000_000),
        # Set false in tests so archiving runs inline / not at all.
        "START_WORKERS": True,
    }
    if overrides:
        config.update(overrides)
    return config
