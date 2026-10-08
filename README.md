# obomoboe

A read-it-later list that keeps its own copy. Save an article and it goes on
a list you can sort, tag, search and mark as read, and the whole piece,
images included, is kept on your computer, so you still have it after the
original changes or disappears.

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

Opening the app opens obomoboe in your browser. It has no window of its own:
to get back to it, open the app again or bookmark the page. To stop it, click
**quit** at the top of the page.

## Saving articles

- **Paste a link** into the box at the top of the list.
- **Use the bookmarklet.** The **bookmarklet** page has a button to drag to
  your bookmarks bar; click it on any article you're reading to save it. This
  saves the page as you see it, so it works for sites you're logged into and
  sites that turn automated requests away. Wait for the article to finish
  loading before you click, and if your browser asks about pop-ups, allow
  them for that site.

Each article is saved in full: a clean reading view, the original page and
its images. Search covers the whole text, not just titles. Saving the same
article twice, even from slightly different links, finds the one you
already have.

When a link only returns a paywall or a stub, obomoboe tries a copy from
archive.today instead. That doesn't always work; when it doesn't, the
article is marked as partial and its page has a link for making the snapshot
yourself, after which **re-archive** picks it up.

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
make run         # http://127.0.0.1:5001
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
| `launcher.py` | what the app runs: starts obomoboe and opens the browser |
| `obomoboe.spec` | builds the app |
