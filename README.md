# obomoboe

A local read-it-later list. Paste a URL, it gets added to a sortable,
taggable, read/unread list — and a full copy of the article is saved to disk so
you still have it when the original goes away.

## Running it

```sh
make install     # creates venv/, installs requirements.txt
make run         # http://127.0.0.1:5001
make test
```

Everything lives in `data/` (gitignored): `data/obomoboe.db` and
`data/archives/<article-id>/`. Back that one directory up and you have
everything.

There is no login. It binds to `127.0.0.1` and is meant to stay there — the
archive viewer renders HTML from arbitrary websites, so don't expose it to a
network you share.

## Adding articles

- Paste a URL into the box on the list page.
- Drag the bookmarklet from `/bookmarklet` to your bookmarks bar and click it
  on any page you're reading.
- `POST /add` with `url` (and optional comma-separated `tags`), form-encoded or
  JSON.

URLs are normalized before saving — scheme added, host lowercased, `utm_*`,
`fbclid` and friends stripped — so the same article pasted from two places
doesn't get saved twice.

## What gets archived

Adding returns immediately; a background worker does the fetching, and the row
updates itself in place when it lands. For each article:

```
data/archives/<id>/original.html   the page as served, scripts removed
data/archives/<id>/readable.html   sanitized reader view
data/archives/<id>/article.txt     plain text (this is what search indexes)
data/archives/<id>/meta.json       title, author, site, date, word count
data/archives/<id>/assets/         images, downloaded so they can't rot
```

Search is SQLite FTS5 over the full text, not just titles.

## Paywalls and archive.today

If the live fetch returns very little text, or trips a paywall marker
("subscribe to continue", "you've reached your…"), or returns 401/402/403/451,
the archiver retries via `archive.ph/newest/<url>` and keeps whichever copy has
more text. The article page shows which source won.

**This is best-effort.** archive.today sits behind Cloudflare and rate-limits
scripted requests — an HTTP 429 or a bot check is a normal outcome, and in
testing it happened often. When it fails, the article is still saved with
whatever the live page gave, flagged `partial`, and the error explains why.
The article page then offers a link that opens archive.today so you can create
the snapshot by hand; hit **re-archive** afterwards and it will pick it up.

Configuration is via environment variables: `OBOMOBOE_PORT`,
`OBOMOBOE_DATA_DIR`, `OBOMOBOE_MIN_WORDS` (paywall threshold, default 200),
`OBOMOBOE_ARCHIVE_PH=0` to disable the fallback, `OBOMOBOE_ARCHIVE_IMAGES=0` to
skip image downloads, `OBOMOBOE_WORKERS`.

## Layout

| file | what it does |
| --- | --- |
| `src/routes.py` | HTTP endpoints |
| `src/db.py` | SQLite schema and queries |
| `src/extract.py` | URL normalizing, fetching, readability, sanitizing, archive.today |
| `src/archiver.py` | writes snapshots to disk, background worker pool |
| `src/config.py` | settings and env overrides |
