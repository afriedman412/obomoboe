"""Fetching a URL and turning it into archivable content.

Nothing here touches the database or Flask -- it is all pure-ish functions so
it can be exercised from tests without a server.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import (parse_qsl, quote, urlencode, urljoin, urlparse,
                          urlunparse)

import requests
import trafilatura
from bs4 import BeautifulSoup

# Query params that are pure tracking noise; dropped when de-duplicating URLs.
TRACKING_PARAMS = re.compile(
    r"^(utm_|ic[id]$|mc_[ce]id$|fbclid$|gclid$|igshid$|mkt_tok$|ref$|"
    r"ref_src$|s_campaign$|cmpid$|smid$|partner$|__twitter_impression$)",
    re.IGNORECASE,
)

HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscribe to keep reading",
    "already a subscriber",
    "already have an account",
    "this article is for subscribers",
    "for subscribers only",
    "create a free account to",
    "you have reached your",
    "you've reached your",
    "your free articles",
    "free articles remaining",
    "sign in to continue reading",
    "register to continue",
    "become a member to",
    "unlock this article",
    "continue reading your article",
)

# Bot-check signatures. Machine-facing identifiers -- script endpoints, JS
# globals and cookie names -- because the human-readable copy on these pages
# changes freely and is localized.
CHALLENGE_MARKUP = (
    # Options blob Cloudflare only emits on an actual interstitial. Its
    # invisible bot-management JS (/cdn-cgi/challenge-platform, __cf$cv$params)
    # is injected into ordinary pages too, so matching on that flagged a
    # perfectly good article as a bot check -- keep to the challenge-only bits.
    "_cf_chl_opt",
    "window._cf_chl",
    "captcha-delivery.com",          # DataDome captcha page
    "px-captcha",                    # PerimeterX captcha page, not its sensor
    "queue-it.net",                  # Queue-it waiting room
    "bm-verify",                     # Akamai challenge body
    "cf-error",                      # Cloudflare block page (error 1020 etc.)
)


ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "dd",
    "del", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p",
    "pre", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u", "ul", "var",
}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "time": {"datetime"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

SAFE_URL_SCHEMES = {"http", "https", "mailto", ""}


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved at all."""


@dataclass
class Extracted:
    """Everything we learned about a page."""

    url: str
    html: str = ""
    readable_html: str = ""
    text: str = ""
    title: str | None = None
    author: str | None = None
    site_name: str | None = None
    excerpt: str | None = None
    published_at: str | None = None
    status_code: int = 0
    source: str = "direct"
    images: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def metadata(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "site_name": self.site_name,
            "excerpt": self.excerpt,
            "published_at": self.published_at,
            "word_count": self.word_count,
            "source": self.source,
            "status_code": self.status_code,
        }


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------


def normalize_url(raw: str) -> str:
    """Canonical form used for de-duplication. Raises ValueError if unusable."""
    candidate = (raw or "").strip()
    if not candidate:
        raise ValueError("No URL given")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", candidate):
        if not re.match(r"^https?://", candidate, re.IGNORECASE):
            scheme = candidate.split(":", 1)[0]
            raise ValueError(f"Unsupported URL scheme: {scheme!r}")
    else:
        candidate = "https://" + candidate

    parts = urlparse(candidate)
    if not parts.netloc:
        raise ValueError(f"Not a URL: {raw!r}")

    netloc = parts.netloc.lower()
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    if not HOSTNAME.match(host) or ("." not in host and host != "localhost"):
        raise ValueError(f"Not a URL: {raw!r}")

    if netloc.endswith(":80") and parts.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and parts.scheme == "https":
        netloc = netloc[:-4]

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAMS.match(key)
    ]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((parts.scheme, netloc, path, parts.params,
                       urlencode(kept), ""))


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# Sanitizing
# --------------------------------------------------------------------------


def _safe_url(value: str | None) -> bool:
    if not value:
        return False
    scheme = urlparse(value.strip()).scheme.lower()
    if scheme in SAFE_URL_SCHEMES:
        return True
    # Inline images are fine; data:text/html is not.
    return value.strip().lower().startswith("data:image/")


def sanitize_fragment(html: str) -> str:
    """Whitelist-filter extracted HTML before it is rendered same-origin."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    if soup.body:
        soup = BeautifulSoup(soup.body.decode_contents(), "lxml")

    for tag in soup.find_all(["script", "style", "iframe", "object", "embed",
                              "form", "input", "button", "svg", "link",
                              "meta", "noscript"]):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed = ALLOWED_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag[attr]
        if tag.name == "a" and not _safe_url(tag.get("href")):
            del tag["href"]
        if tag.name == "img" and not _safe_url(tag.get("src")):
            tag.decompose()

    return soup.body.decode_contents() if soup.body else str(soup)


def strip_scripts(html: str) -> str:
    """Light pass over a raw snapshot -- keeps layout, drops active content."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "noscript", "iframe", "object",
                              "embed", "form"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag[attr]
    return str(soup)


# --------------------------------------------------------------------------
# Fetch + extract
# --------------------------------------------------------------------------


class Fetched(NamedTuple):
    """What a request gave back. Headers are kept because a bot check is far
    easier to recognize from them than from the page body."""

    html: str
    status_code: int
    url: str
    headers: dict[str, str]


def fetch(url: str, user_agent: str, timeout: int = 25,
          session: requests.Session | None = None) -> Fetched:
    """Return the body, status, final URL and response headers."""
    sess = session or requests.Session()
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = sess.get(url, headers=headers, timeout=timeout,
                        allow_redirects=True)
    except requests.RequestException as exc:
        raise FetchError(str(exc)) from exc

    content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type and "xml" not in content_type:
        raise FetchError(f"Not an HTML page (Content-Type: {content_type!r})")
    return Fetched(resp.text, resp.status_code, resp.url,
                   dict(resp.headers))


def _meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = (soup.find("meta", attrs={"property": name})
               or soup.find("meta", attrs={"name": name}))
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def extract(html: str, url: str, status_code: int = 200,
            source: str = "direct",
            headers: dict[str, str] | None = None) -> Extracted:
    """Run readability extraction and pull metadata out of a page."""
    result = Extracted(url=url, html=html, status_code=status_code,
                       source=source, headers=dict(headers or {}))

    readable = trafilatura.extract(
        html, url=url, output_format="html", include_links=True,
        include_images=True, include_formatting=True, include_tables=True,
        favor_recall=True,
    ) or ""
    result.text = trafilatura.extract(html, url=url, favor_recall=True) or ""

    soup = BeautifulSoup(html, "lxml")

    # A single-container reading can miss most of a block-built article.
    blocks = collect_blocks(soup)
    block_text = BeautifulSoup(blocks, "lxml").get_text(" ", strip=True)
    if len(block_text.split()) > len(result.text.split()) * 1.4:
        readable, result.text = blocks, block_text

    result.readable_html = sanitize_fragment(readable)

    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # trafilatura raises assorted parse errors
        meta = None

    if meta is not None:
        result.title = meta.title or None
        result.author = meta.author or None
        result.site_name = meta.sitename or None
        result.excerpt = meta.description or None
        result.published_at = meta.date or None

    if not result.title:
        og_title = _meta_content(soup, "og:title", "twitter:title")
        if og_title:
            result.title = og_title
        elif soup.title and soup.title.string:
            result.title = soup.title.string.strip()
    if not result.excerpt:
        result.excerpt = _meta_content(soup, "og:description", "description")
    if not result.site_name:
        result.site_name = _meta_content(soup, "og:site_name") or domain_of(url)
    if not result.published_at:
        result.published_at = _meta_content(
            soup, "article:published_time", "datePublished"
        )

    if not result.text and result.readable_html:
        result.text = BeautifulSoup(result.readable_html, "lxml").get_text(" ")

    result.images = _image_urls(result.readable_html, url)
    return result


def _image_urls(fragment: str, base_url: str) -> list[str]:
    if not fragment:
        return []
    soup = BeautifulSoup(fragment, "lxml")
    urls: list[str] = []
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if absolute not in urls and urlparse(absolute).scheme in ("http", "https"):
            urls.append(absolute)
    return urls


# Block-based CMS layouts (Webflow and friends) split one article across many
# sibling containers. Readability heuristics score a single best container and
# return only that, so the rest of the piece is silently dropped -- one article
# came back as 444 of its 1279 words, starting mid-sentence. When that happens,
# collect the blocks ourselves and keep whichever copy recovered more.

BOILERPLATE_HINTS = (
    "newsletter", "subscribe", "signup", "sign-up", "related", "recirc",
    "footer", "nav", "menu", "social", "share", "comment", "promo",
    "advert", "cookie", "banner", "sidebar", "breadcrumb", "caption",
)

BLOCK_TAGS = ("p", "blockquote", "h2", "h3", "h4", "li", "pre")


def _is_chrome(tag: Any) -> bool:
    """Is this element page furniture rather than the article?"""
    for element in [tag, *list(tag.parents)[:6]]:
        name = getattr(element, "name", "") or ""
        if name in ("nav", "footer", "header", "aside", "form"):
            return True
        classes = element.get("class") or [] if hasattr(element, "get") else []
        ident = " ".join(list(classes) + [str(element.get("id") or "")]).lower()
        if any(hint in ident for hint in BOILERPLATE_HINTS):
            return True
    return False


def collect_blocks(soup: BeautifulSoup) -> str:
    """Gather the article's text blocks in document order, skipping chrome."""
    kept: list[str] = []
    seen: set[int] = set()
    for tag in soup.find_all(BLOCK_TAGS):
        if id(tag) in seen or _is_chrome(tag):
            continue
        # Skip a block already covered by one we kept (nested li inside li).
        if any(id(parent) in seen for parent in tag.parents):
            continue
        text = tag.get_text(" ", strip=True)
        if len(text.split()) < 4:
            continue
        seen.add(id(tag))
        kept.append(str(tag))
    return "".join(kept)


def looks_paywalled(result: Extracted, min_words: int = 200) -> bool:
    """Heuristic: did we get a stub instead of the article?"""
    if result.status_code in (401, 402, 403, 451):
        return True
    # A live page can serve the same bot check archive.today does -- "enable
    # JavaScript and cookies to continue". Without this the interstitial gets
    # archived as though it were the article.
    if is_challenge_page(result.html, result.headers):
        return True
    haystack = f"{result.text}\n{result.excerpt or ''}".lower()
    if any(marker in haystack for marker in PAYWALL_MARKERS):
        return True
    # Little text and no obvious marker still smells like a stub.
    return result.word_count < min_words


# --------------------------------------------------------------------------
# archive.today
# --------------------------------------------------------------------------


def archive_ph_submit_url(url: str, host: str = "archive.ph") -> str:
    """The page a human can open to create a snapshot by hand."""
    return f"https://{host}/?run=1&url={quote(url, safe='')}"


def fetch_from_archive_ph(url: str, user_agent: str, hosts: list[str],
                          timeout: int = 25,
                          session: requests.Session | None = None
                          ) -> tuple[str, int, str]:
    """Pull the newest archive.today snapshot for `url`.

    archive.today sits behind Cloudflare and frequently refuses scripted
    requests; every failure mode ends up as a FetchError with a readable
    message so the UI can offer the manual link instead.
    """
    errors: list[str] = []
    for host in hosts:
        snapshot_url = f"https://{host}/newest/{url}"
        try:
            fetched = fetch(snapshot_url, user_agent, timeout, session)
            html, status, final_url = (fetched.html, fetched.status_code,
                                       fetched.url)
        except FetchError as exc:
            errors.append(f"{host}: {exc}")
            continue

        if status == 404:
            errors.append(f"{host}: no snapshot exists yet")
            continue
        if status >= 400:
            errors.append(f"{host}: HTTP {status}")
            continue
        if is_challenge_page(html, fetched.headers):
            errors.append(f"{host}: blocked by a bot check")
            continue
        if "/newest/" in final_url or "/submit" in final_url:
            errors.append(f"{host}: no snapshot exists yet")
            continue
        return html, status, final_url

    raise FetchError("; ".join(errors) or "no archive.today host responded")


def is_challenge_page(html: str,
                      headers: dict[str, str] | None = None) -> bool:
    """Is this a bot check rather than the page we asked for?

    Headers and markup only. Phrase matching was tried and dropped: across
    every page tested it produced no true positives and two failures -- it
    missed a real Cloudflare interstitial titled "Please hold a moment", and
    it flagged this app's own bookmarklet page, whose copy happens to quote
    the words a bot check puts on screen. Any page that merely writes about
    bot checks would trip it.

    Nothing is lost by dropping it. A challenge page has almost no text, so
    the word-count floor still catches one from a vendor with no signature
    here; it is only reported as thin rather than as a bot check.
    """
    if headers:
        lowered = {str(k).lower(): str(v).lower() for k, v in headers.items()}
        # Cloudflare states it outright when it intervenes.
        if "challenge" in lowered.get("cf-mitigated", ""):
            return True
        if lowered.get("x-datadome", "") == "protected":
            return True

    # Vendor infrastructure: script endpoints, JS globals and cookie names.
    # These are machine-facing identifiers, so they survive rewording.
    haystack = html.lower()
    if any(marker in haystack for marker in CHALLENGE_MARKUP):
        return True

    return False


def dump_metadata(result: Extracted) -> str:
    return json.dumps(result.metadata(), indent=2, ensure_ascii=False)
