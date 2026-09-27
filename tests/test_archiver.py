"""Archiver tests with the network stubbed out."""
import pytest

from src import archiver, db
from src import extract as extract_mod
from src.extract import FetchError

FULL_PAGE = """
<html><head><meta property="og:title" content="Real Article">
<meta property="og:site_name" content="Example"></head>
<body><article>%s</article></body></html>
""" % "".join(f"<p>Sentence {i} carrying a decent number of ordinary words "
              f"so the extractor treats this as a genuine article body.</p>"
              for i in range(30))

PAYWALL_STUB = """
<html><head><meta property="og:title" content="Locked Article"></head>
<body><article><p>Subscribe to continue reading this story.</p></article></body>
</html>
"""


@pytest.fixture()
def config(app):
    return dict(app.config)


def make_article(conn, url="https://example.com/post"):
    return db.insert_article(conn, url=url, original_url=url)


class TestArchiveArticle:
    def test_saves_snapshot_and_updates_row(self, config, conn, monkeypatch):
        article_id = make_article(conn)
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(FULL_PAGE, 200, url, {}))

        result = archiver.archive_article(config, article_id)
        assert result["ok"] is True

        row = db.get_article(conn, article_id)
        assert row["archive_status"] == "ok"
        assert row["archive_source"] == "direct"
        assert row["title"] == "Real Article"
        assert row["word_count"] > 200

        directory = archiver.article_dir(config, article_id)
        assert (directory / "readable.html").read_text()
        assert (directory / "original.html").read_text()
        assert "Sentence 1" in (directory / "article.txt").read_text()
        assert (directory / "meta.json").exists()

    def test_indexes_for_search(self, config, conn, monkeypatch):
        article_id = make_article(conn)
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(FULL_PAGE, 200, url, {}))
        archiver.archive_article(config, article_id)
        assert db.search_ids(conn, "Sentence") == [article_id]

    def test_scripts_stripped_from_raw_snapshot(self, config, conn, monkeypatch):
        article_id = make_article(conn)
        page = FULL_PAGE.replace("<article>", "<script>evil()</script><article>")
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(page, 200, url, {}))
        archiver.archive_article(config, article_id)

        raw = (archiver.article_dir(config, article_id) / "original.html").read_text()
        assert "evil()" not in raw

    def test_failure_is_recorded_not_raised(self, config, conn, monkeypatch):
        article_id = make_article(conn)

        def boom(url, ua, timeout, session=None):
            raise FetchError("connection refused")

        monkeypatch.setattr(archiver, "fetch", boom)
        result = archiver.archive_article(config, article_id)

        assert result["ok"] is False
        row = db.get_article(conn, article_id)
        assert row["archive_status"] == "failed"
        assert "connection refused" in row["archive_error"]


class TestArchiveTodayFallback:
    def test_paywall_falls_back_to_archive_today(self, config, conn, monkeypatch):
        config["ARCHIVE_PH_ENABLED"] = True
        article_id = make_article(conn)

        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(PAYWALL_STUB, 200, url, {}))
        monkeypatch.setattr(
            archiver, "fetch_from_archive_ph",
            lambda url, ua, hosts, timeout, session=None: (FULL_PAGE, 200, url))

        result = archiver.archive_article(config, article_id)
        assert result["ok"] is True
        assert result["source"] == "archive.today"

        row = db.get_article(conn, article_id)
        assert row["archive_source"] == "archive.today"
        assert row["word_count"] > 200

    def test_keeps_direct_copy_when_archive_is_worse(
            self, config, conn, monkeypatch):
        config["ARCHIVE_PH_ENABLED"] = True
        article_id = make_article(conn)
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(PAYWALL_STUB, 200, url, {}))
        monkeypatch.setattr(
            archiver, "fetch_from_archive_ph",
            lambda url, ua, hosts, timeout, session=None: (PAYWALL_STUB, 200, url))

        result = archiver.archive_article(config, article_id)
        assert result["ok"] is True
        assert db.get_article(conn, article_id)["archive_source"] == "direct"

    def test_records_note_when_archive_unavailable(self, config, conn,
                                                   monkeypatch):
        config["ARCHIVE_PH_ENABLED"] = True
        article_id = make_article(conn)

        def no_snapshot(url, ua, hosts, timeout, session=None):
            raise FetchError("no snapshot exists yet")

        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(PAYWALL_STUB, 200, url, {}))
        monkeypatch.setattr(archiver, "fetch_from_archive_ph", no_snapshot)

        archiver.archive_article(config, article_id)
        row = db.get_article(conn, article_id)
        # We still keep the stub, but say why it is thin.
        assert row["archive_status"] == "ok"
        assert "no snapshot exists yet" in row["archive_error"]

    def test_force_archive_skips_live_fetch(self, config, conn, monkeypatch):
        config["ARCHIVE_PH_ENABLED"] = True
        article_id = make_article(conn)

        def should_not_run(*args, **kwargs):
            raise AssertionError("live fetch should have been skipped")

        monkeypatch.setattr(archiver, "fetch", should_not_run)
        monkeypatch.setattr(
            archiver, "fetch_from_archive_ph",
            lambda url, ua, hosts, timeout, session=None: (FULL_PAGE, 200, url))

        result = archiver.archive_article(config, article_id,
                                          force_archive_ph=True)
        assert result["source"] == "archive.today"

    def test_forced_archive_failure_has_no_none_prefix(self, config, conn,
                                                       monkeypatch):
        """Forcing skips the live fetch, so there is no direct error to lead
        with -- the message must not start with a stringified None."""
        config["ARCHIVE_PH_ENABLED"] = True
        article_id = make_article(conn)

        def no_snapshot(url, ua, hosts, timeout, session=None):
            raise FetchError("no snapshot exists yet")

        monkeypatch.setattr(archiver, "fetch_from_archive_ph", no_snapshot)

        archiver.archive_article(config, article_id, force_archive_ph=True)
        error = db.get_article(conn, article_id)["archive_error"]
        assert not error.startswith("None")
        assert error.startswith("archive.today unavailable")


class TestEnqueue:
    def test_second_enqueue_is_refused_while_in_flight(self):
        archiver._inflight.clear()
        assert archiver.enqueue({}, 4242) is True
        assert archiver.enqueue({}, 4242, force_archive_ph=True) is False
        assert archiver.enqueue({}, 4243) is True
        archiver._inflight.clear()
        while not archiver._queue.empty():
            archiver._queue.get()
            archiver._queue.task_done()


class TestDoesNotDegradeGoodArchive:
    def test_bot_check_does_not_overwrite_a_good_copy(self, config, conn,
                                                      monkeypatch):
        article_id = make_article(conn)
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(FULL_PAGE, 200, url, {}))
        archiver.archive_article(config, article_id)
        good = db.get_article(conn, article_id)
        assert good["archive_status"] == "ok"

        challenge = ("<html><head><title>Just a moment...</title></head><body>"
                     "<p>Enable JavaScript and cookies to continue</p>"
                     "</body></html>")
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None: extract_mod.Fetched(challenge, 200, url, {}))

        archiver.archive_article(config, article_id)

        row = db.get_article(conn, article_id)
        assert row["title"] == good["title"], "title was clobbered"
        assert row["word_count"] == good["word_count"]
        assert row["archive_status"] == "ok"
        assert "kept the earlier copy" in row["archive_error"]
        # The files on disk must still be the good copy, not the bot check.
        text = (archiver.article_dir(config, article_id) / "article.txt").read_text()
        assert "Sentence 1" in text
        assert "Enable JavaScript" not in text


class TestTagModes:
    """off / suggest / apply, the three ways an article can be tagged."""

    def _archive(self, config, conn, monkeypatch, mode):
        config["TAG_MODE"] = mode
        config["LLM_TAGS_ENABLED"] = False
        article_id = make_article(conn, url=f"https://example.com/{mode}")
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None:
                extract_mod.Fetched(FULL_PAGE, 200, url, {}))
        archiver.archive_article(config, article_id)
        row = db.get_article(conn, article_id)
        return (db.tags_for_articles(conn, [article_id]).get(article_id, []),
                db.get_suggestions(row))

    def test_off_produces_nothing(self, config, conn, monkeypatch):
        tags, suggestions = self._archive(config, conn, monkeypatch, "off")
        assert tags == [] and suggestions == []

    def test_suggest_offers_without_filing(self, config, conn, monkeypatch):
        tags, suggestions = self._archive(config, conn, monkeypatch, "suggest")
        assert tags == [], "suggest mode must not apply tags"
        assert suggestions, "suggest mode must offer something"

    def test_apply_files_them_and_leaves_nothing_pending(
            self, config, conn, monkeypatch):
        tags, suggestions = self._archive(config, conn, monkeypatch, "apply")
        assert tags, "apply mode must file tags"
        assert suggestions == [], "applied tags must not also be suggested"

    def test_default_is_suggest(self, config, conn, monkeypatch):
        config.pop("TAG_MODE", None)
        config["LLM_TAGS_ENABLED"] = False
        article_id = make_article(conn, url="https://example.com/default")
        monkeypatch.setattr(
            archiver, "fetch",
            lambda url, ua, timeout, session=None:
                extract_mod.Fetched(FULL_PAGE, 200, url, {}))
        archiver.archive_article(config, article_id)
        row = db.get_article(conn, article_id)
        assert db.get_suggestions(row)
        assert db.tags_for_articles(conn, [article_id]).get(article_id, []) == []
