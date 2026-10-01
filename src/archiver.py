"""Snapshotting articles to disk, on a background worker thread.

Layout for each article:

    data/archives/<id>/original.html   raw bytes as served (or from archive.today)
    data/archives/<id>/readable.html   sanitized reader-view fragment
    data/archives/<id>/article.txt     plain text (also what FTS indexes)
    data/archives/<id>/meta.json       extracted metadata
    data/archives/<id>/assets/         downloaded images, if enabled
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import queue
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import db, tagging
from .extract import (Extracted, FetchError, dump_metadata, extract, fetch,
                      fetch_from_archive_ph, is_challenge_page,
                      looks_paywalled, strip_scripts)

log = logging.getLogger(__name__)

_queue: "queue.Queue[tuple[dict[str, Any], int, bool, bool]]" = queue.Queue()
_workers_started = threading.Event()

# Article ids currently queued or being archived. Two re-archives of the same
# article would otherwise run on two workers and race to write the same row,
# last writer winning -- so clicking "re-archive" and "try archive.today" in
# quick succession gave whichever finished last, not the one you asked for.
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def article_dir(config: dict[str, Any], article_id: int) -> Path:
    return Path(config["ARCHIVE_DIR"]) / str(article_id)


# --------------------------------------------------------------------------
# The actual work
# --------------------------------------------------------------------------


CAPTURE_NAME = "captured.html"


def capture_path(config: dict[str, Any], article_id: int) -> Path:
    """Where a page sent from the browser is parked for the worker."""
    return article_dir(config, article_id) / CAPTURE_NAME


def archive_article(config: dict[str, Any], article_id: int,
                    force_archive_ph: bool = False,
                    from_capture: bool = False) -> dict[str, Any]:
    """Fetch, extract, and write an article to disk. Updates the DB row."""
    conn = db.connect(config["DB_PATH"])
    try:
        row = db.get_article(conn, article_id)
        if row is None:
            return {"ok": False, "error": "article no longer exists"}

        # `url` is what was saved -- an archive.today link stays one, so a
        # re-archive reads the same snapshot. `original_url` is the
        # publisher's, which is what archive.today has to be asked about.
        url = row["url"]
        original = row["original_url"] or url
        db.update_article(conn, article_id, archive_status=db.PENDING,
                          archive_error=None)

        result, note = _retrieve(config, url, force_archive_ph,
                                 article_id if from_capture else None,
                                 lookup_url=original)
        if result is None:
            db.update_article(conn, article_id, archive_status=db.FAILED,
                              archive_error=note, archived_at=db.utcnow())
            return {"ok": False, "error": note}

        # Never trade a good archive for a worse one. A re-archive that trips
        # a bot check would otherwise overwrite the saved files and replace
        # the real title with the interstitial's.
        existing_words = int(row["word_count"] or 0)
        if (row["archive_status"] == db.OK
                and result.word_count < existing_words):
            kept = _note(note, f"kept the earlier copy ({existing_words} "
                               f"words); this attempt got {result.word_count}")
            # Where the piece lives is a fact about the source, not the
            # extraction, so learn it even when the text is not replaced.
            db.update_article(conn, article_id, archive_status=db.OK,
                              archive_error=kept, archived_at=db.utcnow(),
                              **_source_urls(row, result))
            return {"ok": True, "source": row["archive_source"],
                    "word_count": existing_words, "note": kept, "kept": True}

        _write_snapshot(config, article_id, result)

        db.update_article(
            conn,
            article_id,
            title=result.title or row["title"],
            author=result.author,
            site_name=result.site_name,
            excerpt=(result.excerpt or "")[:600] or None,
            published_at=result.published_at,
            word_count=result.word_count,
            archive_status=db.OK,
            archive_source=result.source,
            archive_error=note,
            archived_at=db.utcnow(),
            **_source_urls(row, result),
        )
        db.index_article(conn, article_id, result.title or url, result.text)
        _apply_tags(config, conn, article_id, result)
        return {"ok": True, "source": result.source,
                "word_count": result.word_count, "note": note}
    except Exception as exc:  # a worker thread must never die silently
        log.exception("archiving article %s failed", article_id)
        db.update_article(conn, article_id, archive_status=db.FAILED,
                          archive_error=str(exc), archived_at=db.utcnow())
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def _source_urls(row: Any, result: Extracted) -> dict[str, str]:
    """The publisher's URL and the snapshot link, when the copy was one.

    Only ever adds what was learned: a later direct fetch does not forget
    the snapshot an article was first read from.
    """
    fields: dict[str, str] = {}
    if result.original_url and result.original_url != row["original_url"]:
        fields["original_url"] = result.original_url
    if result.archive_url and result.archive_url != row["archive_url"]:
        fields["archive_url"] = result.archive_url
    return fields


def _apply_tags(config: dict[str, Any], conn: Any, article_id: int,
                result: Extracted) -> None:
    """Work out candidate tags, then either file them or offer them.

    Never fatal: a tagging failure must not cost you the archive, which is
    the part that cannot be recreated later.
    """
    mode = config.get("TAG_MODE", db.TAG_SUGGEST)
    if mode == db.TAG_OFF:
        return

    try:
        candidates = tagging.suggest(config, result.html, result.text,
                                     result.title)
    except Exception:
        log.exception("tag suggestion failed for article %s", article_id)
        return
    if not candidates:
        return

    if mode == db.TAG_APPLY:
        for name in candidates:
            db.add_tag(conn, article_id, name)
        db.set_suggestions(conn, article_id, [])
        return

    # Do not offer what is already filed.
    applied = {db.normalize_tag(t) for t
               in db.tags_for_articles(conn, [article_id]).get(article_id, [])}
    db.set_suggestions(conn, article_id,
                       [c for c in candidates
                        if db.normalize_tag(c) not in applied])


def _note(*parts: str | None) -> str | None:
    """Join the parts of an explanation, dropping the ones that aren't there.

    Forcing archive.today skips the live fetch, so there is no direct error to
    lead with -- interpolating it blindly put a literal "None; " in front of
    the message the article page shows.
    """
    kept = [part for part in parts if part]
    return "; ".join(kept) if kept else None


def _retrieve(config: dict[str, Any], url: str, force_archive_ph: bool,
              capture_id: int | None = None, lookup_url: str | None = None
              ) -> tuple[Extracted | None, str | None]:
    """Try the live page, fall back to archive.today when it looks paywalled.

    `lookup_url` is what archive.today is asked about; it differs from `url`
    when the saved link is itself a snapshot.
    """
    lookup_url = lookup_url or url
    # A page handed to us by the browser needs no fetching at all -- it was
    # rendered in a session that is already logged in and already past
    # whatever bot check stands between us and the article.
    if capture_id is not None:
        path = capture_path(config, capture_id)
        try:
            html = path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"captured page could not be read ({exc})"
        # The point of capturing is to get past a bot check, so a captured
        # bot check is a failed capture -- say so instead of filing it as the
        # article. Happens if the bookmarklet is clicked while the
        # interstitial is still on screen.
        if is_challenge_page(html):
            return None, ("the captured page was still a bot check -- wait for "
                          "the article to load, then click the bookmarklet")
        captured = extract(html, url, 200, source="browser")
        if not captured.text.strip():
            return None, "captured page had no readable text"
        return captured, None

    session = requests.Session()
    user_agent = config["USER_AGENT"]
    timeout = config["FETCH_TIMEOUT"]
    direct: Extracted | None = None
    direct_error: str | None = None

    if not force_archive_ph:
        try:
            got = fetch(url, user_agent, timeout, session)
            direct = extract(got.html, got.url or url, got.status_code,
                             source="direct", headers=got.headers)
            if not looks_paywalled(direct, config["MIN_WORDS"]):
                return direct, None
            direct_error = (
                "live page returned a bot check"
                if is_challenge_page(direct.html, direct.headers)
                else f"live page looked paywalled or truncated "
                     f"({direct.word_count} words)"
            )
        except FetchError as exc:
            direct_error = f"live fetch failed ({exc})"

    if not config["ARCHIVE_PH_ENABLED"]:
        return direct, direct_error

    try:
        html, status, final_url = fetch_from_archive_ph(
            lookup_url, user_agent, config["ARCHIVE_PH_HOSTS"], timeout, session
        )
        archived = extract(html, lookup_url, status, source="archive.today")
        archived.url = lookup_url
        archived.original_url = archived.original_url or lookup_url
        archived.archive_url = archived.archive_url or final_url
        # Only prefer the snapshot if it actually gave us more to read.
        if direct is None or archived.word_count > direct.word_count:
            note = f"used archive.today ({direct_error})" if direct_error else None
            return archived, note
        return direct, _note(direct_error, "archive.today had no better copy")
    except FetchError as exc:
        note = _note(direct_error, f"archive.today unavailable ({exc})")
        if direct is not None:
            return direct, note
        return None, note


def _write_snapshot(config: dict[str, Any], article_id: int,
                    result: Extracted) -> None:
    target = article_dir(config, article_id)
    (target / "assets").mkdir(parents=True, exist_ok=True)

    readable = result.readable_html
    if config["ARCHIVE_IMAGES"] and result.images:
        readable = _localize_images(config, target, result, readable)

    (target / "original.html").write_text(strip_scripts(result.html),
                                          encoding="utf-8")
    (target / "readable.html").write_text(readable, encoding="utf-8")
    (target / "article.txt").write_text(result.text, encoding="utf-8")
    (target / "meta.json").write_text(dump_metadata(result), encoding="utf-8")
    result.readable_html = readable


def _localize_images(config: dict[str, Any], target: Path, result: Extracted,
                     readable: str) -> str:
    """Download images and rewrite srcs to point at the local copies."""
    if not readable:
        return readable

    session = requests.Session()
    headers = {"User-Agent": config["USER_AGENT"], "Referer": result.url}
    limit = config["MAX_IMAGES"]
    max_bytes = config["MAX_IMAGE_BYTES"]
    saved: dict[str, str] = {}

    for image_url in result.images[:limit]:
        try:
            resp = session.get(image_url, headers=headers, timeout=15,
                               stream=True)
            if resp.status_code >= 400:
                continue
            content_type = resp.headers.get("Content-Type", "").split(";")[0]
            if not content_type.startswith("image/"):
                continue
            payload = b""
            for chunk in resp.iter_content(64 * 1024):
                payload += chunk
                if len(payload) > max_bytes:
                    payload = b""
                    break
            if not payload:
                continue
            suffix = (mimetypes.guess_extension(content_type)
                      or Path(urlparse(image_url).path).suffix or ".img")
            name = hashlib.sha256(image_url.encode()).hexdigest()[:16] + suffix
            (target / "assets" / name).write_bytes(payload)
            saved[image_url] = f"assets/{name}"
        except requests.RequestException:
            continue

    if not saved:
        return readable

    soup = BeautifulSoup(readable, "lxml")
    for img in soup.find_all("img"):
        absolute = urljoin(result.url, (img.get("src") or "").strip())
        if absolute in saved:
            img["src"] = saved[absolute]
    return soup.body.decode_contents() if soup.body else str(soup)


# --------------------------------------------------------------------------
# Worker pool
# --------------------------------------------------------------------------


def enqueue(config: dict[str, Any], article_id: int,
            force_archive_ph: bool = False,
            from_capture: bool = False) -> bool:
    """Queue an article. Returns False if it is already being archived."""
    with _inflight_lock:
        if article_id in _inflight:
            return False
        _inflight.add(article_id)
    _queue.put((config, article_id, force_archive_ph, from_capture))
    return True


def _worker() -> None:
    while True:
        config, article_id, force, captured = _queue.get()
        try:
            archive_article(config, article_id, force, captured)
        except Exception:
            log.exception("worker crashed on article %s", article_id)
        finally:
            with _inflight_lock:
                _inflight.discard(article_id)
            _queue.task_done()


def start_workers(count: int = 2) -> None:
    """Idempotent -- safe to call once per app instance."""
    if _workers_started.is_set():
        return
    _workers_started.set()
    for index in range(max(1, count)):
        thread = threading.Thread(target=_worker, daemon=True,
                                  name=f"archiver-{index}")
        thread.start()


def wait_for_idle(timeout: float | None = None) -> None:
    """Test helper: block until the queue drains."""
    _queue.join()
