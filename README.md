# obomoboe

A local read-it-later list. Paste a URL, it gets added to a sortable,
taggable, read/unread list — and a full copy of the article is saved to disk so
you still have it when the original goes away.

## Getting it

Download the zip for your computer from the
[releases page](https://github.com/afriedman412/obomoboe/releases/latest) and
unzip it. Nothing else to install — Python and everything it needs are inside.

- **Mac** (`obomoboe-mac-apple-silicon.zip` for M-series Macs,
  `obomoboe-mac-intel.zip` for older ones): drag `obomoboe.app` into
  Applications and open it. The first time, macOS says it can't check the app
  for malicious software, because it isn't signed with an Apple developer
  certificate. Open **System Settings → Privacy & Security**, scroll down to
  the message about obomoboe and click **Open Anyway**. After that it opens
  normally.
- **Windows** (`obomoboe-windows.zip`): put the `obomoboe` folder somewhere
  that suits you and open `obomoboe.exe` inside it. If SmartScreen says
  "Windows protected your PC", click **More info → Run anyway**.

Opening the app starts obomoboe in the background and opens it in your
browser. There's no window of its own: to get back to it, open the app again
or bookmark the page. To stop it, click **quit** at the top of the page.

The app keeps its data in `~/Library/Application Support/obomoboe` on a Mac
and `%APPDATA%\obomoboe` on Windows. Back up that folder and you have
everything. Updating means replacing the app; the data stays where it is.

It runs at `http://127.0.0.1:5001`. If something else already has that port,
it takes the next free one and keeps using it from then on, since the
bookmarklet has the address built in. If it ever has to move after you've
added the bookmarklet, it opens the bookmarklet page so you can drag the new
one in.

## Running from source

```sh
make install     # creates venv/, installs requirements.txt
make run         # http://127.0.0.1:5001
make test
make app         # builds the double-clickable app into dist/
```

Run this way, everything lives in `data/` (gitignored): `data/obomoboe.db`
and `data/archives/<article-id>/`. `python launcher.py` behaves the way the
packaged app does, with a background server and a quit button, but keeps
using `data/`.

Pushing a `v*` tag runs `.github/workflows/release.yml`, which tests and
builds the app for Apple Silicon Macs, Intel Macs and Windows and attaches
the zips to a GitHub release.

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

Before the readability pass, page furniture is cut out of the document:
`nav`, `footer`, `aside`, `dialog` and `form` elements, anything with a
landmark role such as `complementary` or `navigation`, hidden elements, and
containers whose id, class or `data-testid` names them as a menu,
recirculation list, newsletter box, comment thread, ad slot or the like.
Names are matched as whole words, a name containing "not" is ignored, and
nothing holding most of the page's text is ever removed, so an article that
happens to sit in `<article class="newsletter-post">` or a
`pmc-not-a-paywall` div survives. This exists because nytimes.com renders its
entire mobile menu, 900 words of blurbs, into every page, and readability
heuristics happily took that as the article.

## Capturing from your browser

A server-side fetch runs from this machine with no session and no way through
a bot check. Your browser has both. So the bookmarklet hands
`document.documentElement.outerHTML` to `/capture`, and the archiver extracts
from that instead of fetching anything.

It does this through a small pop-up window on the app's own origin
(`/capture/window`) rather than posting straight from the article's page. A
fetch from the page is subject to that page's Content-Security-Policy, and a
strict `connect-src` — nytimes.com allows only `https:` — blocks a request to
`http://127.0.0.1` before it leaves the browser, which surfaces as
`TypeError: Failed to fetch`. `postMessage` into a window on our origin is
not governed by CSP, and from that window the post is same-origin. If your
browser blocks the pop-up, allow pop-ups for that site.

This is the only thing that works for sites that refuse the server outright.
Medium, for instance, answers a plain fetch with HTTP 403 and
`cf-mitigated: challenge`, and answers a headless browser with "Sorry, you
have been blocked" — but it renders fine in the browser you're logged into.
The same applies to anything you subscribe to: the copy that gets archived is
the copy you can see.

Saving from an archive.today snapshot (archive.ph, archive.is and the other
mirrors) files the article under the publisher's own URL, read from the
snapshot's canonical link. The list shows the publisher's site, the article
page links to the original as "from", and the snapshot is kept beside it as
"archive". A snapshot link pasted into the box works the same way once it has
been archived, and adding the publisher's URL afterwards finds the same
article rather than a second copy. When the server itself falls back to
archive.today, the snapshot it used is recorded the same way.

archive.today also rewrites the page's title and date tags: titles are cut at
70 characters and every date is set to the moment of capture. For snapshots
those tags are ignored. The title is completed from the page's own `<title>`
or headline and loses a trailing site name such as "| WIRED". The publication
date is the first date on the archived page that falls before the capture
and, for news URLs carrying a date, within a few days of it. That skips
embedded posts and sidebar lists. With no such date, the URL's date is used,
and failing that the date is left blank rather than showing the capture time.

Captured articles show as `via your browser`. Capturing a page while the bot
check is still on screen is rejected rather than filed as the article, so wait
for the piece to load before clicking.

`/capture` also sends CORS headers, so a page on another origin can post to
it directly (the bookmarklet used to). That means any page you visit while
the app is running could post to it — the app binds to `127.0.0.1`, and
captured HTML is sanitized and sandboxed exactly like fetched HTML, but it is
the reason this stays off any network you share.

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

Three modes, set at `/settings`:

| mode | what happens |
| --- | --- |
| **off** | no tags are worked out at all |
| **auto-suggest** *(default)* | candidates appear as dashed chips on the article and in the list; clicking one keeps it |
| **auto-apply** | tags are filed for you as articles land |

The default is auto-suggest because your tag list then holds only what you
chose. Auto-apply is quicker, but it fills up with whatever the pages say and
removing them is one at a time.

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
`OBOMOBOE_MAX_CAPTURE_BYTES` (default 12MB),
`OBOMOBOE_TAG_MODE` (`off`/`suggest`/`apply`), `OBOMOBOE_TAG_MODEL`, `OBOMOBOE_TAG_SUGGESTIONS`, `OBOMOBOE_LLM_TAGS=0`,
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
| `launcher.py` | what the packaged app runs: starts the server, opens the browser |
| `obomoboe.spec` | PyInstaller build of the app |
