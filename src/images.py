"""Keeping an article's pictures on disk.

An archive that still points at the publisher's servers for its images loses
them when the publisher moves or deletes them -- psmag.com's went within a
couple of years. So every picture in the reading view, and as many as fit in
the saved original page, is downloaded into the article's assets/ folder and
the page rewritten to use the copy.

Both pages share one cache, so a picture in both is fetched once, and a file
already in assets/ is never fetched again -- which is what lets articles
saved before this existed be filled in later without re-reading them.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Where lazy-loading scripts park the real address until the picture
# scrolls into view. The saved page has no scripts, so it never would.
LAZY_ATTRS = ("data-src", "data-lazy-src", "data-original", "data-lazy",
              "data-url", "data-hi-res-src")
LAZY_SRCSET_ATTRS = ("data-srcset", "data-lazy-srcset", "srcset")

DOWNLOADERS = 6


def page_base(html: str, url: str) -> str:
    """The address relative links in this page resolve against.

    Usually the page's own, unless it says otherwise with <base> -- as every
    archive.today snapshot does, pointing at the archive's image host. Read
    against the publisher's address instead, a snapshot's pictures all came
    back 404.
    """
    from .extract import _page_base

    return _page_base(BeautifulSoup(html or "", "lxml"), url)


def image_source(img: Any, base: str) -> str | None:
    """The real address of a picture, absolute, or None if it has none."""
    # A lazy-loading attribute holds the real picture; src is then only a
    # placeholder (often a data: URI, sometimes a blank gif), so it goes
    # after them.
    candidates = [value for value in
                  ((img.get(attr) or "").strip() for attr in LAZY_ATTRS)
                  if value]
    src = (img.get("src") or "").strip()
    if src and not src.lower().startswith("data:"):
        candidates.append(src)
    for attr in LAZY_SRCSET_ATTRS:
        largest = _largest_in_srcset(img.get(attr) or "")
        if largest:
            candidates.append(largest)
    for candidate in candidates:
        absolute = urljoin(base, candidate)
        if urlparse(absolute).scheme in ("http", "https"):
            return absolute
    return None


def _largest_in_srcset(srcset: str) -> str | None:
    best, best_width = None, -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        width = 0.0
        if len(bits) > 1:
            try:
                width = float(bits[1].rstrip("wx"))
            except ValueError:
                width = 0.0
        if width > best_width:
            best, best_width = bits[0], width
    return best


class ImageCache:
    """Downloads into one article's assets/ folder, once per picture."""

    def __init__(self, config: dict[str, Any], target: Path, referer: str) -> None:
        self.folder = target / "assets"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.headers = {"User-Agent": config["USER_AGENT"], "Referer": referer}
        self.max_bytes = config["MAX_IMAGE_BYTES"]
        self._saved: dict[str, str | None] = {}

    def local(self, url: str) -> str | None:
        """The saved copy's path relative to the article, if there is one."""
        if url not in self._saved:
            self._saved[url] = self._existing(url)
        return self._saved[url]

    def fetch(self, urls: list[str]) -> None:
        """Download whichever of these are not saved yet, a few at a time."""
        wanted = [url for url in dict.fromkeys(urls) if self.local(url) is None]
        if not wanted:
            return
        with ThreadPoolExecutor(max_workers=DOWNLOADERS) as pool:
            for url, path in zip(wanted, pool.map(self._download, wanted)):
                self._saved[url] = path

    def _stem(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:16]

    def _existing(self, url: str) -> str | None:
        for path in self.folder.glob(self._stem(url) + ".*"):
            return f"assets/{path.name}"
        return None

    def _download(self, url: str) -> str | None:
        try:
            with requests.get(url, headers=self.headers, timeout=15,
                              stream=True) as resp:
                if resp.status_code >= 400:
                    return None
                content_type = resp.headers.get("Content-Type", "").split(";")[0]
                if not content_type.startswith("image/"):
                    return None
                payload = bytearray()
                for chunk in resp.iter_content(64 * 1024):
                    payload += chunk
                    if len(payload) > self.max_bytes:
                        return None
        except requests.RequestException:
            return None
        if not payload:
            return None
        suffix = (mimetypes.guess_extension(content_type)
                  or Path(urlparse(url).path).suffix or ".img")
        name = self._stem(url) + suffix
        (self.folder / name).write_bytes(bytes(payload))
        return f"assets/{name}"


def localize(html: str, base: str, cache: ImageCache, limit: int,
             fragment: bool = True) -> str:
    """Point every picture in this HTML at its saved copy.

    Up to `limit` pictures are downloaded, in page order. One that cannot be
    saved keeps its absolute address, so it still shows while the publisher
    serves it. srcset and the lazy-loading attributes go: they would send
    the browser back to the publisher for a picture already on disk.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "lxml")
    images = soup.find_all("img")

    # Already pointing at a saved copy (a page filled in before): leave it.
    images = [img for img in images
              if not (img.get("src") or "").startswith("assets/")]
    sources = [image_source(img, base) for img in images]
    cache.fetch([url for url in sources if url][:limit])

    changed = False
    for img, url in zip(images, sources):
        if url is None:
            continue
        local = cache.local(url)
        new_src = local or url
        if img.get("src") != new_src:
            img["src"] = new_src
            changed = True
        for attr in ("srcset", "sizes", *LAZY_ATTRS, *LAZY_SRCSET_ATTRS):
            if img.has_attr(attr):
                del img[attr]
                changed = True
    # <picture> offers its own sources ahead of the <img>; drop them so the
    # saved copy is the one shown. Unwrapped, not removed: lxml does not
    # know <source> is empty and parses the <img> after it as its child.
    for source in soup.select("picture source"):
        source.unwrap()
        changed = True

    if not fragment:
        # Saved pictures are addressed relative to the article; a <base>
        # (every archive.today page has one) would send them to its host.
        for tag in soup.find_all("base"):
            tag.decompose()
            changed = True

    if not changed:
        return html
    if fragment:
        return soup.body.decode_contents() if soup.body else str(soup)
    return str(soup)
