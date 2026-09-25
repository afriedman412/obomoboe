from src import db


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
