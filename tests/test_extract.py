import pytest

from bs4 import BeautifulSoup

from src.extract import (Extracted, drop_leading_title, extract,
                         is_challenge_page, looks_paywalled, normalize_url,
                         sanitize_fragment, snapshot_urls, strip_chrome,
                         strip_site_suffix)

ARTICLE_HTML = """
<html><head>
  <title>Site name | The Headline</title>
  <meta property="og:title" content="The Headline">
  <meta property="og:site_name" content="Example Review">
  <meta property="og:description" content="A short summary.">
  <meta property="article:published_time" content="2026-02-03T10:00:00Z">
</head><body>
  <nav>menu junk</nav>
  <article>
    <h1>The Headline</h1>
    %s
  </article>
  <footer>copyright junk</footer>
</body></html>
""" % ("".join(f"<p>Paragraph {i} with enough words to look like real prose "
               f"rather than boilerplate navigation text.</p>"
               for i in range(30)))


class TestNormalizeUrl:
    def test_adds_scheme(self):
        assert normalize_url("example.com/post") == "https://example.com/post"

    def test_strips_tracking_and_fragment(self):
        url = "https://Example.com/post/?utm_source=x&id=7&fbclid=abc#section"
        assert normalize_url(url) == "https://example.com/post?id=7"

    def test_lowercases_host_only(self):
        url = normalize_url("https://Example.COM/Path/To/Post")
        assert url == "https://example.com/Path/To/Post"

    def test_rejects_junk(self):
        with pytest.raises(ValueError):
            normalize_url("not a url")
        with pytest.raises(ValueError):
            normalize_url("")
        with pytest.raises(ValueError):
            normalize_url("javascript:alert(1)")


class TestSanitize:
    def test_drops_scripts_and_handlers(self):
        dirty = ('<p onclick="steal()">hi</p><script>steal()</script>'
                 '<iframe src="http://evil"></iframe>')
        clean = sanitize_fragment(dirty)
        assert "script" not in clean
        assert "onclick" not in clean
        assert "iframe" not in clean
        assert "hi" in clean

    def test_drops_javascript_hrefs_but_keeps_text(self):
        clean = sanitize_fragment('<a href="javascript:evil()">click</a>')
        assert "javascript:" not in clean
        assert "click" in clean

    def test_keeps_normal_markup(self):
        clean = sanitize_fragment(
            '<p>a <strong>b</strong> <a href="https://x.com">c</a>'
            '<img src="https://x.com/i.png" alt="i"></p>'
        )
        assert "<strong>" in clean
        assert 'href="https://x.com"' in clean
        assert 'src="https://x.com/i.png"' in clean

    def test_unwraps_unknown_tags_keeping_content(self):
        assert "inner" in sanitize_fragment("<custom-el>inner</custom-el>")


class TestExtract:
    def test_pulls_title_and_metadata(self):
        result = extract(ARTICLE_HTML, "https://example.com/post")
        assert result.title == "The Headline"
        assert result.site_name == "Example Review"
        assert result.excerpt == "A short summary."
        assert result.published_at.startswith("2026-02-03")
        assert result.word_count > 200
        assert "Paragraph 1" in result.text
        assert "copyright junk" not in result.text

    def test_readable_html_is_sanitized(self):
        html = ARTICLE_HTML.replace("<nav>menu junk</nav>",
                                    "<script>alert(1)</script>")
        result = extract(html, "https://example.com/post")
        assert "<script" not in result.readable_html


class TestPaywallDetection:
    def test_full_article_is_not_paywalled(self):
        result = extract(ARTICLE_HTML, "https://example.com/post")
        assert not looks_paywalled(result, min_words=200)

    def test_short_stub_is_paywalled(self):
        result = Extracted(url="https://x.com", text="Only a few words here.")
        assert looks_paywalled(result, min_words=200)

    def test_marker_beats_length(self):
        text = " ".join(["word"] * 500) + " Subscribe to continue reading"
        result = Extracted(url="https://x.com", text=text)
        assert looks_paywalled(result, min_words=200)

    def test_http_402_is_paywalled(self):
        result = Extracted(url="https://x.com", text=" ".join(["w"] * 900),
                           status_code=403)
        assert looks_paywalled(result, min_words=200)


class TestChallengePage:
    CHALLENGE = """
    <html><head><title>Just a moment...</title></head>
    <body><h1>Please hold a moment...</h1>
    <p>Enable JavaScript and cookies to continue</p></body></html>
    """

    def test_thin_interstitial_is_not_treated_as_an_article(self):
        """No signature here, but the word-count floor still catches it."""
        result = extract(self.CHALLENGE, "https://example.com/post")
        assert looks_paywalled(result)

    def test_writing_about_bot_checks_is_not_a_bot_check(self):
        """This app's own bookmarklet page quotes the words a challenge puts
        on screen; phrase matching flagged it as one."""
        page = ("<html><body><p>The bookmarklet is the difference between "
                "saving the article and saving the words 'enable JavaScript "
                "and cookies to continue'.</p></body></html>")
        assert not is_challenge_page(page)

    def test_bot_check_caught_even_above_the_word_threshold(self):
        # Length alone must not clear it: a wordy interstitial is still not
        # the article.
        padded = self.CHALLENGE.replace(
            "</body>",
            "<script>window._cf_chl_opt={cvId:'3'};</script>"
            "<p>" + "filler " * 400 + "</p></body>")
        result = extract(padded, "https://example.com/post")
        assert result.word_count > 200
        assert looks_paywalled(result)

    def test_detected_with_no_matching_prose_at_all(self):
        """The real Cloudflare interstitial says "Please hold a moment" and
        matches none of the stock phrases -- markup has to carry it."""
        page = """
        <html><head><title>Please hold a moment&hellip;</title></head>
        <body><p>We are just confirming your site connection is secure.</p>
        <script>window._cf_chl_opt={cvId:"3",cType:"managed"};</script>
        </body></html>
        """
        assert is_challenge_page(page)

    def test_response_headers_alone_are_enough(self):
        innocuous = "<html><body><p>nothing telling here</p></body></html>"
        assert not is_challenge_page(innocuous)
        assert is_challenge_page(innocuous, {"CF-Mitigated": "challenge"})

    def test_ordinary_page_behind_cloudflare_is_not_a_challenge(self):
        """Much of the web is served by Cloudflare; that alone proves nothing."""
        page = "<html><body>" + "<p>real words here</p>" * 50 + "</body></html>"
        assert not is_challenge_page(
            page, {"Server": "cloudflare", "CF-Ray": "a40b98-IAD"})


class TestBlockBuiltArticles:
    """Some CMSs split one article across many sibling containers; a
    single-best-container reading keeps one and drops the rest."""

    PAGE = """
    <html><head><title>Split</title></head><body>
      <nav><p>home about contact us today</p></nav>
      <div class="grid">
        <div class="rich-text-block"><p>%s</p></div>
        <div class="rich-text-block"><p>%s</p></div>
        <div class="rich-text-block"><p>%s</p></div>
      </div>
      <div class="newsletter"><p>Stay updated with our newsletter today
        and confirm your opt-in preferences now please</p></div>
      <footer><p>copyright notice goes here all rights reserved</p></footer>
    </body></html>
    """ % tuple("Block %d " % i + "sentence words carrying real article text "
                * 12 for i in range(3))

    def test_all_blocks_are_recovered(self):
        result = extract(self.PAGE, "https://example.com/post")
        for i in range(3):
            assert f"Block {i}" in result.text, f"block {i} was dropped"

    def test_chrome_is_left_out(self):
        text = extract(self.PAGE, "https://example.com/post").text.lower()
        assert "newsletter" not in text
        assert "rights reserved" not in text
        assert "home about contact" not in text


class TestChallengeFalsePositives:
    def test_invisible_bot_management_is_not_a_challenge(self):
        """Cloudflare injects its detection JS into ordinary pages. Treating
        that as an interstitial flagged a real article as a bot check."""
        page = ("<html><body>" + "<p>genuine article prose here</p>" * 40 +
                '<script src="/cdn-cgi/challenge-platform/h/b/scripts/jsd">'
                '</script><script>__CF$cv$params={r:"abc"};</script>'
                "</body></html>")
        assert not is_challenge_page(page, {"Server": "cloudflare"})


class TestPublishedTimestamp:
    def _page(self, extra: str) -> str:
        return ("<html><head><title>T</title>" + extra + "</head><body>"
                "<article>" + "<p>ordinary article words here</p>" * 30 +
                "</article></body></html>")

    def test_meta_time_is_kept_not_truncated_to_the_day(self):
        page = self._page(
            "<meta property='article:published_time' "
            "content='2026-09-24T22:49:25.000Z'>")
        result = extract(page, "https://example.com/p")
        assert result.published_at.startswith("2026-09-24T22:49:25")

    def test_ld_json_is_read_too(self):
        page = self._page(
            '<script type="application/ld+json">'
            '{"@type":"Article","datePublished":"2026-03-04T08:15:00+00:00"}'
            "</script>")
        result = extract(page, "https://example.com/p")
        assert result.published_at.startswith("2026-03-04T08:15:00")

    def test_graph_wrapped_ld_json_is_read(self):
        page = self._page(
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"WebPage"},'
            '{"@type":"Article","datePublished":"2026-05-06T17:30:00Z"}]}'
            "</script>")
        result = extract(page, "https://example.com/p")
        assert result.published_at.startswith("2026-05-06T17:30:00")

    def test_a_bare_date_is_left_alone(self):
        page = self._page(
            "<meta property='article:published_time' content='2026-09-24'>")
        result = extract(page, "https://example.com/p")
        assert str(result.published_at).startswith("2026-09-24")

    def test_unparseable_timestamp_does_not_break_extraction(self):
        page = self._page(
            "<meta property='article:published_time' content='last Tuesday'>")
        assert extract(page, "https://example.com/p").word_count > 50


class TestAuthorExtraction:
    def _page(self, extra: str) -> str:
        return ("<html><head><title>T</title>" + extra + "</head><body>"
                "<article>" + "<p>ordinary article words here</p>" * 30 +
                "</article></body></html>")

    def test_a_url_is_never_used_as_a_byline(self):
        """One page put a Patreon link in the author slot."""
        page = self._page(
            "<meta name='author' content='https://www.patreon.com/CultureStudy'>")
        assert extract(page, "https://example.com/p").author is None

    def test_ld_json_author_name_is_used(self):
        page = self._page(
            '<script type="application/ld+json">'
            '{"@type":"Article","author":{"@type":"Person","name":"Ada Lovelace"}}'
            "</script>")
        assert extract(page, "https://example.com/p").author == "Ada Lovelace"

    def test_meta_author_name_is_used(self):
        page = self._page("<meta name='author' content='Grace Hopper'>")
        assert extract(page, "https://example.com/p").author == "Grace Hopper"

    def test_handles_are_not_bylines(self):
        page = self._page("<meta name='author' content='@someaccount'>")
        assert extract(page, "https://example.com/p").author is None


def _prose(label: str, n: int = 12) -> str:
    return "".join(f"<p>{label} paragraph {i} carries real article words "
                   f"for the reader to keep and enjoy at length.</p>"
                   for i in range(n))


class TestChromeStripping:
    """Menus, recirculation and the like are cut out before extraction.

    Modelled on nytimes.com, whose mobile navigation -- 900 words of
    newsletter blurbs -- sits in a <dialog> eleven levels deep, well past
    the six ancestors the block collector used to check, followed by
    "Explore" and "Trending" lists in role=complementary and
    data-testid=recirculation sections. All of it was archived as the piece.
    """

    MENU = "".join(
        f"<li><a href='/n{i}'><div>Letter {i}</div><p>Make sense of the "
        f"day's news and ideas with newsletter number {i} today.</p></a></li>"
        for i in range(40))

    PAGE = f"""
    <html><head><title>The Piece</title>
    <meta property="og:title" content="The Piece"></head><body>
    <div><div><div><div data-testid="masthead-container">
      <dialog id="mobile-hamburger"><nav data-testid="mobile-nav">
        <ul><li><div aria-hidden="true" data-testid="hamburger-pane-U.S.">
          <div><div><div><ul>{MENU}</ul></div></div></div>
        </div></li></ul>
      </nav></dialog>
    </div></div></div></div>
    <main id="site-content"><article id="story">
      <header><h1>The Piece</h1><p>A subtitle for the piece here.</p></header>
      <section name="articleBody">
        <div data-testid="StoryAd"><p>Advertisement</p></div>
        {_prose("Body")}
        <aside><ol aria-label="Comments"><li><p>Reader comment saying
          something nixilating about the piece here.</p></li></ol></aside>
      </section>
      <section role="complementary"><h2>Explore The Magazine</h2>
        <ul><li><p>They Kept Outsiders Away for 500 Years and more.</p></li>
        </ul></section>
      <section data-testid="recirculation"><h3>Trending in The Times</h3>
        <ul><li><p>Five Gastrointestinal Symptoms You Should Never Ignore.
        </p></li></ul></section>
      <div id="bottom-slug"><p>Advertisement</p></div>
    </article></main>
    <footer><p>copyright notice goes here all rights reserved</p></footer>
    </body></html>
    """

    def test_menu_deep_in_a_dialog_is_not_the_article(self):
        text = extract(self.PAGE, "https://example.com/p").text
        assert "Body paragraph 3" in text
        assert "Make sense of the day" not in text
        assert "newsletter number" not in text

    def test_recirculation_and_complementary_sections_are_dropped(self):
        text = extract(self.PAGE, "https://example.com/p").text
        assert "Kept Outsiders Away" not in text
        assert "Gastrointestinal" not in text

    def test_comments_and_ad_labels_are_dropped(self):
        text = extract(self.PAGE, "https://example.com/p").text
        assert "nixilating" not in text
        assert "Advertisement" not in text

    def test_headline_is_not_repeated_in_the_reader_view(self):
        result = extract(self.PAGE, "https://example.com/p")
        assert result.title == "The Piece"
        assert "<h1>" not in result.readable_html
        assert "Body paragraph 0" in result.readable_html

    def test_a_named_wrapper_holding_the_article_survives(self):
        """Substack's post is <article class="newsletter-post">; Variety's
        body sits in <div class="pmc-not-a-paywall">. Both vanished."""
        page = (f"<html><body><div class='comments-page'>"
                f"<article class='typography newsletter-post post'>"
                f"<div class='pmc-not-a-paywall'>{_prose('Post')}</div>"
                f"</article></div>"
                f"<div class='comment'><p>a reader comment with several "
                f"words in it here</p></div></body></html>")
        text = extract(page, "https://x.substack.com/p/a").text
        assert "Post paragraph 5" in text
        assert "reader comment" not in text

    def test_headings_and_in_article_headers_are_kept(self):
        """Variety titles each film in a "...__header" div; Substack
        headings carry class "header-anchor-post"."""
        page = (f"<html><body><article>"
                f"<h2 class='header-anchor-post'>Why Boring?</h2>"
                f"<div class='c-gallery-vertical-featured-image__header'>"
                f"<h2>Dead Man's Wire</h2></div>{_prose('Film')}"
                f"</article></body></html>")
        text = extract(page, "https://example.com/p").text
        assert "Why Boring?" in text
        assert "Dead Man's Wire" in text

    def test_names_are_matched_as_whole_tokens(self):
        soup = BeautifulSoup(
            "<html><body><div class='canvas-address'><p>kept words</p></div>"
            "<div class='ad-slot'><p>Advertisement text</p></div>"
            "</body></html>", "lxml")
        cleaned = str(strip_chrome(soup))
        assert "kept words" in cleaned
        assert "Advertisement text" not in cleaned

    def test_nothing_holding_most_of_the_page_is_removed(self):
        soup = BeautifulSoup(
            f"<html><body><div id='comments-wrapper'>{_prose('All')}</div>"
            "</body></html>", "lxml")
        assert "All paragraph 3" in str(strip_chrome(soup))

    def test_drop_leading_title_only_touches_a_matching_heading(self):
        fragment = "<h2>Something Else</h2><p>text</p>"
        assert drop_leading_title(fragment, "The Piece") == fragment
        assert "<h2>" not in drop_leading_title(
            "<h2>The Piece</h2><p>text</p>", "The Piece")


class TestSnapshotUrls:
    CANONICAL = ("<link rel='canonical' href='https://archive.ph/"
                 "2024.05.31-135535/https://www.stltoday.com/news/a.html'>")

    def test_canonical_link_names_the_original(self):
        page = (f"<html><head>{self.CANONICAL}<meta property='og:url' "
                f"content='https://archive.ph/TazWD'></head></html>")
        assert snapshot_urls(page, "https://archive.ph/TazWD") == (
            "https://www.stltoday.com/news/a.html", "https://archive.ph/TazWD")

    def test_search_box_is_the_fallback_on_an_archive_page(self):
        page = ("<html><body><input name='q' "
                "value='https://jacobin.com/2022/12/piece'></body></html>")
        assert snapshot_urls(page, "https://archive.is/OtQbq") == (
            "https://jacobin.com/2022/12/piece", "https://archive.is/OtQbq")

    def test_a_search_box_elsewhere_means_nothing(self):
        page = ("<html><body><input name='q' "
                "value='https://jacobin.com/x'></body></html>")
        assert snapshot_urls(page, "https://example.com/search") is None

    def test_ordinary_canonical_is_not_a_snapshot(self):
        page = ("<html><head><link rel='canonical' "
                "href='https://example.com/2024.05.31-135535/x'></head></html>")
        assert snapshot_urls(page, "https://example.com/p") is None

    def test_site_name_is_the_publisher_not_the_archive(self):
        page = ("<html><head>" + self.CANONICAL +
                "<meta property='og:site_name' content='archive.ph'></head>"
                "<body><article>" + "<p>ordinary article words here</p>" * 30
                + "</article></body></html>")
        result = extract(page, "https://archive.ph/TazWD")
        assert result.site_name == "stltoday.com"
        assert result.original_url == "https://www.stltoday.com/news/a.html"


def _snapshot(original: str, head: str, body: str,
              stamp: str = "2022.11.23-130517") -> str:
    """A trimmed archive.today page: its rewritten tags, its own header with
    the capture time, then the archived page."""
    return (
        f"<html><head><link rel='canonical' href='https://archive.ph/{stamp}/"
        f"{original}'><meta property='og:url' content='https://archive.ph/Ab1'>"
        f"<meta property='article:published_time' content='2022-11-23T13:05:17Z'>"
        f"{head}</head><body><div id='HEADER'><time datetime='2022-11-23T13:05:17Z'>"
        f"23 Nov 2022 13:05:17 UTC</time></div><div id='CONTENT'>{body}"
        + "<p>ordinary article words carrying the piece along here</p>" * 40
        + "</div></body></html>")


class TestSnapshotTitleAndDate:
    """archive.today cuts titles at 70 characters and stamps every date tag
    with the capture time, so a saved snapshot showed a truncated title
    and the day it was archived as the day it was published."""

    def test_truncated_title_is_completed_from_the_page(self):
        page = _snapshot(
            "https://www.stltoday.com/news/a.html",
            "<title>Police were unlicensed. A burglary case was dismissed as a "
            "result.</title><meta property='og:title' content='Police were "
            "unlicensed. A burglary case was dismis…'>",
            "<h1>Police were unlicensed. A burglary case was dismissed as a "
            "result.</h1>")
        result = extract(page, "https://archive.ph/Ab1")
        assert result.title == ("Police were unlicensed. A burglary case was "
                                "dismissed as a result.")

    def test_site_suffix_is_dropped(self):
        assert strip_site_suffix("War Bros | WIRED",
                                 "https://www.wired.com/story/x") == "War Bros"
        assert strip_site_suffix("Dril speaks - The Washington Post",
                                 "https://www.washingtonpost.com/x") \
            == "Dril speaks"
        assert strip_site_suffix("Latimore Dies - The New York Times",
                                 "https://www.nytimes.com/x") == "Latimore Dies"

    def test_a_dash_that_is_not_the_site_stays(self):
        assert strip_site_suffix("Rust - A Love Story",
                                 "https://example.com/x") == "Rust - A Love Story"

    def test_dateline_beats_the_capture_time(self):
        page = _snapshot(
            "https://www.washingtonpost.com/technology/2022/11/22/dril/",
            "<meta property='og:title' content='Dril speaks'>",
            "<h1>Dril speaks</h1><p>By Taylor Lorenz</p>"
            "<p>November 23, 2022 at 5:00 a.m. EST</p>")
        result = extract(page, "https://archive.ph/Ab1")
        assert result.published_at == "2022-11-23T05:00:00-05:00"

    def test_embedded_posts_older_than_the_url_date_are_passed_over(self):
        page = _snapshot(
            "https://www.washingtonpost.com/technology/2022/11/22/dril/",
            "", "<blockquote><time datetime='2016-01-07T23:39:46.000Z'>"
                "Jan 7, 2016</time></blockquote>"
                "<p>November 23, 2022 at 5:00 a.m. EST</p>")
        result = extract(page, "https://archive.ph/Ab1")
        assert result.published_at.startswith("2022-11-23")

    def test_numeric_date_is_read_the_way_the_url_agrees_with(self):
        page = _snapshot("https://jacobin.com/2022/12/bowling",
                         "", "<time>12.05.2022</time>",
                         stamp="2022.12.13-205349")
        assert extract(page, "https://archive.ph/Ab1").published_at \
            == "2022-12-05"

    def test_no_date_at_all_is_left_empty_not_the_capture_time(self):
        page = _snapshot("https://example.com/piece", "", "")
        assert extract(page, "https://archive.ph/Ab1").published_at is None

    def test_url_date_is_the_last_resort(self):
        page = _snapshot("https://example.com/2022/11/20/piece", "", "")
        assert extract(page, "https://archive.ph/Ab1").published_at \
            == "2022-11-20"

    def test_ordinary_pages_keep_their_metadata(self):
        page = ("<html><head><title>Sourdough - Wikipedia</title><meta "
                "property='article:published_time' "
                "content='2026-02-03T10:00:00Z'></head><body><article>"
                + "<p>ordinary article words here</p>" * 30
                + "</article></body></html>")
        result = extract(page, "https://en.wikipedia.org/wiki/Sourdough")
        assert result.original_url is None
        assert result.published_at.startswith("2026-02-03T10:00")


class TestSnapshotFormatting:
    """archive.today flattens a page to styled <div>s with no class names;
    see strip_chrome. trafilatura only splits a div at its links inside an
    <article>, which is where a publisher's body copy usually is."""

    @staticmethod
    def _snapshot(inner):
        filler = " ".join(["Plain running text for the article body."] * 6)
        paras = "".join(f"<div>Paragraph {n}. {filler}</div>" for n in range(6))
        return (f"<html><body><div id='HEADER'>archive</div><div id='CONTENT'>"
                f"<article><div><div>{inner}</div>{paras}</div></article>"
                f"</div></body></html>")

    def test_links_stay_inside_their_paragraph_with_their_spaces(self):
        page = self._snapshot(
            "<div>A 2007 study found that <a href='https://e.example/s'>frat "
            "brothers are more likely</a> than other students; and in a "
            "<a href='https://e.example/c'>2025 survey</a> at Cornell, more "
            "said the same thing again and again for many years.</div>")
        html = extract(page, "https://archive.ph/abc").readable_html
        soup = BeautifulSoup(html, "lxml")
        link = soup.find("a", string="2025 survey")
        assert link is not None and link.find_parent("p") is not None
        assert "in a <a" in html and "</a> at Cornell" in html

    def test_a_read_more_card_goes_with_its_picture(self):
        page = self._snapshot(
            "<div><div>Read More</div><a href='https://e.example/r'>"
            "<img src='https://e.example/t.jpg' alt='thumb'>Another Headline "
            "About Something Else Entirely</a><div>By <a href="
            "'https://e.example/a'>Some Writer</a></div></div>")
        html = extract(page, "https://archive.ph/abc").readable_html
        assert "<img" not in html
        assert "Another Headline" not in html and "Read More" not in html

    def test_latest_on_label_goes_with_its_carousel(self):
        links = "".join(f"<div><a href='https://e.example/{n}'>Story number "
                        f"{n} headline</a></div>" for n in range(5))
        page = self._snapshot(f"<div><div>LATEST ON EXAMPLE</div>{links}</div>")
        text = extract(page, "https://archive.ph/abc").text
        assert "LATEST ON EXAMPLE" not in text and "Story number" not in text

    def test_a_credit_without_its_photo_goes(self):
        page = self._snapshot("<span>Photo: Getty Images</span>")
        assert "Getty" not in extract(page, "https://archive.ph/abc").text

    def test_a_credit_under_its_photo_stays(self):
        soup = BeautifulSoup(
            "<html><body><article><img src='https://e.example/i.jpg'>"
            "<span>Illustration: Some Artist</span>"
            f"{_prose('Body')}</article></body></html>", "lxml")
        assert "Some Artist" in strip_chrome(soup).get_text()

    def test_a_video_player_leaves_no_controls_behind(self):
        page = self._snapshot(
            "<div><div><video>To view this video please enable JavaScript"
            "</video><div>Font Size Small Medium Large Current Time 0:00 "
            "Remaining Time -0:00</div></div><div>A Video Title</div></div>")
        text = extract(page, "https://archive.ph/abc").text
        assert "enable JavaScript" not in text and "Remaining Time" not in text
        assert "Paragraph 3" in text

    def test_live_pages_keep_their_divs(self):
        """Only snapshots are rewritten; elsewhere a <div> may be layout."""
        soup = BeautifulSoup(
            f"<html><body><div>Some text <a href='/x'>a link</a></div>"
            f"{_prose('Body')}</body></html>", "lxml")
        assert strip_chrome(soup).find("div").name == "div"

    def test_menu_links_do_not_become_paragraphs(self):
        menu = "".join(f"<div><a href='https://e.example/{n}'>Section {n}</a>"
                       f"</div>" for n in range(4))
        text = extract(self._snapshot(f"<div>{menu}</div>"),
                       "https://archive.ph/abc").text
        assert "Section 2" not in text and "Paragraph 3" in text

    def test_an_empty_comment_box_says_nothing(self):
        page = self._snapshot(
            "<div><div>There aren’t any comments yet.</div><div>Be the first "
            "to start the conversation! You need an account to add or like "
            "comments.</div></div>")
        text = extract(page, "https://archive.ph/abc").text
        assert "comments yet" not in text and "conversation" not in text

    def test_headings_keep_their_parts_together(self):
        """A <p> inside a snapshot's <h2> splits the heading apart."""
        page = self._snapshot("<h2><div>14.</div><div>Never send an Edible "
                              "Arrangement.</div></h2>")
        heading = strip_chrome(BeautifulSoup(page, "lxml")).find("h2")
        assert heading.find("p") is None
