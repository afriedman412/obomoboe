"""SQLite storage. One file, WAL mode, FTS5 for full-text search."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY,
    url             TEXT NOT NULL UNIQUE,
    original_url    TEXT NOT NULL,
    title           TEXT,
    author          TEXT,
    site_name       TEXT,
    excerpt         TEXT,
    published_at    TEXT,
    added_at        TEXT NOT NULL,
    read_at         TEXT,
    status          TEXT NOT NULL DEFAULT 'unread',
    word_count      INTEGER NOT NULL DEFAULT 0,
    archive_status  TEXT NOT NULL DEFAULT 'pending',
    archive_source  TEXT,
    archive_error   TEXT,
    archived_at     TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    suggested_tags  TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_articles_added ON articles(added_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id);

-- Settings the UI can change, so a key or a toggle does not mean editing the
-- environment and restarting.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- Standalone (not external-content) so it survives article edits simply.
CREATE VIRTUAL TABLE IF NOT EXISTS article_search USING fts5(
    title, body, article_id UNINDEXED
);
"""

READ = "read"
UNREAD = "unread"

PENDING = "pending"
OK = "ok"
FAILED = "failed"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas this app relies on."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so do it by hand.
ADDED_COLUMNS = (
    ("articles", "suggested_tags", "TEXT NOT NULL DEFAULT ''"),
)


def init_db(db_path: Path | str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for table, column, decl in ADDED_COLUMNS:
            existing = {row["name"] for row in
                        conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    """Request-scoped connection."""
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# --------------------------------------------------------------------------
# Articles
# --------------------------------------------------------------------------


def insert_article(conn: sqlite3.Connection, url: str, original_url: str,
                   title: str | None = None) -> int:
    """Insert a stub row. Raises sqlite3.IntegrityError if the URL exists."""
    cur = conn.execute(
        """
        INSERT INTO articles (url, original_url, title, added_at, status,
                              archive_status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (url, original_url, title, utcnow(), UNREAD, PENDING),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_article(conn: sqlite3.Connection, article_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM articles WHERE id = ?", (article_id,)
    ).fetchone()


def find_by_url(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM articles WHERE url = ?", (url,)).fetchone()


def update_article(conn: sqlite3.Connection, article_id: int, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [article_id]
    conn.execute(f"UPDATE articles SET {columns} WHERE id = ?", values)
    conn.commit()


def delete_article(conn: sqlite3.Connection, article_id: int) -> None:
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.execute("DELETE FROM article_search WHERE article_id = ?", (article_id,))
    conn.commit()


def set_status(conn: sqlite3.Connection, article_id: int, status: str) -> None:
    read_at = utcnow() if status == READ else None
    update_article(conn, article_id, status=status, read_at=read_at)


def index_article(conn: sqlite3.Connection, article_id: int, title: str,
                  body: str) -> None:
    conn.execute("DELETE FROM article_search WHERE article_id = ?", (article_id,))
    conn.execute(
        "INSERT INTO article_search (title, body, article_id) VALUES (?, ?, ?)",
        (title or "", body or "", article_id),
    )
    conn.commit()


SORTS = {
    "added_desc": "a.added_at DESC",
    "added_asc": "a.added_at ASC",
    "title": "COALESCE(NULLIF(a.title, ''), a.url) COLLATE NOCASE ASC",
    "site": "COALESCE(a.site_name, '') COLLATE NOCASE ASC, a.added_at DESC",
    "published": "COALESCE(a.published_at, '') DESC, a.added_at DESC",
    "longest": "a.word_count DESC",
    "shortest": "a.word_count ASC",
}


def list_articles(conn: sqlite3.Connection, status: str = "all",
                  tag: str | None = None, query: str | None = None,
                  sort: str = "added_desc") -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    if status in (READ, UNREAD):
        where.append("a.status = ?")
        params.append(status)

    if tag:
        where.append(
            "a.id IN (SELECT at.article_id FROM article_tags at "
            "JOIN tags t ON t.id = at.tag_id WHERE t.name = ?)"
        )
        params.append(tag)

    if query:
        ids = search_ids(conn, query)
        if not ids:
            return []
        where.append(f"a.id IN ({','.join('?' * len(ids))})")
        params.extend(ids)

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = SORTS.get(sort, SORTS["added_desc"])
    rows = conn.execute(
        f"SELECT a.* FROM articles a {clause} ORDER BY {order}", params
    ).fetchall()

    articles = [dict(row) for row in rows]
    tag_map = tags_for_articles(conn, [a["id"] for a in articles])
    for article in articles:
        article["tags"] = tag_map.get(article["id"], [])
        article["suggestions"] = get_suggestions(article)
    return articles


def search_ids(conn: sqlite3.Connection, query: str) -> list[int]:
    """FTS5 match, falling back to LIKE when the query isn't valid FTS syntax."""
    try:
        rows = conn.execute(
            "SELECT article_id FROM article_search WHERE article_search MATCH ? "
            "ORDER BY rank",
            (query,),
        ).fetchall()
        return [int(r["article_id"]) for r in rows]
    except sqlite3.OperationalError:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT id FROM articles WHERE title LIKE ? OR url LIKE ? "
            "OR excerpt LIKE ?",
            (like, like, like),
        ).fetchall()
        return [int(r["id"]) for r in rows]


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------


def normalize_tag(name: str) -> str:
    return " ".join(name.strip().lower().split())


def add_tag(conn: sqlite3.Connection, article_id: int, name: str) -> str | None:
    tag = normalize_tag(name)
    if not tag:
        return None
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
        (article_id, row["id"]),
    )
    conn.commit()
    return tag


def remove_tag(conn: sqlite3.Connection, article_id: int, name: str) -> None:
    tag = normalize_tag(name)
    conn.execute(
        "DELETE FROM article_tags WHERE article_id = ? AND tag_id = "
        "(SELECT id FROM tags WHERE name = ?)",
        (article_id, tag),
    )
    conn.commit()


def tags_for_articles(conn: sqlite3.Connection,
                      article_ids: Iterable[int]) -> dict[int, list[str]]:
    ids = list(article_ids)
    if not ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT at.article_id, t.name FROM article_tags at
        JOIN tags t ON t.id = at.tag_id
        WHERE at.article_id IN ({','.join('?' * len(ids))})
        ORDER BY t.name
        """,
        ids,
    ).fetchall()
    out: dict[int, list[str]] = {}
    for row in rows:
        out.setdefault(int(row["article_id"]), []).append(row["name"])
    return out


def all_tags(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.name, COUNT(at.article_id) AS n FROM tags t
        JOIN article_tags at ON at.tag_id = t.id
        GROUP BY t.id ORDER BY n DESC, t.name
        """
    ).fetchall()
    return [{"name": r["name"], "count": int(r["n"])} for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'unread' THEN 1 ELSE 0 END) AS unread,
            SUM(CASE WHEN status = 'read' THEN 1 ELSE 0 END) AS read
        FROM articles
        """
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "unread": int(row["unread"] or 0),
        "read": int(row["read"] or 0),
    }


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# What the settings page may write, and what each becomes in the app config.
SETTING_KEYS = ("ANTHROPIC_API_KEY", "LLM_TAGS_ENABLED", "AUTO_APPLY_TAGS",
                "TAG_MODEL", "TAG_SUGGESTIONS")

BOOL_SETTINGS = ("LLM_TAGS_ENABLED", "AUTO_APPLY_TAGS")
INT_SETTINGS = ("TAG_SUGGESTIONS",)


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Stored settings, typed. Absent keys are simply missing, so a caller
    can tell "never set" from "set to off"."""
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        key, raw = row["key"], row["value"]
        if key not in SETTING_KEYS:
            continue
        if key in BOOL_SETTINGS:
            out[key] = raw == "1"
        elif key in INT_SETTINGS:
            try:
                out[key] = int(raw)
            except ValueError:
                continue
        elif raw != "":
            out[key] = raw
    return out


def set_settings(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key not in SETTING_KEYS:
            continue
        if key in BOOL_SETTINGS:
            stored = "1" if value else "0"
        else:
            stored = "" if value is None else str(value)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, stored),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Suggested tags
# --------------------------------------------------------------------------


def get_suggestions(row: sqlite3.Row | dict[str, Any]) -> list[str]:
    raw = (row["suggested_tags"] if "suggested_tags" in row.keys()
           else "") if hasattr(row, "keys") else row.get("suggested_tags", "")
    return [part for part in (chunk.strip() for chunk in (raw or "").split(","))
            if part]


def set_suggestions(conn: sqlite3.Connection, article_id: int,
                    tags: Iterable[str]) -> None:
    update_article(conn, article_id, suggested_tags=",".join(tags))


def drop_suggestion(conn: sqlite3.Connection, article_id: int,
                    name: str) -> list[str]:
    """Remove one suggestion, e.g. because it was accepted. Returns the rest."""
    row = get_article(conn, article_id)
    if row is None:
        return []
    target = normalize_tag(name)
    remaining = [t for t in get_suggestions(row) if normalize_tag(t) != target]
    set_suggestions(conn, article_id, remaining)
    return remaining
