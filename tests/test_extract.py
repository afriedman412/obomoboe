import pytest

from src.extract import (Extracted, extract,
                         is_challenge_page, looks_paywalled, normalize_url,
                         sanitize_fragment)

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
