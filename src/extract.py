"""Fetching a URL and turning it into archivable content.

Nothing here touches the database or Flask -- it is all pure-ish functions so
it can be exercised from tests without a server.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from urllib.parse import (parse_qsl, quote, urlencode, urljoin, urlparse,
                          urlunparse)

import requests
import trafilatura
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup, NavigableString, Tag

# Query params that are pure tracking noise; dropped when de-duplicating URLs.
TRACKING_PARAMS = re.compile(
    r"^(utm_|ic[id]$|mc_[ce]id$|fbclid$|gclid$|igshid$|mkt_tok$|ref$|"
    r"ref_src$|s_campaign$|cmpid$|smid$|partner$|__twitter_impression$)",
    re.IGNORECASE,
)

HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

# archive.today answers on all of these; a snapshot link can use any of them.
ARCHIVE_TODAY_HOSTS = {
    "archive.ph", "archive.today", "archive.is", "archive.li", "archive.vn",
    "archive.md", "archive.fo",
}

# A snapshot's canonical link: https://archive.ph/2022.11.23-130517/<original>
SNAPSHOT_CANONICAL = re.compile(
    r"^https?://(?:www\.)?(?P<host>[a-z.]+)/"
    r"(?P<stamp>\d{4}\.\d{2}\.\d{2}-\d{6})/"
    r"(?P<original>https?://.+)$", re.IGNORECASE)

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
    # Set when the page is an archive.today snapshot: the publisher's own
    # URL, and the snapshot's short link. `url` stays the address the page
    # was read from, since the snapshot's images resolve against it.
    original_url: str | None = None
    archive_url: str | None = None

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def metadata(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "original_url": self.original_url,
            "archive_url": self.archive_url,
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


def is_archive_today(url: str | None) -> bool:
    return bool(url) and domain_of(str(url)) in ARCHIVE_TODAY_HOSTS


def snapshot_urls(html: str, page_url: str = ""
                  ) -> tuple[str, str] | None:
    """(original URL, snapshot link) if this page is an archive.today snapshot.

    Saving from a snapshot used to file the article under archive.ph, so the
    list said "archive.ph" and the link back went to the snapshot. The page
    states what it is a copy of in two places: the canonical link,
    archive.ph/<timestamp>/<original>, and the search box in its header. The
    original is the one followed through redirects, so a snapshot of a t.co
    link still yields the article.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    original: str | None = None

    canonical = soup.find("link", rel="canonical")
    match = SNAPSHOT_CANONICAL.match(str(canonical.get("href") or "")) \
        if canonical else None
    if match and match.group("host").lower() in ARCHIVE_TODAY_HOSTS:
        original = match.group("original")
    elif is_archive_today(page_url):
        box = soup.find("input", attrs={"name": "q"})
        if box is not None and box.get("value"):
            original = str(box["value"])
    if not original:
        return None

    try:
        original = normalize_url(original)
    except ValueError:
        return None
    if is_archive_today(original):
        return None

    short = _meta_content(soup, "og:url")
    if is_archive_today(short):
        snapshot = str(short)
    elif is_archive_today(page_url):
        snapshot = page_url
    else:
        snapshot = str(canonical.get("href")) if canonical else page_url
    return original, snapshot


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

    soup = BeautifulSoup(html, "lxml")
    # Menus, recirculation and the like are cut out before either extractor
    # sees the page; see strip_chrome. Metadata is still read from the
    # untouched document.
    cleaned = strip_chrome(BeautifulSoup(html, "lxml"))
    cleaned_html = str(cleaned)

    readable = trafilatura.extract(
        cleaned_html, url=url, output_format="html", include_links=True,
        include_images=True, include_formatting=True, include_tables=True,
        favor_recall=True,
    ) or ""
    result.text = trafilatura.extract(cleaned_html, url=url,
                                      favor_recall=True) or ""

    # A single-container reading can miss most of a block-built article.
    blocks = collect_blocks(cleaned)
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

    snapshot = snapshot_urls(html, url)
    if snapshot is not None:
        result.original_url, result.archive_url = snapshot
        # archive.today names itself as the site; that is the shelf, not
        # the publisher.
        if result.site_name and (is_archive_today("https://" + result.site_name)
                                 or result.site_name.lower() in
                                 ARCHIVE_TODAY_HOSTS):
            result.site_name = None

    if not result.title:
        og_title = _meta_content(soup, "og:title", "twitter:title")
        if og_title:
            result.title = og_title
        elif soup.title and soup.title.string:
            result.title = soup.title.string.strip()
    if not result.author:
        result.author = named_author(soup)
    if not result.excerpt:
        result.excerpt = _meta_content(soup, "og:description", "description")
    if not result.site_name:
        site = _meta_content(soup, "og:site_name")
        if snapshot is not None or not site:
            site = domain_of(result.original_url or url)
        result.site_name = site
    exact = precise_published_at(soup)
    if exact and (not result.published_at
                  or exact[:10] == str(result.published_at)[:10]):
        # Same day, more detail -- take it. A different day means the two
        # disagree about the facts, and readability checks more signals.
        result.published_at = exact
    elif not result.published_at:
        result.published_at = _meta_content(
            soup, "article:published_time", "datePublished"
        )

    if snapshot is not None:
        # archive.today rewrites every title and date tag on the page: the
        # titles are cut at 70 characters and both published and modified
        # times are set to the moment of capture. Read them off the
        # archived page instead.
        title, published = snapshot_title_and_date(
            html, result.title, result.original_url or url)
        result.title = title or result.title
        result.published_at = published

    if not result.text and result.readable_html:
        result.text = BeautifulSoup(result.readable_html, "lxml").get_text(" ")

    result.readable_html = drop_leading_title(result.readable_html,
                                              result.title)
    result.text = dedupe_head(result.text)

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

# Naming used for page furniture. Matched as whole tokens of an id, class
# or data-testid (split on punctuation and camelCase), never as substrings:
# "nav" must not hit "canvas", nor "ad" hit "address".
CHROME_TOKENS = {
    "nav", "navbar", "navigation", "menu", "submenu", "hamburger", "masthead",
    "header", "footer", "sidebar", "rail", "rightrail", "leftrail",
    "recirc", "recirculation", "related", "trending", "popular", "morein",
    "readmore", "readnext", "promo", "promotion", "outbrain", "taboola",
    "newsletter", "subscribe", "signup",
    "social", "share", "sharing", "sharebar", "comment", "comments",
    "ad", "ads", "adslot", "advert", "advertisement", "advertising",
    "cookie", "cookies", "consent", "banner", "breadcrumb", "breadcrumbs",
    "modal", "popup", "overlay", "toast", "skip",
}
# Prefix matches, for compound tokens like "recirc-collection" the token split
# leaves intact ("recirccollection" never occurs; "recirc" does) and hashed
# names that keep a readable stem ("newsletterSignup").
CHROME_PREFIXES = ("recirc", "newsletter", "advert", "sharebar", "breadcrumb")

# Blocks whose whole text is one of these are labels on furniture, not prose.
CHROME_LABELS = {
    "advertisement", "advertisements", "supported by", "sponsored",
    "sponsored content", "paid post", "paid content", "skip to content",
    "skip to main content", "continue reading the main story",
    "read more", "share this article", "share full article",
}
LABEL_BLOCKS = {"p", "div", "span", "a", "li", "section", "h1", "h2", "h3",
                "h4", "h5", "h6", "small", "strong", "em"}

CHROME_TAGS = {"nav", "footer", "aside", "dialog", "form", "menu", "template"}
CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary",
                "dialog", "alertdialog", "menu", "menubar", "search"}

# Kept for the block collector's per-element check.
BOILERPLATE_HINTS = CHROME_TOKENS

BLOCK_TAGS = ("p", "blockquote", "h2", "h3", "h4", "li", "pre")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^a-z0-9]+")


def _name_tokens(tag: Any) -> set[str]:
    """Words in an element's id, class and data-testid, lowercased."""
    parts: list[str] = []
    for attr in ("id", "class", "data-testid", "data-test", "data-module"):
        value = tag.get(attr)
        if not value:
            continue
        parts.extend(value if isinstance(value, list) else [str(value)])
    joined = _CAMEL.sub(" ", " ".join(parts)).lower()
    return {token for token in _SPLIT.split(joined) if token}


# Names that negate the hint next to them: Variety's body copy sits in
# <div class="pmc-not-a-paywall">, and its whole article went with it.
NEGATIONS = {"not", "no", "non", "without"}

# Structural words that mean something else inside the piece. Variety's
# film titles sit in "c-gallery-vertical-featured-image__header"; Substack
# headings are <h2 class="header-anchor-post">. Outside the article these
# still name the masthead and the site footer.
STRUCTURAL_TOKENS = {"header", "footer"}

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _named_as_chrome(tag: Any, in_article: bool = False) -> bool:
    if tag.name in HEADINGS:
        return False
    tokens = _name_tokens(tag)
    if tokens & NEGATIONS:
        return False
    hits = tokens & CHROME_TOKENS
    if in_article:
        hits -= STRUCTURAL_TOKENS
    if hits:
        return True
    return any(token.startswith(CHROME_PREFIXES) for token in tokens)


def _holds_article(tag: Any) -> bool:
    """Is this the article container, or a wrapper around it?

    Naming is too weak a signal to throw away the piece itself: a Substack
    post is <article class="newsletter-post">, and Variety wraps its body
    in the class above. Landmarks and roles still apply to these.
    """
    name = getattr(tag, "name", "") or ""
    if name in ("article", "main", "body"):
        return True
    if str(tag.get("role") or "").lower() in ("main", "article"):
        return True
    return tag.find(["article", "main"]) is not None


def _is_chrome_element(tag: Any, in_article: bool = False) -> bool:
    """Is this element itself page furniture?

    A header outside the article is the masthead; inside it is the headline
    and byline, which are left alone.
    """
    name = getattr(tag, "name", "") or ""
    if not name or not hasattr(tag, "get"):
        return False
    if name in CHROME_TAGS:
        return True
    if name == "header" and not in_article:
        return True
    role = str(tag.get("role") or "").lower()
    if role in CHROME_ROLES:
        return True
    if tag.has_attr("hidden") or str(tag.get("aria-hidden")).lower() == "true":
        return True
    return not _holds_article(tag) and _named_as_chrome(tag, in_article)


def _word_count(tag: Any) -> int:
    return len(tag.get_text(" ", strip=True).split())


def strip_chrome(soup: BeautifulSoup) -> BeautifulSoup:
    """Cut page furniture out of a document before extraction.

    Readability heuristics work on text density and are easily fooled by a
    site menu full of blurbs. nytimes.com renders its entire mobile
    navigation -- 900 words of newsletter and podcast descriptions -- inside
    a <dialog> eleven levels deep, followed by "Explore the magazine" and
    "Trending" lists in sections marked role=complementary and
    data-testid=recirculation. All of it came through as the article.

    Landmark tags, ARIA roles, hidden state and naming carry the decision.
    Nothing holding most of the page's words is removed, whatever it is
    called: a wrapper with "gateway" or "comments" in its id that happens to
    contain the article must survive.
    """
    body = soup.body or soup
    total = _word_count(body)
    ceiling = max(total // 2, 1)
    for tag in list(body.find_all(True)):
        if tag.decomposed or not tag.parent:
            continue
        in_article = any(getattr(parent, "name", "") in ("article", "main")
                         for parent in tag.parents)
        if not _is_chrome_element(tag, in_article):
            continue
        if _word_count(tag) > ceiling:
            continue
        tag.decompose()
    _strip_labels(body)
    return soup


def _strip_labels(body: Any) -> None:
    """Remove blocks that say nothing but "Advertisement" and the like.

    Works up from the matching text node to the outermost block that still
    reads as just the label, so the wrapper goes too and no empty <p> is
    left behind.
    """
    for node in list(body.find_all(string=True)):
        text = " ".join(str(node).split()).lower()
        if text not in CHROME_LABELS or not node.parent:
            continue
        doomed = None
        for element in node.parents:
            name = getattr(element, "name", "") or ""
            if name not in LABEL_BLOCKS:
                break
            if " ".join(element.get_text(" ", strip=True).split()).lower() != text:
                break
            doomed = element
        (doomed if doomed is not None else node).extract()


def drop_leading_title(fragment: str, title: str | None) -> str:
    """Take the headline out of the reader view when it opens with one.

    The article page already prints the title above the text; a heading
    that repeats it is noise. Only a heading among the first few elements
    is considered, and only when it says the same thing as the title.
    """
    if not fragment or not title:
        return fragment
    wanted = " ".join(title.split()).lower()
    soup = BeautifulSoup(fragment, "lxml")
    body = soup.body or soup
    dropped = False
    # More than one can match: nytimes.com renders a desktop and a mobile
    # header, each with the headline.
    for element in list(body.find_all(True))[:8]:
        if element.name not in HEADINGS:
            continue
        heading = " ".join(element.get_text(" ", strip=True).split()).lower()
        if heading == wanted or (len(heading) > 20 and heading in wanted) \
                or (len(wanted) > 20 and wanted in heading):
            element.decompose()
            dropped = True
    return body.decode_contents() if dropped else fragment


def dedupe_head(text: str, lines: int = 10) -> str:
    """Drop a line repeated near the top of the text, case-insensitively.

    Two headers on one page means the kicker and the headline each appear
    twice in the plain text, which then counts double and searches double.
    """
    head, tail = text.split("\n")[:lines], text.split("\n")[lines:]
    seen: set[str] = set()
    kept: list[str] = []
    for line in head:
        key = " ".join(line.split()).lower()
        if key and key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return "\n".join(kept + tail)


def _is_chrome(tag: Any) -> bool:
    """Is this element page furniture, or inside some?"""
    in_article = False
    for element in [tag, *tag.parents]:
        name = getattr(element, "name", "") or ""
        if name in ("article", "main"):
            in_article = True
        if _is_chrome_element(element, in_article):
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


def _ld_json_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, including @graph members."""
    found: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        queue = data if isinstance(data, list) else [data]
        while queue:
            item = queue.pop(0)
            if not isinstance(item, dict):
                continue
            found.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                queue.extend(graph)
    return found


def precise_published_at(soup: BeautifulSoup) -> str | None:
    """The publisher's own timestamp, seconds and offset intact.

    Readability metadata reports a date and drops the time, so "posted at
    22:49" became just the day. The page usually states it exactly; prefer
    that when it is there and parseable.
    """
    candidates = [_meta_content(soup, "article:published_time", "datePublished",
                                "article:published", "pubdate")]
    for block in _ld_json_blocks(soup):
        value = block.get("datePublished") or block.get("dateCreated")
        if isinstance(value, str):
            candidates.append(value)
    for tag in soup.find_all("time"):
        if tag.get("datetime"):
            candidates.append(str(tag["datetime"]))

    for raw in candidates:
        if not raw:
            continue
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        # A bare date is no better than what we already have.
        if (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0) \
                and "T" not in text:
            continue
        return parsed.isoformat(timespec="seconds")
    return None


def named_author(soup: BeautifulSoup) -> str | None:
    """An author's name, from JSON-LD or meta tags.

    Pages often put a profile URL in the author slot -- one had
    "https://www.patreon.com/CultureStudy" there. A link is not a byline, so
    anything URL-shaped is refused rather than printed under a headline.
    """
    candidates: list[Any] = []
    for block in _ld_json_blocks(soup):
        value = block.get("author") or block.get("creator")
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, dict):
                candidates.append(item.get("name"))
            elif isinstance(item, str):
                candidates.append(item)
    candidates.append(_meta_content(soup, "author", "article:author",
                                    "parsely-author", "sailthru.author"))

    for raw in candidates:
        if not isinstance(raw, str):
            continue
        name = " ".join(raw.split())
        if not name or len(name) > 120:
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*://", name, re.IGNORECASE):
            continue
        if name.startswith("@") or "/" in name:
            continue
        return name
    return None


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
# Title and date of an archive.today snapshot
# --------------------------------------------------------------------------

_MONTHS = {name: number for number, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"),
     ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
     ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
     ("nov", "november"), ("dec", "december")), start=1) for name in names}
_MONTH = (r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
          r"june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|"
          r"nov(?:ember)?|dec(?:ember)?)\.?")
_CLOCK = (r"(?:,?\s*(?:at\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
          r"(?P<ampm>[ap]\.?\s?m\.?)?\s*(?P<tz>[ECMP][SD]?T|UTC|GMT)?)?")
_YEAR = r"(?P<year>(?:19|20)\d{2})"
DATELINES = (
    # November 23, 2022 at 5:00 a.m. EST / Sept. 5, 2026, 3:58 p.m. ET
    re.compile(rf"\b{_MONTH}\s+(?P<day>\d{{1,2}}),?\s+{_YEAR}\b{_CLOCK}",
               re.IGNORECASE),
    # 5 September 2026
    re.compile(rf"\b(?P<day>\d{{1,2}})\s+{_MONTH}\s+{_YEAR}\b{_CLOCK}",
               re.IGNORECASE),
)
# 12.05.2022 -- read both ways; the URL usually settles which is meant.
NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./]((?:19|20)\d{2})\b")
URL_DATE = re.compile(r"/((?:19|20)\d{2})/(\d{1,2})(?:/(\d{1,2}))?/")
_ZONES = {
    "et": "America/New_York", "ct": "America/Chicago",
    "mt": "America/Denver", "pt": "America/Los_Angeles",
}
_OFFSETS = {"est": -5, "edt": -4, "cst": -6, "cdt": -5, "mst": -7,
            "mdt": -6, "pst": -8, "pdt": -7, "utc": 0, "gmt": 0}
BLOCKED_PLACEHOLDER = re.compile(r"^\S+\.\S+ is blocked$")
TITLE_SEPARATORS = (" | ", " - ", " — ", " – ")


def snapshot_taken(html: str) -> datetime | None:
    """When archive.today captured the page, from its canonical link."""
    soup = BeautifulSoup(html, "lxml")
    canonical = soup.find("link", rel="canonical")
    match = SNAPSHOT_CANONICAL.match(str(canonical.get("href") or "")) \
        if canonical else None
    if not match:
        return None
    return datetime.strptime(match.group("stamp"), "%Y.%m.%d-%H%M%S") \
        .replace(tzinfo=timezone.utc)


def _archived_page(html: str) -> Tag:
    """The archived page itself, without archive.today's own header."""
    soup = BeautifulSoup(html, "lxml")
    header = soup.find(id="HEADER")
    if header is not None:
        header.decompose()
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    return soup.find(id="CONTENT") or soup.body or soup


def _clean(text: str) -> str:
    return " ".join(text.split())


def strip_site_suffix(title: str, url: str) -> str:
    """'War Bros ... | WIRED' -> 'War Bros ...', when the tail is the site.

    The tail counts as the site when it spells the domain: WIRED is
    wired.com, The Washington Post is washingtonpost.com, and The New York
    Times is nytimes.com by its initials.
    """
    label = domain_of(url).split(".")[0]
    for separator in TITLE_SEPARATORS:
        head, found, tail = title.rpartition(separator)
        if not found or not head.strip() or len(tail.split()) > 6:
            continue
        words = [w for w in re.split(r"[^a-z0-9]+", tail.lower()) if w]
        if words and words[0] == "the":
            words = words[1:]
        if not words:
            continue
        spellings = {"".join(words),
                     "".join(w[0] for w in words[:-1]) + words[-1]}
        if label in spellings:
            return head.strip()
    return title


def _complete_title(title: str | None, page: Tag, full_title: str | None
                    ) -> str | None:
    """Finish a title archive.today cut short with an ellipsis.

    The page's own <title> survives whole, as does the headline; whichever
    begins with the shortened text is the rest of it.
    """
    if not title or not title.rstrip().endswith(("…", "...")):
        return title
    prefix = _clean(title.rstrip().rstrip(".…")).lower()
    candidates = [full_title or ""] + [
        _clean(h.get_text(" ", strip=True)) for h in page.find_all("h1")]
    for candidate in candidates:
        if BLOCKED_PLACEHOLDER.match(candidate):
            continue
        if candidate.lower().startswith(prefix) and len(candidate) > len(prefix):
            return candidate
    return title


def _parse_dateline(match: re.Match[str]) -> datetime | date | None:
    try:
        day = date(int(match.group("year")),
                   _MONTHS[match.group("month").lower().rstrip(".")],
                   int(match.group("day")))
    except (KeyError, ValueError):
        return None
    if not match.group("hour"):
        return day
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    ampm = (match.group("ampm") or "").lower().replace(".", "").strip()
    if ampm.startswith("p") and hour < 12:
        hour += 12
    if ampm.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return day
    moment = datetime(day.year, day.month, day.day, hour, minute)
    zone = (match.group("tz") or "").lower()
    if zone in _ZONES:
        return moment.replace(tzinfo=ZoneInfo(_ZONES[zone]))
    if zone in _OFFSETS:
        return moment.replace(tzinfo=timezone(timedelta(hours=_OFFSETS[zone])))
    return moment


def _dates_in(text: str) -> list[datetime | date]:
    found: list[tuple[int, datetime | date]] = []
    for pattern in DATELINES:
        for match in pattern.finditer(text):
            parsed = _parse_dateline(match)
            if parsed is not None:
                found.append((match.start(), parsed))
    for match in NUMERIC_DATE.finditer(text):
        first, second, year = (int(g) for g in match.groups())
        for month, day in ((first, second), (second, first)):
            try:
                found.append((match.start(), date(year, month, day)))
            except ValueError:
                continue
    return [value for _, value in sorted(found, key=lambda pair: pair[0])]


def _page_dates(page: Tag) -> list[datetime | date]:
    """Every date the archived page states, in reading order."""
    dates: list[datetime | date] = []
    for node in page.descendants:
        if isinstance(node, Tag) and node.name == "time":
            stamp = str(node.get("datetime") or "").strip()
            try:
                dates.append(datetime.fromisoformat(stamp.replace("Z",
                                                                  "+00:00")))
                continue
            except ValueError:
                pass
            dates.extend(_dates_in(node.get_text(" ", strip=True)))
        elif isinstance(node, NavigableString) and node.find_parent("time") \
                is None:
            dates.extend(_dates_in(str(node)))
    return dates


def _day(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def impute_published(page: Tag, url: str, taken: datetime | None
                     ) -> str | None:
    """The publication date stated on an archived page.

    The first date on the page that could be the publication wins: not
    after the capture, and, when the URL carries a date as news URLs do,
    within a few days of it. That passes over embedded posts quoted in the
    piece and the "today" lists in a sidebar. Failing all that, the URL's
    own date is better than nothing; the capture time is not used, since a
    page saved years later would claim to be new.
    """
    url_match = URL_DATE.search(urlparse(url).path + "/")
    url_day: date | None = None
    url_month: tuple[int, int] | None = None
    if url_match:
        year, month, day = url_match.groups()
        try:
            url_month = (int(year), int(month))
            url_day = date(int(year), int(month), int(day)) if day else None
        except ValueError:
            url_month = url_day = None
    latest = _day(taken) + timedelta(days=1) if taken else None

    dates = _page_dates(page)
    for index, value in enumerate(dates):
        day = _day(value)
        if latest and day > latest:
            continue
        if url_day and not (url_day - timedelta(days=2) <= day
                            <= url_day + timedelta(days=3)):
            continue
        if url_month and not url_day and (day.year, day.month) != url_month:
            continue
        if not isinstance(value, datetime):
            # A dateline often says the day and, a line later, the minute
            # ("Sept. 5, 2026" over a timestamped <time>). Take the minute.
            value = next((later for later in dates[index + 1:index + 4]
                          if isinstance(later, datetime)
                          and later.date() == day), value)
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return value.isoformat()
    return url_day.isoformat() if url_day else None


def snapshot_title_and_date(html: str, title: str | None, url: str
                            ) -> tuple[str | None, str | None]:
    """The title and publication date of an archive.today snapshot."""
    soup = BeautifulSoup(html, "lxml")
    full_title = _clean(soup.title.get_text(" ", strip=True)) \
        if soup.title else None
    page = _archived_page(html)
    title = _complete_title(title or full_title, page, full_title)
    if title:
        title = strip_site_suffix(title, url)
    return title, impute_published(page, url, snapshot_taken(html))


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
