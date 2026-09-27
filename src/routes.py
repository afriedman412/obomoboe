"""HTTP layer."""
from __future__ import annotations

import shutil
import sqlite3
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_from_directory, url_for)
from markupsafe import Markup

from . import archiver, db
from .extract import archive_ph_submit_url, normalize_url

bp = Blueprint("main", __name__)

# An archived page is untrusted third-party HTML. Serve it in its own opaque
# origin with scripts disabled so it can never touch this app.
SANDBOX_CSP = (
    "sandbox; default-src 'none'; img-src 'self' data:; "
    "style-src 'unsafe-inline'; font-src data:"
)


def _wants_json() -> bool:
    return (request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.accept_mimetypes.best == "application/json")


@bp.route("/")
def index():
    conn = db.get_db()
    status = request.args.get("status", "all")
    tag = request.args.get("tag") or None
    query = (request.args.get("q") or "").strip() or None
    sort = request.args.get("sort", "added_desc")

    articles = db.list_articles(conn, status=status, tag=tag, query=query,
                                sort=sort)
    return render_template(
        "index.html",
        articles=articles,
        tags=db.all_tags(conn),
        counts=db.counts(conn),
        status=status,
        tag=tag,
        query=query or "",
        sort=sort,
        sorts=db.SORTS,
    )


@bp.post("/add")
def add():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("url", "")
        tags = payload.get("tags", "")
    else:
        raw = request.form.get("url", "")
        tags = request.form.get("tags", "")

    try:
        url = normalize_url(raw)
    except ValueError as exc:
        return _add_response(None, str(exc), 400)

    conn = db.get_db()
    existing = db.find_by_url(conn, url)
    if existing is not None:
        return _add_response(existing["id"], "Already on the list", 200,
                             duplicate=True)

    try:
        article_id = db.insert_article(conn, url=url, original_url=url)
    except sqlite3.IntegrityError:
        existing = db.find_by_url(conn, url)
        return _add_response(existing["id"] if existing else None,
                             "Already on the list", 200, duplicate=True)

    for name in _split_tags(tags):
        db.add_tag(conn, article_id, name)

    archiver.enqueue(dict(current_app.config), article_id)
    return _add_response(article_id, "Added, archiving in the background", 201)


def _split_tags(raw: str) -> list[str]:
    return [part for part in (chunk.strip() for chunk in raw.split(",")) if part]


def _add_response(article_id: int | None, message: str, code: int,
                  duplicate: bool = False):
    if _wants_json() or request.is_json:
        return jsonify({"id": article_id, "message": message,
                        "duplicate": duplicate}), code
    flash(message, "error" if code >= 400 else "ok")
    return redirect(request.referrer or url_for("main.index"))


# The bookmarklet posts from the article's own origin, so the browser needs
# permission to send it and to read the reply. Scoped to this one endpoint.
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


@bp.route("/capture", methods=["POST", "OPTIONS"])
def capture():
    """Take a page already rendered by the browser.

    The live fetch runs from this machine with no session and no way past a
    bot check. The browser doing the asking has both, so for anything behind
    a challenge or a subscription this is the only thing that works.
    """
    if request.method == "OPTIONS":
        return _cors(current_app.make_response(("", 204)))

    payload = request.get_json(silent=True) or request.form
    raw = payload.get("url", "")
    html = payload.get("html", "") or ""
    tags = payload.get("tags", "") or ""

    try:
        url = normalize_url(raw)
    except ValueError as exc:
        return _cors(jsonify({"error": str(exc)})), 400

    # Clicking the bookmarklet while still on obomoboe's own page is an easy
    # mistake -- it is where you just dragged it from. Saying so beats filing
    # the app's own page as an article.
    if urlparse(url).netloc == urlparse(request.url_root).netloc:
        return _cors(jsonify({
            "error": "that is obomoboe's own page -- open the article you want "
                     "to save, then click the bookmarklet there"})), 400

    limit = current_app.config["MAX_CAPTURE_BYTES"]
    if len(html.encode("utf-8", "ignore")) > limit:
        return _cors(jsonify({"error": f"page too large (limit {limit} bytes)"})), 413
    if not html.strip():
        return _cors(jsonify({"error": "no page content sent"})), 400

    conn = db.get_db()
    existing = db.find_by_url(conn, url)
    if existing is not None:
        article_id = int(existing["id"])
    else:
        try:
            article_id = db.insert_article(conn, url=url, original_url=url)
        except sqlite3.IntegrityError:
            row = db.find_by_url(conn, url)
            if row is None:
                return _cors(jsonify({"error": "could not save"})), 500
            article_id = int(row["id"])

    # Tag whether or not the row is new -- re-capturing a page you are
    # refiling should still accept the tags you sent with it.
    for name in _split_tags(tags):
        db.add_tag(conn, article_id, name)

    path = archiver.capture_path(dict(current_app.config), article_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")

    db.update_article(conn, article_id, archive_status=db.PENDING,
                      archive_error=None)
    queued = archiver.enqueue(dict(current_app.config), article_id,
                              from_capture=True)
    return _cors(jsonify({"id": article_id, "queued": queued,
                          "message": "Captured from your browser"})), 201


@bp.route("/a/<int:article_id>")
def article(article_id: int):
    conn = db.get_db()
    row = db.get_article(conn, article_id)
    if row is None:
        abort(404)

    record = dict(row)
    record["tags"] = db.tags_for_articles(conn, [article_id]).get(article_id, [])

    body = ""
    path = archiver.article_dir(current_app.config, article_id) / "readable.html"
    if path.exists():
        body = _rewrite_asset_urls(path.read_text(encoding="utf-8"), article_id)

    return render_template(
        "article.html",
        article=record,
        body=Markup(body),
        has_original=(path.parent / "original.html").exists(),
        submit_url=archive_ph_submit_url(record["original_url"]),
    )


def _rewrite_asset_urls(html: str, article_id: int) -> str:
    """Point locally-saved images at this article's asset route."""
    if "assets/" not in html:
        return html
    soup = BeautifulSoup(html, "lxml")
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if src.startswith("assets/"):
            img["src"] = url_for("main.asset", article_id=article_id,
                                 filename=src[len("assets/"):])
    return soup.body.decode_contents() if soup.body else str(soup)


@bp.route("/a/<int:article_id>/original")
def original(article_id: int):
    """The raw snapshot, sandboxed."""
    directory = archiver.article_dir(current_app.config, article_id)
    if not (directory / "original.html").exists():
        abort(404)
    response = send_from_directory(directory, "original.html")
    response.headers["Content-Security-Policy"] = SANDBOX_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.route("/a/<int:article_id>/assets/<path:filename>")
def asset(article_id: int, filename: str):
    directory = archiver.article_dir(current_app.config, article_id) / "assets"
    if not directory.exists():
        abort(404)
    response = send_from_directory(directory, filename)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.post("/a/<int:article_id>/status")
def set_status(article_id: int):
    conn = db.get_db()
    row = db.get_article(conn, article_id)
    if row is None:
        abort(404)
    target = request.form.get("status")
    if target not in (db.READ, db.UNREAD):
        target = db.READ if row["status"] == db.UNREAD else db.UNREAD
    db.set_status(conn, article_id, target)

    if _wants_json():
        return jsonify({"id": article_id, "status": target})
    return redirect(request.referrer or url_for("main.index"))


@bp.post("/a/<int:article_id>/tags")
def edit_tags(article_id: int):
    conn = db.get_db()
    if db.get_article(conn, article_id) is None:
        abort(404)

    for name in _split_tags(request.form.get("add", "")):
        db.add_tag(conn, article_id, name)
    remove = request.form.get("remove", "").strip()
    if remove:
        db.remove_tag(conn, article_id, remove)

    tags = db.tags_for_articles(conn, [article_id]).get(article_id, [])
    if _wants_json():
        return jsonify({"id": article_id, "tags": tags})
    return redirect(request.referrer or url_for("main.article",
                                                article_id=article_id))


@bp.post("/a/<int:article_id>/notes")
def edit_notes(article_id: int):
    conn = db.get_db()
    if db.get_article(conn, article_id) is None:
        abort(404)
    db.update_article(conn, article_id, notes=request.form.get("notes", ""))
    return redirect(url_for("main.article", article_id=article_id))


@bp.post("/a/<int:article_id>/rearchive")
def rearchive(article_id: int):
    conn = db.get_db()
    if db.get_article(conn, article_id) is None:
        abort(404)
    force = request.form.get("source") == "archive.ph"
    # Re-reading a page your browser handed over means re-reading that page,
    # not going back to a server that could not fetch it in the first place.
    recapture = (not force
                 and archiver.capture_path(dict(current_app.config),
                                           article_id).exists())
    db.update_article(conn, article_id, archive_status=db.PENDING,
                      archive_error=None)
    # A run already underway will write the row when it lands, so the article
    # never gets stranded as pending -- but say so rather than pretending the
    # click started the run you asked for.
    queued = archiver.enqueue(dict(current_app.config), article_id,
                              force_archive_ph=force, from_capture=recapture)

    if _wants_json():
        return jsonify({"id": article_id, "archive_status": db.PENDING,
                        "queued": queued, "from_capture": recapture})
    flash("Re-archiving in the background" if queued
          else "Already archiving -- wait for the run in progress to finish",
          "ok")
    return redirect(request.referrer or url_for("main.article",
                                                article_id=article_id))


@bp.post("/a/<int:article_id>/delete")
def delete(article_id: int):
    conn = db.get_db()
    if db.get_article(conn, article_id) is None:
        abort(404)
    db.delete_article(conn, article_id)
    shutil.rmtree(archiver.article_dir(current_app.config, article_id),
                  ignore_errors=True)

    if _wants_json():
        return jsonify({"id": article_id, "deleted": True})
    flash("Deleted", "ok")
    return redirect(url_for("main.index"))


@bp.route("/api/articles")
def api_articles():
    """Used by the page to refresh rows that are still archiving."""
    conn = db.get_db()
    raw_ids = (request.args.get("ids") or "").strip()
    if raw_ids:
        ids = [int(part) for part in raw_ids.split(",") if part.strip().isdigit()]
        if not ids:
            return jsonify({"articles": []})
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM articles WHERE id IN ({placeholders})", ids
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM articles ORDER BY added_at DESC"
        ).fetchall()

    articles = [dict(row) for row in rows]
    tag_map = db.tags_for_articles(conn, [a["id"] for a in articles])
    for item in articles:
        item["tags"] = tag_map.get(item["id"], [])
    return jsonify({"articles": articles, "counts": db.counts(conn)})


@bp.route("/bookmarklet")
def bookmarklet():
    base = request.url_root.rstrip("/")
    # Sends the page as your browser rendered it -- logged in, and past any
    # bot check -- instead of asking the server to go and fetch it blind.
    code = (
        "javascript:(function(){"
        "fetch('" + base + "/capture',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({url:location.href,"
        "html:document.documentElement.outerHTML})})"
        ".then(function(r){return r.json()})"
        ".then(function(d){alert(d.error?('obomoboe: '+d.error):"
        "'Saved to obomoboe')})"
        ".catch(function(e){alert('obomoboe: '+e)});})();"
    )
    return render_template("bookmarklet.html", code=code, base=base)
