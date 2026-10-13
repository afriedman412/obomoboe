import json

import pytest
from bs4 import BeautifulSoup

from src import archiver, images

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeResponse:
    def __init__(self, status=200, content_type="image/png", body=PNG):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self._body = body

    def iter_content(self, size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def fetched(monkeypatch):
    """Every image URL requested; anything with 'gone' in it is a 404."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if "gone" in url:
            return FakeResponse(status=404, content_type="text/html")
        return FakeResponse()

    monkeypatch.setattr(images.requests, "get", fake_get)
    return calls


@pytest.fixture()
def config(tmp_path):
    return {"USER_AGENT": "test", "MAX_IMAGE_BYTES": 1_000_000,
            "MAX_IMAGES": 25, "MAX_ORIGINAL_IMAGES": 60,
            "ARCHIVE_IMAGES": True, "ARCHIVE_DIR": tmp_path / "archives"}


def srcs(html):
    return [img.get("src") for img in BeautifulSoup(html, "lxml").find_all("img")]


class TestFindingThePicture:
    def test_base_tag_wins_over_the_page_address(self):
        """archive.today snapshots set <base> to the archive's image host;
        resolving against the publisher's address lost every picture."""
        html = "<html><head><base href='//d9g.archive.ph'></head></html>"
        assert images.page_base(html, "https://www.vulture.com/a") == \
            "https://d9g.archive.ph"
        assert images.page_base("<p>no base</p>", "https://e.example/a") == \
            "https://e.example/a"

    def test_lazy_attribute_beats_the_placeholder(self):
        img = BeautifulSoup("<img src='/blank.gif' data-src='/real.jpg'>",
                            "lxml").img
        assert images.image_source(img, "https://e.example/a") == \
            "https://e.example/real.jpg"

    def test_data_uri_placeholder_falls_through_to_srcset(self):
        img = BeautifulSoup("<img src='data:image/gif;base64,R0lG' "
                            "srcset='/s.jpg 300w, /l.jpg 1200w'>", "lxml").img
        assert images.image_source(img, "https://e.example/") == \
            "https://e.example/l.jpg"

    def test_no_real_picture(self):
        img = BeautifulSoup("<img src='data:image/gif;base64,R0lG'>", "lxml").img
        assert images.image_source(img, "https://e.example/") is None


class TestLocalizing:
    def test_pictures_are_saved_and_pointed_at(self, config, tmp_path, fetched):
        cache = images.ImageCache(config, tmp_path, "https://e.example/a")
        html = ("<p>x</p><img src='/one.png' srcset='/one-2x.png 2x'>"
                "<img src='https://cdn.example/two.png'>")
        out = images.localize(html, "https://e.example/a", cache, limit=25)
        assert all(src.startswith("assets/") for src in srcs(out))
        assert "srcset" not in out
        assert len(list((tmp_path / "assets").glob("*.png"))) == 2

    def test_a_picture_that_cannot_be_saved_keeps_its_address(
            self, config, tmp_path, fetched):
        cache = images.ImageCache(config, tmp_path, "https://e.example/a")
        out = images.localize("<img src='/gone.png'>", "https://e.example/a",
                              cache, limit=25)
        assert srcs(out) == ["https://e.example/gone.png"]

    def test_limit_is_respected(self, config, tmp_path, fetched):
        cache = images.ImageCache(config, tmp_path, "https://e.example/a")
        html = "".join(f"<img src='/{n}.png'>" for n in range(5))
        images.localize(html, "https://e.example/", cache, limit=2)
        assert len(fetched) == 2

    def test_a_saved_picture_is_not_fetched_again(self, config, tmp_path,
                                                  fetched):
        html = "<img src='/one.png'>"
        images.localize(html, "https://e.example/",
                        images.ImageCache(config, tmp_path, ""), limit=25)
        images.localize(html, "https://e.example/",
                        images.ImageCache(config, tmp_path, ""), limit=25)
        assert len(fetched) == 1

    def test_full_page_loses_its_base_and_picture_sources(
            self, config, tmp_path, fetched):
        page = ("<html><head><base href='//img.archive.ph'></head><body>"
                "<picture><source srcset='/a.webp'><img src='/a.png'></picture>"
                "</body></html>")
        cache = images.ImageCache(config, tmp_path, "")
        out = images.localize(page, images.page_base(page, "https://pub.example/"),
                              cache, limit=60, fragment=False)
        assert "<base" not in out and "<source" not in out
        assert fetched == ["https://img.archive.ph/a.png"]
        assert srcs(out)[0].startswith("assets/")


class TestFillingInOldArticles:
    def _old_article(self, config, article_id=7):
        target = archiver.article_dir(config, article_id)
        target.mkdir(parents=True)
        (target / "readable.html").write_text(
            "<p>text</p><img src='https://e.example/pic.png'>")
        (target / "original.html").write_text(
            "<html><body><img src='/pic.png'><img data-src='/lazy.png' "
            "src='data:image/gif;base64,R0lG'></body></html>")
        (target / "meta.json").write_text(json.dumps({"url": "https://e.example/a"}))
        return target

    def test_pictures_are_saved_and_the_text_left_alone(self, config, fetched):
        target = self._old_article(config)
        assert archiver.fill_in_images(config, 7) is True
        readable = (target / "readable.html").read_text()
        assert "<p>text</p>" in readable
        assert srcs(readable)[0].startswith("assets/")
        assert all(src.startswith("assets/")
                   for src in srcs((target / "original.html").read_text()))
        # The same picture in both pages was fetched once.
        assert sorted(fetched) == ["https://e.example/lazy.png",
                                   "https://e.example/pic.png"]

    def test_each_article_is_filled_in_once(self, config, fetched):
        self._old_article(config)
        archiver.fill_in_images(config, 7)
        fetched.clear()
        assert archiver.fill_in_images(config, 7) is False
        assert fetched == []
