import pytest

from src.extract import (Extracted, extract, looks_paywalled, normalize_url,
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
