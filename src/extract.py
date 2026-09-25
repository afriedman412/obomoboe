"""Fetching a URL and turning it into archivable content.

Nothing here touches the database or Flask -- it is all pure-ish functions so
it can be exercised from tests without a server.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
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


def fetch(url: str, user_agent: str, timeout: int = 25,
          session: requests.Session | None = None) -> tuple[str, int, str]:
    """Return (html, status_code, final_url)."""
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
    return resp.text, resp.status_code, resp.url


def _meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = (soup.find("meta", attrs={"property": name})
               or soup.find("meta", attrs={"name": name}))
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def extract(html: str, url: str, status_code: int = 200,
            source: str = "direct") -> Extracted:
    """Run readability extraction and pull metadata out of a page."""
    result = Extracted(url=url, html=html, status_code=status_code,
                       source=source)

    readable = trafilatura.extract(
        html, url=url, output_format="html", include_links=True,
        include_images=True, include_formatting=True, include_tables=True,
        favor_recall=True,
    ) or ""
    result.readable_html = sanitize_fragment(readable)
    result.text = trafilatura.extract(html, url=url, favor_recall=True) or ""

    soup = BeautifulSoup(html, "lxml")

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


def looks_paywalled(result: Extracted, min_words: int = 200) -> bool:
    """Heuristic: did we get a stub instead of the article?"""
    if result.status_code in (401, 402, 403, 451):
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
            html, status, final_url = fetch(snapshot_url, user_agent, timeout,
                                            session)
        except FetchError as exc:
            errors.append(f"{host}: {exc}")
            continue

        if status == 404:
            errors.append(f"{host}: no snapshot exists yet")
            continue
        if status >= 400:
            errors.append(f"{host}: HTTP {status}")
            continue
        if _is_challenge_page(html):
            errors.append(f"{host}: blocked by a bot check")
            continue
        if "/newest/" in final_url or "/submit" in final_url:
            errors.append(f"{host}: no snapshot exists yet")
            continue
        return html, status, final_url

    raise FetchError("; ".join(errors) or "no archive.today host responded")


def _is_challenge_page(html: str) -> bool:
    lowered = html[:4000].lower()
    return any(
        marker in lowered
        for marker in ("just a moment", "checking your browser",
                       "cf-browser-verification", "enable javascript and cookies",
                       "captcha-delivery")
    )


def dump_metadata(result: Extracted) -> str:
    return json.dumps(result.metadata(), indent=2, ensure_ascii=False)
