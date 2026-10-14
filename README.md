# obomoboe

Save articles to read later. obomoboe keeps a full copy of each one — text
and images — on your computer, so it's still there when the original changes
or disappears. Sort, tag and search your list, and mark things as read.

## Getting it

Download the zip for your computer from the
[releases page](https://github.com/afriedman412/obomoboe/releases/latest) and
unzip it. There's nothing else to install.

- **Mac** (`obomoboe-mac-apple-silicon.zip` for M-series Macs,
  `obomoboe-mac-intel.zip` for older ones): drag `obomoboe.app` into
  Applications and open it.
- **Windows** (`obomoboe-windows.zip`): put the `obomoboe` folder somewhere
  that suits you and open `obomoboe.exe` inside it. If SmartScreen says
  "Windows protected your PC", click **More info → Run anyway**.

Opening the app opens obomoboe in a window of its own. Its **File** menu
brings the window back and quits. On a Mac, closing the window keeps
obomoboe running in the Dock (click the Dock icon to bring it back) and
**Quit** stops it; on Windows, closing the window quits. Links to other sites
open in your web browser.

## Saving articles

- **Paste a link** into the box at the top of the list.
- **Use the bookmarklet.** Choose **File → Set up the bookmarklet in your
  browser…**: it opens a page in your web browser with a button to drag to
  your bookmarks bar. Then click it on any article you're reading to save it,
  while obomoboe is open. This
  saves the page as you see it, so it works for sites you're logged into and
  sites that turn automated requests away. Wait for the article to finish
  loading before you click, and if your browser asks about pop-ups, allow
  them for that site.

Each article is saved in full: a clean reading view, the original page and
its images. Search covers the whole text, not just titles. Saving the same
article twice, even from slightly different links, finds the one you
already have.

Every save is checked for being the whole article -- against the length the
page says it is, where it says. When only part came through (a paywall, or
a page saved before it finished loading), obomoboe tries a copy from
archive.today and keeps whichever is fuller. When that doesn't work either,
the save window says so, and the article is marked **Only part of this
article came through** in the list, with a button to try archive.today
again and a link for making the snapshot yourself.

## Tags

Articles come with suggested tags, drawn from the publisher's own labels and
the words the piece leans on. They're rough but free, and work offline. Add
an Anthropic API key at **settings** and Claude refines them into something
closer to how you'd file things. That's optional, and everything else works
without it.

On the settings page you choose what happens with suggestions:

| mode | what happens |
| --- | --- |
| **off** | no suggestions |
| **auto-suggest** *(default)* | suggestions appear on each article; click one to keep it |
| **auto-apply** | suggestions are filed as tags automatically |

## Your data

Everything (the list, the saved articles, your settings) lives in one folder:
`~/Library/Application Support/obomoboe` on a Mac, `%APPDATA%\obomoboe` on
Windows. Back that folder up and you have it all. Updating the app leaves it
alone.

The API key, if you add one, is stored there in plain text. That's fine on a
computer only you use.

obomoboe only listens to your own computer, at `http://127.0.0.1:5001`. If
something else already has that address, it picks another and sticks with it;
if that happens after you've added the bookmarklet, it shows you the
bookmarklet page so you can drag in the new one. Don't open it up to a shared
network: it has no login.

## Running from source

```sh
make install     # creates venv/, installs requirements.txt
make run         # the app, in its own window (needs requirements-app.txt)
make serve       # just the server, for a browser: http://127.0.0.1:5001
make site        # the download page: http://localhost:8000
make test
make app         # builds the double-clickable app into dist/
```

Run from source, data lives in `data/` in the checkout. Settings can also be
set with `OBOMOBOE_*` environment variables; see `src/config.py`.

Pushing a `v*` tag builds, signs and publishes a release through
`.github/workflows/release.yml`; the secrets it needs are listed at the top of
that file.

| file | what it does |
| --- | --- |
| `src/routes.py` | web pages and endpoints |
| `src/db.py` | database |
| `src/extract.py` | fetching pages and pulling out the article |
| `src/archiver.py` | saving copies to disk |
| `src/tagging.py` | tag suggestions |
| `src/config.py` | settings |
| `launcher.py` | what the app runs: starts obomoboe and shows its window |
| `obomoboe.spec` | builds the app |
