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

- Paste a URL into the box on the list page. The server then goes and fetches
  it.
- Drag the bookmarklet from `/bookmarklet` to your bookmarks bar and click it
  on any page you're reading. This sends the page **as your browser has it**
  rather than asking the server to fetch it — see below.
- `POST /add` with `url` (and optional comma-separated `tags`), form-encoded or
  JSON.
- `POST /capture` with `url` and `html` (and optional `tags`) to hand over a
  page you already have.

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

## Capturing from your browser

A server-side fetch runs from this machine with no session and no way through
a bot check. Your browser has both. So the bookmarklet posts
`document.documentElement.outerHTML` to `/capture`, and the archiver extracts
from that instead of fetching anything.

This is the only thing that works for sites that refuse the server outright.
Medium, for instance, answers a plain fetch with HTTP 403 and
`cf-mitigated: challenge`, and answers a headless browser with "Sorry, you
have been blocked" — but it renders fine in the browser you're logged into.
The same applies to anything you subscribe to: the copy that gets archived is
the copy you can see.

Captured articles show as `via your browser`. Capturing a page while the bot
check is still on screen is rejected rather than filed as the article, so wait
for the piece to load before clicking.

`/capture` sends CORS headers, because the bookmarklet posts from the
article's own origin. That means any page you visit while the app is running
could also post to it — the app binds to `127.0.0.1`, and captured HTML is
sanitized and sandboxed exactly like fetched HTML, but it is the reason this
stays off any network you share.

## Tags

Every archived article gets candidate tags from two places that cost nothing
and work offline: whatever the publisher declared (`article:tag`, JSON-LD
`keywords` — only about a quarter of pages carry them) and the terms the text
itself leans on, by frequency against a stoplist. The second is never clever.
It finds *causal inference* and *street harassment*; it will also offer you
*systems*.

With an Anthropic API key, Claude reads the article and refines those
candidates into something closer to how you would file it. It is entirely
optional — without a key the offline tags carry on working, and if the call
fails the article still archives with them.

Tags are **suggested, not applied**: they appear as dashed chips on the
article and in the list, and clicking one keeps it. Your taxonomy stays
yours. Turn on *Apply tags automatically* in settings if you would rather
they were filed for you.

Configure all of it at `/settings` — the key, the toggles, how many
suggestions per article, and which model. The key is stored in plain text in
`data/obomoboe.db`, like everything else here; that is fine on a machine only
you use, and it is not a secret store. An `ANTHROPIC_API_KEY` in the
environment works too; the settings page takes precedence over it.

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
`OBOMOBOE_MAX_CAPTURE_BYTES` (default 12MB), `OBOMOBOE_AUTO_APPLY_TAGS`,
`OBOMOBOE_TAG_MODEL`, `OBOMOBOE_TAG_SUGGESTIONS`, `OBOMOBOE_LLM_TAGS=0`,
`OBOMOBOE_DATA_DIR`, `OBOMOBOE_MIN_WORDS` (paywall threshold, default 200),
`OBOMOBOE_ARCHIVE_PH=0` to disable the fallback, `OBOMOBOE_ARCHIVE_IMAGES=0` to
skip image downloads, `OBOMOBOE_WORKERS`.

## Layout

| file | what it does |
| --- | --- |
| `src/routes.py` | HTTP endpoints, including `/capture` |
| `src/db.py` | SQLite schema and queries |
| `src/extract.py` | URL normalizing, fetching, readability, sanitizing, archive.today |
| `src/archiver.py` | writes snapshots to disk, background worker pool |
| `src/tagging.py` | candidate tags: publisher, keywords, Claude |
| `src/config.py` | settings and env overrides |
