import re

from bs4 import BeautifulSoup

from src import archiver, db, theme


def add(client, url, tags=""):
    return client.post("/add", data={"url": url, "tags": tags},
                       headers={"X-Requested-With": "XMLHttpRequest"})


class TestAdd:
    def test_adds_and_normalizes(self, client, conn):
        resp = add(client, "example.com/post?utm_source=news")
        assert resp.status_code == 201
        row = db.find_by_url(conn, "https://example.com/post")
        assert row is not None
        assert row["status"] == "unread"
        assert row["archive_status"] == "pending"

    def test_duplicate_is_rejected_not_duplicated(self, client, conn):
        add(client, "https://example.com/post")
        resp = add(client, "https://example.com/post/?utm_medium=x")
        assert resp.get_json()["duplicate"] is True
        count = conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
        assert count == 1

    def test_bad_url_is_a_400(self, client):
        assert add(client, "nope nope").status_code == 400

    def test_tags_applied_on_add(self, client, conn):
        article_id = add(client, "https://example.com/x", "Rust, Long Reads")
        article_id = article_id.get_json()["id"]
        tags = db.tags_for_articles(conn, [article_id])[article_id]
        assert tags == ["long reads", "rust"]


class TestListing:
    def test_index_renders_articles(self, client, conn):
        article_id = add(client, "https://example.com/post").get_json()["id"]
        db.update_article(conn, article_id, title="A Fine Headline")
        body = client.get("/").get_data(as_text=True)
        assert "A Fine Headline" in body

    def test_status_filter(self, client, conn):
        first = add(client, "https://example.com/a").get_json()["id"]
        add(client, "https://example.com/b")
        db.set_status(conn, first, db.READ)

        unread = client.get("/?status=unread").get_data(as_text=True)
        assert "example.com/b" in unread and "example.com/a" not in unread

        read = client.get("/?status=read").get_data(as_text=True)
        assert "example.com/a" in read and "example.com/b" not in read

    def test_home_opens_on_unread(self, client, conn):
        first = add(client, "https://example.com/a").get_json()["id"]
        add(client, "https://example.com/b")
        db.set_status(conn, first, db.READ)

        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        assert "Unread" in soup.select_one(".tab.is-on").get_text()
        page = str(soup)
        assert "example.com/b" in page and "example.com/a" not in page

        everything = client.get("/?status=all").get_data(as_text=True)
        assert "example.com/a" in everything and "example.com/b" in everything

    def test_tabs_run_unread_read_all(self, client):
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        labels = [tab.get_text(" ", strip=True).split()[0]
                  for tab in soup.select(".tabs .tab")]
        assert labels == ["Unread", "Read", "All"]

    def test_empty_unread_says_caught_up(self, client, conn):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        db.set_status(conn, article_id, db.READ)
        assert "All caught up" in client.get("/").get_data(as_text=True)

    def _dated(self, conn, slug, added, published=None):
        article_id = db.insert_article(conn, url=f"https://e.example/{slug}",
                                       original_url=f"https://e.example/{slug}")
        db.update_article(conn, article_id, title=slug, added_at=added,
                          published_at=published)
        return article_id

    def _breaks(self, client, query):
        soup = BeautifulSoup(client.get("/" + query).get_data(as_text=True), "lxml")
        return [li.get_text(strip=True) if "month-break" in li["class"]
                else li.select_one(".title").get_text(strip=True)
                for li in soup.select(".articles > li")]

    def test_month_breaks_follow_date_added(self, client, conn):
        self._dated(conn, "oct-a", "2026-10-05T12:00:00")
        self._dated(conn, "oct-b", "2026-10-01T12:00:00")
        self._dated(conn, "aug", "2026-08-20T12:00:00")
        self._dated(conn, "dec", "2025-12-15T12:00:00")
        assert self._breaks(client, "?status=all") == [
            "October 2026", "oct-a", "oct-b", "August 2026", "aug",
            "December 2025", "dec"]
        assert self._breaks(client, "?status=all&sort=added_asc")[:2] == [
            "December 2025", "dec"]

    def test_month_breaks_follow_date_published(self, client, conn):
        self._dated(conn, "new", "2026-10-05T12:00:00", "2024-03-02")
        self._dated(conn, "undated", "2026-10-04T12:00:00")
        assert self._breaks(client, "?status=all&sort=published") == [
            "March 2024", "new", "Undated", "undated"]

    def test_no_month_breaks_when_dates_are_not_the_order(self, client, conn):
        self._dated(conn, "b", "2026-10-05T12:00:00")
        self._dated(conn, "a", "2026-08-05T12:00:00")
        assert self._breaks(client, "?status=all&sort=title") == ["a", "b"]

    def test_tag_filter(self, client):
        add(client, "https://example.com/a", "python")
        add(client, "https://example.com/b", "cooking")
        body = client.get("/?tag=python").get_data(as_text=True)
        assert "example.com/a" in body and "example.com/b" not in body

    def test_full_text_search(self, client, conn):
        first = add(client, "https://example.com/a").get_json()["id"]
        second = add(client, "https://example.com/b").get_json()["id"]
        db.index_article(conn, first, "Bridges", "a long piece about suspension")
        db.index_article(conn, second, "Bread", "sourdough starter maintenance")

        body = client.get("/?q=sourdough").get_data(as_text=True)
        assert "example.com/b" in body and "example.com/a" not in body

    def test_search_survives_fts_syntax_errors(self, client):
        add(client, "https://example.com/a")
        assert client.get('/?q=broken"quote AND').status_code == 200

    def test_sort_by_title(self, client, conn):
        first = add(client, "https://example.com/a").get_json()["id"]
        second = add(client, "https://example.com/b").get_json()["id"]
        db.update_article(conn, first, title="Zebra")
        db.update_article(conn, second, title="Aardvark")
        body = client.get("/?sort=title").get_data(as_text=True)
        assert body.index("Aardvark") < body.index("Zebra")


class TestMutations:
    def test_toggle_status(self, client, conn):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        resp = client.post(f"/a/{article_id}/status",
                           headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["status"] == "read"
        assert db.get_article(conn, article_id)["read_at"] is not None

        resp = client.post(f"/a/{article_id}/status",
                           headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["status"] == "unread"
        assert db.get_article(conn, article_id)["read_at"] is None

    def test_add_and_remove_tags(self, client, conn):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        client.post(f"/a/{article_id}/tags", data={"add": "essays, 2026"})
        assert db.tags_for_articles(conn, [article_id])[article_id] == [
            "2026", "essays"]

        client.post(f"/a/{article_id}/tags", data={"remove": "2026"})
        assert db.tags_for_articles(conn, [article_id])[article_id] == ["essays"]

    def test_notes_round_trip(self, client, conn):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        client.post(f"/a/{article_id}/notes", data={"notes": "read this soon"})
        assert db.get_article(conn, article_id)["notes"] == "read this soon"

    def test_delete_removes_row_and_files(self, client, conn, app):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        directory = app.config["ARCHIVE_DIR"] / str(article_id)
        directory.mkdir(parents=True)
        (directory / "readable.html").write_text("<p>hi</p>")

        client.post(f"/a/{article_id}/delete",
                    headers={"X-Requested-With": "XMLHttpRequest"})
        assert db.get_article(conn, article_id) is None
        assert not directory.exists()

    def test_missing_article_is_404(self, client):
        assert client.get("/a/999").status_code == 404
        assert client.post("/a/999/status").status_code == 404


class TestArticlePage:
    def test_renders_saved_body(self, client, app):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        directory = app.config["ARCHIVE_DIR"] / str(article_id)
        directory.mkdir(parents=True)
        (directory / "readable.html").write_text("<p>The saved body text.</p>")

        body = client.get(f"/a/{article_id}").get_data(as_text=True)
        assert "The saved body text." in body

    def test_local_image_paths_are_rewritten(self, client, app):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        directory = app.config["ARCHIVE_DIR"] / str(article_id)
        directory.mkdir(parents=True)
        (directory / "readable.html").write_text('<img src="assets/pic.png">')

        body = client.get(f"/a/{article_id}").get_data(as_text=True)
        assert f'src="/a/{article_id}/assets/pic.png"' in body

    def test_original_snapshot_is_sandboxed(self, client, app):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        directory = app.config["ARCHIVE_DIR"] / str(article_id)
        directory.mkdir(parents=True)
        (directory / "original.html").write_text("<html><body>raw</body></html>")

        resp = client.get(f"/a/{article_id}/original")
        assert resp.status_code == 200
        assert "sandbox" in resp.headers["Content-Security-Policy"]

    def test_api_returns_pending_rows(self, client):
        article_id = add(client, "https://example.com/a").get_json()["id"]
        data = client.get(f"/api/articles?ids={article_id}").get_json()
        assert data["articles"][0]["archive_status"] == "pending"
        assert data["counts"]["unread"] == 1


class TestCapture:
    """Pages handed over by the browser, for sites the server cannot fetch."""

    PAGE = ("<html><head><title>Behind The Wall</title>"
            "<meta property='og:site_name' content='Walled'></head><body>"
            "<article>" + "<p>Real subscriber-only sentence with plenty of "
            "ordinary words in it.</p>" * 30 + "</article></body></html>")

    def _capture(self, client, url="https://walled.example/piece", **extra):
        body = {"url": url, "html": self.PAGE}
        body.update(extra)
        return client.post("/capture", json=body)

    def test_captured_page_is_archived_without_fetching(self, client, app,
                                                        conn, monkeypatch):
        def no_network(*args, **kwargs):
            raise AssertionError("capture must not hit the network")

        monkeypatch.setattr(archiver, "fetch", no_network)
        monkeypatch.setattr(archiver, "fetch_from_archive_ph", no_network)

        resp = self._capture(client, tags="research")
        assert resp.status_code == 201
        article_id = resp.get_json()["id"]

        archiver.archive_article(dict(app.config), article_id,
                                 from_capture=True)

        row = db.get_article(conn, article_id)
        assert row["archive_status"] == "ok"
        assert row["archive_source"] == "browser"
        assert row["title"] == "Behind The Wall"
        assert row["word_count"] > 200
        assert "research" in db.tags_for_articles(conn, [article_id])[article_id]

    def test_captured_text_is_searchable(self, client, app, conn):
        article_id = self._capture(client).get_json()["id"]
        archiver.archive_article(dict(app.config), article_id,
                                 from_capture=True)
        assert db.search_ids(conn, "subscriber") == [article_id]

    def test_capturing_the_same_url_twice_updates_one_row(self, client):
        first = self._capture(client).get_json()["id"]
        second = self._capture(client).get_json()["id"]
        assert first == second

    def test_empty_page_is_refused(self, client):
        resp = client.post("/capture", json={"url": "https://e.example/x",
                                             "html": "   "})
        assert resp.status_code == 400
        assert "no page content" in resp.get_json()["error"]

    def test_oversize_page_is_refused(self, client, app):
        app.config["MAX_CAPTURE_BYTES"] = 500
        resp = client.post("/capture", json={"url": "https://e.example/big",
                                             "html": "x" * 2000})
        assert resp.status_code == 413

    def test_bad_url_is_refused(self, client):
        resp = client.post("/capture", json={"url": "not a url",
                                             "html": "<p>hi</p>"})
        assert resp.status_code == 400

    def test_browser_may_send_and_read_the_reply(self, client):
        """Cross-origin posts to /capture -- the documented API -- need these.
        The bookmarklet itself now posts same-origin from /capture/window."""
        pre = client.open("/capture", method="OPTIONS")
        assert pre.status_code == 204
        assert pre.headers["Access-Control-Allow-Origin"] == "*"
        assert "POST" in pre.headers["Access-Control-Allow-Methods"]
        assert self._capture(client).headers["Access-Control-Allow-Origin"] == "*"

    def test_bookmarklet_hands_over_through_a_window_on_our_origin(self, client):
        """A fetch from the article's page is blocked by a strict
        connect-src (nytimes.com allows only https:), so the bookmarklet
        must not fetch /capture directly from there."""
        page = client.get("/bookmarklet").get_data(as_text=True)
        href = BeautifulSoup(page, "html.parser").select_one(
            ".bookmarklet-drag")["href"]
        assert href.startswith("javascript:")
        assert "/capture/window" in href
        assert "postMessage" in href
        assert "fetch(" not in href

    def test_capture_window_posts_same_origin(self, client):
        page = client.get("/capture/window").get_data(as_text=True)
        assert "obomoboe-ready" in page
        assert "obomoboe-capture" in page
        assert "fetch('/capture'" in page

    def test_capture_from_a_snapshot_files_under_the_original(
            self, client, app, conn):
        """The bookmarklet clicked on an archive.ph page used to save the
        article as archive.ph; the publisher's URL was lost."""
        page = self.PAGE.replace(
            "<head>",
            "<head><link rel='canonical' href='https://archive.ph/"
            "2026.09.05-204116/https://www.nytimes.com/2026/09/05/arts/"
            "latimore.html'><meta property='og:url' "
            "content='https://archive.ph/2Chks'>")
        resp = client.post("/capture", json={
            "url": "https://archive.ph/2Chks", "html": page})
        article_id = resp.get_json()["id"]
        row = db.get_article(conn, article_id)
        original = "https://www.nytimes.com/2026/09/05/arts/latimore.html"
        assert row["url"] == original
        assert row["original_url"] == original
        assert row["archive_url"] == "https://archive.ph/2Chks"

        archiver.archive_article(dict(app.config), article_id,
                                 from_capture=True)
        body = client.get(f"/a/{article_id}").get_data(as_text=True)
        assert f'href="{original}"' in body
        assert 'href="https://archive.ph/2Chks"' in body

    def test_capturing_a_snapshot_of_a_saved_article_refiles_it(self, client,
                                                                conn):
        first = self._capture(client, url="https://walled.example/piece")
        page = self.PAGE.replace(
            "<head>",
            "<head><link rel='canonical' href='https://archive.ph/"
            "2026.01.01-000000/https://walled.example/piece'>")
        second = client.post("/capture", json={
            "url": "https://archive.ph/Xyz12", "html": page})
        assert second.get_json()["id"] == first.get_json()["id"]

    def test_captured_bot_check_is_refused_not_filed(self, client, app, conn):
        """Capturing exists to get past a challenge; a captured challenge is
        a failed capture, not an article."""
        challenge = ("<html><head><title>Just a moment...</title></head><body>"
                     "<script>window._cf_chl_opt={cvId:'3'};</script>"
                     "</body></html>")
        resp = client.post("/capture", json={"url": "https://walled.example/z",
                                             "html": challenge})
        article_id = resp.get_json()["id"]
        archiver.archive_article(dict(app.config), article_id,
                                 from_capture=True)
        row = db.get_article(conn, article_id)
        assert row["archive_status"] == "failed"
        assert "still a bot check" in row["archive_error"]

    def test_recapturing_still_accepts_tags(self, client):
        first = self._capture(client, tags="one").get_json()["id"]
        second = self._capture(client, tags="two").get_json()["id"]
        assert first == second
        resp = client.get(f"/api/articles?ids={first}")
        assert set(resp.get_json()["articles"][0]["tags"]) == {"one", "two"}

    def test_capturing_obomoboes_own_page_is_refused_clearly(self, client):
        """Easy mistake: the bookmarklet is clicked where it was dragged from."""
        resp = client.post("/capture", json={
            "url": "http://localhost/bookmarklet",
            "html": "<html><body><p>drag this to your bookmarks bar</p></body></html>"})
        assert resp.status_code == 400
        assert "obomoboe's own page" in resp.get_json()["error"]


class TestTimestampDisplay:
    def test_time_is_shown_when_the_page_gave_one(self, app):
        fmt = app.jinja_env.filters["humantime"]
        with app.app_context():
            assert "at" in fmt("2026-09-24T22:49:25+00:00")
            # No leading zeros, spelled without %-d so it works on Windows.
            assert fmt("2026-09-04T09:05:00") == "Sep 4, 2026 at 9:05 AM"
            assert fmt("2026-09-04T00:30:00") == "Sep 4, 2026 at 12:30 AM"
            # A bare date has no time to show, so it reads as a date.
            assert fmt("2026-09-24") == "Sep 24, 2026"
            assert fmt(None) == ""
            # Junk passes through rather than raising.
            assert fmt("not a date") == "not a date"

    def test_author_appears_in_the_list(self, client, conn, app):
        article_id = db.insert_article(conn, url="https://e.example/a",
                                       original_url="https://e.example/a")
        db.update_article(conn, article_id, title="Piece", author="Ada Lovelace")
        page = client.get("/").get_data(as_text=True)
        assert "Ada Lovelace" in page


class TestRearchiveOfCaptured:
    def test_rearchive_reuses_the_capture_instead_of_fetching(
            self, client, app, conn, monkeypatch):
        page = ("<html><head><title>Held Page</title></head><body><article>"
                + "<p>captured words that only the browser could reach</p>" * 30
                + "</article></body></html>")
        article_id = client.post("/capture", json={
            "url": "https://walled.example/held", "html": page}).get_json()["id"]

        def no_network(*args, **kwargs):
            raise AssertionError("re-archive must reuse the captured page")

        monkeypatch.setattr(archiver, "fetch", no_network)
        monkeypatch.setattr(archiver, "fetch_from_archive_ph", no_network)

        resp = client.post(f"/a/{article_id}/rearchive",
                           headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["from_capture"] is True
        archiver.archive_article(dict(app.config), article_id,
                                 from_capture=True)
        assert db.get_article(conn, article_id)["archive_source"] == "browser"


class TestOriginalLinkOnPage:
    def test_the_full_source_url_is_shown(self, client, conn):
        url = "https://example.com/section/a-long-piece-about-things?ref=x"
        article_id = db.insert_article(conn, url=url, original_url=url)
        db.update_article(conn, article_id, title="A Piece")
        page = client.get(f"/a/{article_id}").get_data(as_text=True)
        # Not merely linked -- the address itself is on the page.
        assert url in page
        assert 'rel="noreferrer noopener"' in page


class TestSettingsPage:
    def test_defaults_are_conservative(self, client):
        page = client.get("/settings").get_data(as_text=True)
        for label in ("Off", "Auto-suggest", "Auto-apply"):
            assert f">{label}<" in page
        assert "Claude tagging is off" in page

    def test_saving_toggles_round_trips(self, client, conn):
        client.post("/settings", data={"tag_mode": "apply", "llm_tags": "on",
                                       "suggestions": "4",
                                       "tag_model": "claude-opus-5"})
        stored = db.get_settings(conn)
        assert stored["TAG_MODE"] == "apply"
        assert stored["TAG_SUGGESTIONS"] == 4

    def test_each_mode_round_trips(self, client, conn):
        for mode in ("off", "suggest", "apply"):
            client.post("/settings", data={"tag_mode": mode})
            assert db.get_settings(conn)["TAG_MODE"] == mode

    def test_a_bogus_mode_is_ignored(self, client, conn):
        client.post("/settings", data={"tag_mode": "suggest"})
        client.post("/settings", data={"tag_mode": "delete-everything"})
        assert db.get_settings(conn)["TAG_MODE"] == "suggest"

    def test_the_old_auto_apply_setting_is_carried_over(self, conn):
        """The three-way mode replaced a boolean; anyone who had set it
        should not be silently reset."""
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                     ("AUTO_APPLY_TAGS", "1"))
        conn.commit()
        assert db.get_settings(conn)["TAG_MODE"] == "apply"

    def test_api_key_is_stored_and_shown_masked(self, client, conn):
        client.post("/settings", data={"api_key": "sk-ant-secret-value-1234"})
        assert db.get_settings(conn)["ANTHROPIC_API_KEY"] == \
            "sk-ant-secret-value-1234"
        page = client.get("/settings").get_data(as_text=True)
        assert "sk-ant-secret-value-1234" not in page, "key shown in full"
        assert "sk-ant-" in page and "1234" in page

    def test_resubmitting_the_masked_value_keeps_the_key(self, client, conn):
        """The form shows a mask; saving other settings must not wipe the key."""
        client.post("/settings", data={"api_key": "sk-ant-secret-value-1234"})
        masked = "sk-ant-…1234"
        client.post("/settings", data={"api_key": masked, "auto_apply": "on"})
        assert db.get_settings(conn)["ANTHROPIC_API_KEY"] == \
            "sk-ant-secret-value-1234"

    def test_key_can_be_cleared(self, client, conn):
        client.post("/settings", data={"api_key": "sk-ant-secret-value-1234"})
        client.post("/settings", data={"clear_key": "on"})
        assert db.get_settings(conn).get("ANTHROPIC_API_KEY", "") == ""

    def test_suggestion_count_is_clamped(self, client, conn):
        client.post("/settings", data={"suggestions": "999"})
        assert db.get_settings(conn)["TAG_SUGGESTIONS"] == 12


class TestDismissFromTheList:
    def _article_with_suggestions(self, conn, names):
        article_id = db.insert_article(conn, url="https://e.example/s",
                                       original_url="https://e.example/s")
        db.update_article(conn, article_id, title="Piece")
        db.set_suggestions(conn, article_id, names)
        return article_id

    def test_dismiss_clears_what_is_still_on_offer(self, client, conn):
        article_id = self._article_with_suggestions(conn, ["one", "two"])
        resp = client.post(f"/a/{article_id}/suggestions/dismiss",
                           headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.status_code == 200
        assert db.get_suggestions(db.get_article(conn, article_id)) == []

    def test_dismiss_leaves_accepted_tags_alone(self, client, conn):
        article_id = self._article_with_suggestions(conn, ["keeper", "junk"])
        client.post(f"/a/{article_id}/suggestions/accept", data={"tag": "keeper"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
        client.post(f"/a/{article_id}/suggestions/dismiss",
                    headers={"X-Requested-With": "XMLHttpRequest"})
        tags = db.tags_for_articles(conn, [article_id])[article_id]
        assert tags == ["keeper"]
        assert db.get_suggestions(db.get_article(conn, article_id)) == []

    def test_the_button_stays_once_suggestions_are_gone(self, client, conn):
        """It commits tag removals as well, so it is not tied to suggestions."""
        article_id = self._article_with_suggestions(conn, ["one"])
        client.post(f"/a/{article_id}/suggestions/dismiss",
                    headers={"X-Requested-With": "XMLHttpRequest"})
        assert 'data-action="commit-removals"' in \
            client.get("/").get_data(as_text=True)

    def test_accepting_reports_what_is_left(self, client, conn):
        """The list uses this to know when to drop the dismiss button."""
        article_id = self._article_with_suggestions(conn, ["only"])
        resp = client.post(f"/a/{article_id}/suggestions/accept",
                           data={"tag": "only"},
                           headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["suggestions"] == []


class TestRemovableTagsFromTheList:
    def _row(self, conn, tags=(), suggestions=()):
        article_id = db.insert_article(conn, url="https://e.example/r",
                                       original_url="https://e.example/r")
        db.update_article(conn, article_id, title="Piece")
        for name in tags:
            db.add_tag(conn, article_id, name)
        db.set_suggestions(conn, article_id, list(suggestions))
        return article_id

    def test_several_tags_can_be_removed_in_one_go(self, client, conn):
        article_id = self._row(conn, tags=["keep", "drop one", "drop two"])
        resp = client.post(f"/a/{article_id}/tags",
                           data={"remove": "drop one,drop two", "dismiss": "1"},
                           headers={"X-Requested-With": "XMLHttpRequest"})
        assert resp.get_json()["tags"] == ["keep"]

    def test_a_single_name_still_works(self, client, conn):
        """The article page sends one name; that must keep working."""
        article_id = self._row(conn, tags=["keep", "drop"])
        client.post(f"/a/{article_id}/tags", data={"remove": "drop"})
        assert db.tags_for_articles(conn, [article_id])[article_id] == ["keep"]

    def test_the_same_press_clears_suggestions(self, client, conn):
        article_id = self._row(conn, tags=["drop"], suggestions=["a", "b"])
        resp = client.post(f"/a/{article_id}/tags",
                           data={"remove": "drop", "dismiss": "1"},
                           headers={"X-Requested-With": "XMLHttpRequest"})
        body = resp.get_json()
        assert body["tags"] == [] and body["suggestions"] == []

    def test_dismissing_nothing_removes_nothing(self, client, conn):
        article_id = self._row(conn, tags=["keep"])
        client.post(f"/a/{article_id}/tags", data={"dismiss": "1"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
        assert db.tags_for_articles(conn, [article_id])[article_id] == ["keep"]

    def test_the_button_is_always_there(self, client, conn):
        """It commits removals too, so it cannot come and go with suggestions."""
        article_id = self._row(conn, tags=["solo"])
        assert db.get_suggestions(db.get_article(conn, article_id)) == []
        page = client.get("/").get_data(as_text=True)
        assert 'data-action="commit-removals"' in page

    def test_dismiss_sits_with_the_row_actions_not_among_the_chips(
            self, client, conn):
        """It is a control for the row, not another tag to read past."""
        article_id = self._row(conn, tags=["applied"], suggestions=["offered"])
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        row = soup.select_one(f'.article[data-id="{article_id}"]')
        assert row.select_one('.tags [data-action="commit-removals"]') is None
        assert row.select_one('.actions [data-action="commit-removals"]')

    def test_dismiss_still_submits_the_removal_form(self, client, conn):
        """Placed outside that form, it stays wired to it by id."""
        article_id = self._row(conn, tags=["x"])
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        button = soup.select_one('[data-action="commit-removals"]')
        form = soup.select_one("#" + button["form"])
        assert form is not None
        assert form["action"].endswith(f"/a/{article_id}/tags")

    def test_tags_post_back_without_javascript(self, client, conn):
        """Each chip is a submit button in a real form, so removal works with
        scripting off -- immediately, rather than staged."""
        article_id = self._row(conn, tags=["gone"])
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        form = soup.select_one(".tags form[action$='/tags']")
        assert form is not None and form.get("method") == "post"
        chip = form.find("button", attrs={"name": "remove"})
        assert chip["value"] == "gone" and chip["type"] == "submit"

        client.post(f"/a/{article_id}/tags", data={"remove": "gone"})
        assert db.tags_for_articles(conn, [article_id]).get(article_id, []) == []


class TestListStatusButton:
    def test_it_posts_status_rather_than_linking_to_the_article(
            self, client, conn):
        article_id = db.insert_article(conn, url="https://e.example/m",
                                       original_url="https://e.example/m")
        db.update_article(conn, article_id, title="Piece")
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        actions = soup.select_one(".actions")
        assert actions.find("a") is None, "no second link to the article"
        form = actions.find("form")
        assert form["action"].endswith(f"/a/{article_id}/status")
        assert form.find("input", attrs={"name": "status"})["value"] == "read"

    def test_it_works_without_javascript(self, client, conn):
        article_id = db.insert_article(conn, url="https://e.example/m2",
                                       original_url="https://e.example/m2")
        client.post(f"/a/{article_id}/status", data={"status": "read"})
        assert db.get_article(conn, article_id)["status"] == "read"

    def test_the_label_follows_the_state(self, client, conn):
        article_id = db.insert_article(conn, url="https://e.example/m3",
                                       original_url="https://e.example/m3")
        db.update_article(conn, article_id, title="Piece")
        assert "mark read" in client.get("/").get_data(as_text=True)
        db.set_status(conn, article_id, db.READ)
        assert "mark unread" in client.get("/?status=all").get_data(as_text=True)


class TestArchiveMarks:
    """The status pills are marks now; the sentence they replaced has to
    survive as the accessible label."""

    def _article(self, conn, status, source=None, error=None):
        article_id = db.insert_article(conn, url=f"https://e.example/{status}",
                                       original_url=f"https://e.example/{status}")
        db.update_article(conn, article_id, title="Piece",
                          archive_status=status, archive_source=source,
                          archive_error=error)
        return article_id

    def _mark(self, client, article_id):
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        return soup.select_one(f'.article[data-id="{article_id}"] .mark')

    def test_each_state_is_labelled_in_words(self, client, conn):
        cases = [
            ("pending", None, None, "Archiving"),
            ("ok", "direct", None, "Archived"),
            ("failed", None, None, "Archive failed"),
        ]
        for status, source, error, expected in cases:
            article_id = self._article(conn, status, source, error)
            mark = self._mark(client, article_id)
            assert expected in mark["aria-label"]

    def test_source_is_named_not_just_drawn(self, client, conn):
        article_id = self._article(conn, "ok", "browser")
        mark = self._mark(client, article_id)
        assert mark["aria-label"] == "Archived via your browser"
        assert mark["data-source"] == "browser"

    def test_a_thin_archive_reads_as_partial(self, client, conn):
        article_id = self._article(conn, "ok", "direct", "only got 28 words")
        mark = self._mark(client, article_id)
        assert mark["data-mark"] == "thin"
        assert "Partial archive" in mark["aria-label"]

    def test_the_reason_stays_available_on_hover(self, client, conn):
        article_id = self._article(conn, "failed", None, "archive.today said no")
        assert "archive.today said no" in self._mark(client, article_id)["title"]

    def test_the_article_page_marks_it_too(self, client, conn):
        article_id = self._article(conn, "ok", "browser")
        soup = BeautifulSoup(
            client.get(f"/a/{article_id}").get_data(as_text=True), "lxml")
        assert soup.select_one(".byline .mark")["data-source"] == "browser"


class TestReadingDates:
    def test_added_date_is_on_the_article_page(self, client, conn):
        article_id = db.insert_article(conn, url="https://e.example/d",
                                       original_url="https://e.example/d")
        db.update_article(conn, article_id, title="Piece")
        page = client.get(f"/a/{article_id}").get_data(as_text=True)
        assert "added" in page.lower()

    def test_read_date_appears_only_once_read(self, client, conn):
        article_id = db.insert_article(conn, url="https://e.example/d2",
                                       original_url="https://e.example/d2")
        db.update_article(conn, article_id, title="Piece")
        soup = BeautifulSoup(client.get(f"/a/{article_id}").get_data(as_text=True), "lxml")
        assert soup.select_one(".logged__read") is None, "unread piece claims a read date"

        db.set_status(conn, article_id, db.READ)
        soup = BeautifulSoup(client.get(f"/a/{article_id}").get_data(as_text=True), "lxml")
        assert "read" in soup.select_one(".logged__read").get_text().lower()

    def test_end_of_text_is_marked_for_auto_read(self, client, conn, app):
        article_id = db.insert_article(conn, url="https://e.example/d4",
                                       original_url="https://e.example/d4")
        db.update_article(conn, article_id, archive_status=db.OK)
        path = archiver.article_dir(app.config, article_id) / "readable.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<p>Body text.</p>", encoding="utf-8")

        soup = BeautifulSoup(client.get(f"/a/{article_id}").get_data(as_text=True), "lxml")
        reader = soup.select_one(".reader")
        assert reader["data-id"] == str(article_id)
        assert reader["data-status"] == "unread"
        # The sentinel comes after the text, so reaching it means reaching the end.
        assert soup.select_one(".prose").find_next_sibling()["data-role"] == "read-end"

    def test_no_auto_read_without_text(self, client, conn):
        article_id = db.insert_article(conn, url="https://e.example/d5",
                                       original_url="https://e.example/d5")
        soup = BeautifulSoup(client.get(f"/a/{article_id}").get_data(as_text=True), "lxml")
        assert soup.select_one('[data-role="read-end"]') is None

    def test_marking_unread_clears_the_read_date(self, client, conn):
        """set_status already nulls read_at; the page must not keep showing it."""
        article_id = db.insert_article(conn, url="https://e.example/d3",
                                       original_url="https://e.example/d3")
        db.set_status(conn, article_id, db.READ)
        assert db.get_article(conn, article_id)["read_at"]
        db.set_status(conn, article_id, db.UNREAD)
        assert db.get_article(conn, article_id)["read_at"] is None
        soup = BeautifulSoup(client.get(f"/a/{article_id}").get_data(as_text=True), "lxml")
        assert soup.select_one(".logged__read") is None


class TestDesktopApp:
    def test_ping_names_the_app(self, client):
        assert client.get("/ping").get_json() == {"app": "obomoboe"}

    def test_no_quit_unless_the_launcher_runs_the_server(self, client):
        assert client.post("/quit").status_code == 404
        assert 'action="/quit"' not in client.get("/").get_data(as_text=True)

    def test_quit_calls_the_launchers_shutdown(self, app, client):
        calls = []
        app.extensions["obomoboe.shutdown"] = lambda: calls.append(1)
        assert 'action="/quit"' in client.get("/").get_data(as_text=True)
        resp = client.post("/quit", headers={"Origin": "http://localhost"})
        assert resp.status_code == 200
        assert "has stopped" in resp.get_data(as_text=True)
        assert calls == [1]

    def test_another_site_cannot_quit_it(self, app, client):
        calls = []
        app.extensions["obomoboe.shutdown"] = lambda: calls.append(1)
        resp = client.post("/quit", headers={"Origin": "https://elsewhere.example"})
        assert resp.status_code == 403
        assert calls == []

    def test_bookmarklet_page_says_when_the_port_moved(self, client):
        assert "moved to" not in client.get("/bookmarklet").get_data(as_text=True)
        page = client.get("/bookmarklet?moved_from=5001").get_data(as_text=True)
        assert "5001" in page
        assert "moved to http://localhost" in page


class TestReadTally:
    def test_settings_counts_what_you_have_read(self, client, conn):
        for n in range(3):
            article_id = add(client, f"https://example.com/{n}").get_json()["id"]
            if n:
                db.set_status(conn, article_id, db.READ)
        soup = BeautifulSoup(client.get("/settings").get_data(as_text=True), "lxml")
        assert " ".join(soup.select_one(".read-tally").get_text().split()) == \
            "YOU HAVE READ 2 ARTICLES"

    def test_it_sits_above_the_heading(self, client):
        soup = BeautifulSoup(client.get("/settings").get_data(as_text=True), "lxml")
        assert soup.select_one(".read-tally").find_next_sibling("h1")

    def test_one_article_is_singular(self, client, conn):
        db.set_status(conn, add(client, "https://example.com/a").get_json()["id"],
                      db.READ)
        soup = BeautifulSoup(client.get("/settings").get_data(as_text=True), "lxml")
        assert " ".join(soup.select_one(".read-tally").get_text().split()) == \
            "YOU HAVE READ 1 ARTICLE"


class TestTheme:
    def test_pages_carry_the_default_palette(self, client):
        page = client.get("/").get_data(as_text=True)
        assert "--paper: #fcecd8;" in page
        assert ':root[data-theme="dark"]' in page, "CSS was HTML-escaped"
        assert "data-theme=" not in page.split("<head>")[0]

    def test_capture_window_is_themed_too(self, client):
        client.post("/settings/theme", data={"preset": "cyanotype"})
        page = client.get("/capture/window").get_data(as_text=True)
        assert theme.PRESETS["cyanotype"]["light"]["paper"] in page

    def test_choosing_a_preset(self, client, conn):
        client.post("/settings/theme", data={"preset": "plum"})
        assert db.get_settings(conn)["THEME"] == "plum"
        page = client.get("/").get_data(as_text=True)
        assert f"--accent: {theme.PRESETS['plum']['light']['accent']};" in page

    def test_an_unknown_preset_falls_back_to_the_default(self, client, conn):
        client.post("/settings/theme",
                    data={"preset": "</style><script>alert(1)</script>"})
        page = client.get("/").get_data(as_text=True)
        assert "alert(1)" not in page
        assert "--paper: #fcecd8;" in page

    def test_appearance_pins_light_or_dark(self, client):
        client.post("/settings/theme", data={"preset": "umber", "mode": "dark"})
        assert '<html lang="en" data-theme="dark">' in \
            client.get("/").get_data(as_text=True)
        client.post("/settings/theme",
                    data={"preset": "umber", "mode": "sideways"})
        assert "data-theme=" not in \
            client.get("/").get_data(as_text=True).split("<head>")[0]

    def test_every_preset_sets_every_color(self):
        for preset in theme.PRESETS.values():
            for mode in ("light", "dark"):
                assert set(preset[mode]) == set(theme.TOKEN_NAMES)
                assert all(re.fullmatch(r"#[0-9a-f]{6}", v)
                           for v in preset[mode].values())

    def test_settings_page_offers_presets_not_pickers(self, client):
        soup = BeautifulSoup(client.get("/settings").get_data(as_text=True), "lxml")
        names = [el.get_text() for el in soup.select(".preset__name")]
        assert names == [p["label"] for p in theme.PRESETS.values()]
        assert soup.select('input[type="color"]') == []
