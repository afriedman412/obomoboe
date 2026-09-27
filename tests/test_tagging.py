"""Tag suggestion. The Claude path is stubbed -- no test makes a real call."""
from bs4 import BeautifulSoup

from src import tagging

ARTICLE = (
    "Causal inference has roots in control theory. Control theory gave us "
    "feedback, and causal inference borrowed its language of intervention. "
    "A causal model is not a forecast: causal inference asks what would "
    "happen under an intervention, while forecasting asks what comes next. "
    "Control theory engineers think about feedback loops, and causal "
    "inference researchers think about counterfactuals. "
) * 6

OFFLINE = {"LLM_TAGS_ENABLED": False, "TAG_SUGGESTIONS": 6}


class TestPublisherTags:
    def test_reads_article_tag_meta(self):
        soup = BeautifulSoup(
            "<html><head>"
            "<meta property='article:tag' content='AI'>"
            "<meta property='article:tag' content='Cybersecurity'>"
            "</head><body></body></html>", "lxml")
        assert tagging.publisher_tags(soup) == ["ai", "cybersecurity"]

    def test_reads_ld_json_keywords(self):
        soup = BeautifulSoup(
            '<html><head><script type="application/ld+json">'
            '{"@type":"Article","keywords":"machine learning, statistics"}'
            "</script></head><body></body></html>", "lxml")
        assert "machine learning" in tagging.publisher_tags(soup)

    def test_rejects_urls_and_junk(self):
        soup = BeautifulSoup(
            "<html><head>"
            "<meta property='article:tag' content='https://example.com/x'>"
            "<meta property='article:tag' content='   '>"
            "<meta property='article:tag' content='the'>"
            "</head><body></body></html>", "lxml")
        assert tagging.publisher_tags(soup) == []


class TestKeywordTags:
    def test_finds_the_subject_phrases(self):
        tags = tagging.keyword_tags(ARTICLE, title="From Control Theory")
        assert any("causal" in t for t in tags)
        assert any("control" in t for t in tags)

    def test_prefers_phrases_over_their_parts(self):
        tags = tagging.keyword_tags(ARTICLE, title="Causal inference")
        assert any(" " in t for t in tags), f"no phrase in {tags}"

    def test_no_stopwords_ever(self):
        tags = tagging.keyword_tags(ARTICLE)
        assert not [t for t in tags
                    if any(p in tagging.STOPWORDS for p in t.split())]

    def test_short_text_yields_nothing(self):
        assert tagging.keyword_tags("too short to judge") == []


class TestSuggest:
    def test_works_with_no_key_and_no_network(self):
        tags = tagging.suggest(OFFLINE, "<html></html>", ARTICLE, "Causal")
        assert tags, "offline tagging must still produce something"
        assert len(tags) <= 6

    def test_publisher_tags_are_included(self):
        html = ("<html><head><meta property='article:tag' content='AI'>"
                "</head><body></body></html>")
        assert "ai" in tagging.suggest(OFFLINE, html, ARTICLE, "Causal")

    def test_respects_the_configured_limit(self):
        tags = tagging.suggest({**OFFLINE, "TAG_SUGGESTIONS": 2},
                               "<html></html>", ARTICLE, "Causal")
        assert len(tags) <= 2

    def test_zero_limit_disables_it(self):
        assert tagging.suggest({**OFFLINE, "TAG_SUGGESTIONS": 0},
                               "<html></html>", ARTICLE, "Causal") == []

    def test_broken_html_does_not_raise(self):
        assert tagging.suggest(OFFLINE, "<html><<>", ARTICLE, "T") is not None


class TestClaudePath:
    """The API is never actually called; only the wiring is checked."""

    def test_disabled_without_a_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert not tagging.llm_available({"LLM_TAGS_ENABLED": True})

    def test_disabled_when_toggled_off_even_with_a_key(self):
        assert not tagging.llm_available(
            {"LLM_TAGS_ENABLED": False, "ANTHROPIC_API_KEY": "sk-ant-x"})

    def test_settings_key_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
        assert tagging.api_key({"ANTHROPIC_API_KEY": "sk-ant-from-settings"}) \
            == "sk-ant-from-settings"
        assert tagging.api_key({}) == "sk-ant-from-env"

    def test_an_api_failure_falls_back_rather_than_raising(self, monkeypatch):
        """A bad key, no network or an API outage must cost you tags, not
        the archive."""
        monkeypatch.setattr(tagging, "llm_available", lambda config: True)
        monkeypatch.setattr(tagging, "api_key",
                            lambda config: (_ for _ in ()).throw(
                                RuntimeError("API is down")))
        assert tagging.llm_tags({}, ARTICLE, "T", []) == []

    def test_suggest_still_returns_offline_tags_when_claude_returns_nothing(
            self, monkeypatch):
        monkeypatch.setattr(tagging, "llm_tags", lambda *a, **k: [])
        tags = tagging.suggest({"TAG_SUGGESTIONS": 6}, "<html></html>",
                               ARTICLE, "Causal")
        assert tags, "must fall back to the offline candidates"
