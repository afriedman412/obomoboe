"""Working out what an article is about.

Three sources, in descending order of trust:

1. Tags the publisher declared (``article:tag``, JSON-LD ``keywords``). Only
   about a quarter of pages carry them, but where they exist they are the
   author's own filing.
2. Terms the text itself leans on, by frequency against a stoplist. Always
   available, never clever -- it finds "causal inference", not what the piece
   argues about it.
3. Claude, when an API key is configured, to refine the above into something
   closer to how a person would file it. Entirely optional: with no key the
   first two carry on working, offline and free, which is the point.

Nothing here applies a tag. Suggestions are offered and accepted by hand --
see the note in README about keeping your own taxonomy yours.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from typing import Any, Iterable

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Small and deliberately unclever: the frequency pass only needs to stop
# reporting that articles are about "the" and "would".
STOPWORDS = frozenset("""
a about above after again against all almost also although always am among an
and another any anyone anything are around as at back be became because become
becomes been before began being below best better between both but by came can
cannot come could did do does doing done down during each either else enough
even ever every everyone everything few find first for found from further get
gets getting give given go goes going good got had has have having he her here
hers herself him himself his how however i if in indeed instead into is it its
itself just keep kept know known last later least left less let like likely
little long look looking made make makes making many may maybe me mean means
might more most much must my myself near need never new next no nor not nothing
now of off often on once one only onto or other others our ours out over own
part people per perhaps put rather really right said same saw say says see seem
seems seen several shall she should since so some someone something soon still
such take taken than that the their theirs them themselves then there these
they thing things think this those though thought three through thus time to
together too took toward two under until up upon us use used using very want
was way we well went were what when where whether which while who whole whom
whose why will with within without would yet you your yours
""".split())

# Words that describe the shape of a page rather than its subject.
CHROME_WORDS = frozenset("""
article author blog comment comments content cookie cookies copyright edit
email follow home image index javascript link links login menu navigation news
newsletter page pages photo post posts privacy published read reading reply
search share sign signup subscribe tag tags terms twitter update updated
website
accessed archived cited doi eds isbn issn journal originally pmid publisher
retrieved vol wikipedia
""".split())

WORD = re.compile(r"[a-z][a-z-]*(?:['’][a-z]{1,2})?")
# Possessives and contractions: "it's" and "barrio's" are not subjects.
CLITIC = re.compile(r"['’][a-z]{1,2}$")
SENTENCE = re.compile(r"[.!?;:\n]+")

MAX_TAG_WORDS = 3
MAX_TAG_CHARS = 32


def _clean(name: str) -> str | None:
    """Normalize a candidate, or reject it."""
    tag = " ".join(str(name).replace("_", " ").split()).strip(" -–—,·|/").lower()
    if not tag or len(tag) > MAX_TAG_CHARS or len(tag.split()) > MAX_TAG_WORDS:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*://", tag) or "/" in tag or "@" in tag:
        return None
    if not any(ch.isalpha() for ch in tag):
        return None
    if CLITIC.search(tag) or "'" in tag or "’" in tag:
        return None
    if tag in STOPWORDS or tag in CHROME_WORDS:
        return None
    return tag


def _dedupe(names: Iterable[str | None]) -> list[str]:
    """Keep order, drop repeats and near-repeats (plural of one already in)."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        tag = _clean(name) if name else None
        if not tag:
            continue
        key = tag.rstrip("s")
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


# --------------------------------------------------------------------------
# 1. What the publisher said
# --------------------------------------------------------------------------


def publisher_tags(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    for tag in soup.find_all("meta", property="article:tag"):
        if tag.get("content"):
            found.append(str(tag["content"]))

    keywords = soup.find("meta", attrs={"name": "keywords"})
    if keywords and keywords.get("content"):
        found.extend(str(keywords["content"]).split(","))

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
            if isinstance(item.get("@graph"), list):
                queue.extend(item["@graph"])
            value = item.get("keywords") or item.get("about")
            for entry in (value if isinstance(value, list) else [value]):
                if isinstance(entry, dict):
                    found.append(str(entry.get("name") or ""))
                elif isinstance(entry, str):
                    found.extend(entry.split(","))
    return _dedupe(found)


# --------------------------------------------------------------------------
# 2. What the text keeps coming back to
# --------------------------------------------------------------------------


def keyword_tags(text: str, title: str | None = None,
                 limit: int = 6) -> list[str]:
    """Frequency against a stoplist, with phrases preferred over bare words."""
    body = (text or "").lower()
    if not body.strip():
        return []

    sentences = [[w for w in (CLITIC.sub("", m) for m in WORD.findall(part))
                  if len(w) > 2]
                 for part in SENTENCE.split(body)]
    words = [w for sentence in sentences for w in sentence]
    if len(words) < 40:
        return []

    singles = Counter(w for w in words
                      if w not in STOPWORDS and w not in CHROME_WORDS)

    # Two-word phrases read far better as tags than their parts do.
    pairs: Counter[str] = Counter()
    for sentence in sentences:
        for first, second in zip(sentence, sentence[1:]):
            if first in STOPWORDS or second in STOPWORDS:
                continue
            if first in CHROME_WORDS or second in CHROME_WORDS:
                continue
            pairs[f"{first} {second}"] += 1

    title_words = {CLITIC.sub("", w)
                   for w in WORD.findall((title or "").lower())}

    def score(term: str, count: int) -> float:
        weight = count * (2.2 if " " in term else 1.0)
        if any(part in title_words for part in term.split()):
            weight *= 1.8
        return weight

    # A repeated phrase is already good evidence of a subject; a bare word
    # that merely recurs is usually just a common noun, so ask more of it.
    scored = [(t, score(t, c)) for t, c in pairs.items() if c >= 2]
    scored += [(t, score(t, c)) for t, c in singles.items()
               if c >= 4 or (t in title_words and c >= 2)]
    ranked = sorted(scored, key=lambda pair: pair[1], reverse=True)

    # A phrase makes its own components redundant.
    chosen: list[str] = []
    covered: set[str] = set()
    for term, _weight in ranked:
        parts = set(term.split())
        if parts & covered:
            continue
        chosen.append(term)
        covered |= parts
        if len(chosen) >= limit * 2:
            break
    return _dedupe(chosen)[:limit]


# --------------------------------------------------------------------------
# 3. Claude, if a key is configured
# --------------------------------------------------------------------------


PROMPT = """Here is an article. Suggest tags for filing it in a personal \
reading library.

Title: {title}
{candidates}
Article:
{body}

Give {count} tags at most. Prefer the subject the piece is actually about \
over words it happens to repeat. Lowercase, one to three words each. Reuse a \
candidate above when it is already right. Reply with only a JSON array of \
strings."""


def api_key(config: dict[str, Any]) -> str:
    """The settings page wins over the environment -- it is the more recent
    and more deliberate of the two."""
    return (str(config.get("ANTHROPIC_API_KEY") or "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip())


def sdk_installed() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def llm_available(config: dict[str, Any]) -> bool:
    if not config.get("LLM_TAGS_ENABLED", True):
        return False
    if not api_key(config) and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return False
    return sdk_installed()


def llm_tags(config: dict[str, Any], text: str, title: str | None,
             candidates: list[str], limit: int = 6) -> list[str]:
    """Ask Claude to refine the candidates. Returns [] on any failure.

    Deliberately non-fatal: tagging is a convenience, and an article must
    still archive when the network, the key or the API is having a bad day.
    """
    if not llm_available(config):
        return []

    body = (text or "").strip()
    if not body:
        return []
    # Enough to judge the subject; the whole piece would cost more to no end.
    body = body[:config.get("LLM_TAG_CHARS", 6000)]

    prompt = PROMPT.format(
        title=title or "(untitled)",
        candidates=(f"Candidate tags: {', '.join(candidates)}\n"
                    if candidates else ""),
        body=body,
        count=limit,
    )
    try:
        import anthropic

        key = api_key(config)
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        response = client.messages.create(
            model=config.get("TAG_MODEL", "claude-opus-5"),
            max_tokens=256,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # any API or network failure is survivable
        log.warning("tag suggestion via Claude failed: %s", exc)
        return []

    reply = "".join(block.text for block in response.content
                    if block.type == "text").strip()
    match = re.search(r"\[.*\]", reply, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return _dedupe(str(item) for item in parsed)[:limit]


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------


def suggest(config: dict[str, Any], html: str, text: str,
            title: str | None = None) -> list[str]:
    """Candidate tags for an article, best source first."""
    limit = int(config.get("TAG_SUGGESTIONS", 6))
    if limit <= 0:
        return []

    declared: list[str] = []
    if html:
        try:
            declared = publisher_tags(BeautifulSoup(html, "lxml"))
        except Exception:  # a broken page must not stop the archive
            log.debug("could not read publisher tags", exc_info=True)

    local = keyword_tags(text, title, limit=limit)
    candidates = _dedupe(declared + local)

    refined = llm_tags(config, text, title, candidates, limit=limit)
    if refined:
        # Keep anything the publisher declared that Claude also liked, then
        # fill from Claude's own reading.
        return _dedupe(refined + declared)[:limit]
    return candidates[:limit]
