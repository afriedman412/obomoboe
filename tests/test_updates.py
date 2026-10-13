import subprocess

import pytest
import requests
from bs4 import BeautifulSoup

from src import updates
from src.routes import UPDATES_KEY
from src.version import __version__


def release(version, assets=("obomoboe-mac-apple-silicon.zip",
                             "obomoboe-mac-intel.zip", "obomoboe-windows.zip")):
    return updates.Release(
        version=version,
        url=f"https://github.com/x/releases/tag/v{version}",
        assets={name: f"https://dl.example/v{version}/{name}" for name in assets})


def bump(version, by=1):
    major, minor, patch = updates.parse_version(version)
    return f"{major}.{minor + by}.0"


class TestVersions:
    def test_ordering(self):
        assert updates.is_newer("1.10.0", "1.9.3")
        assert updates.is_newer("v2.0.0", "1.99.0")
        assert not updates.is_newer("1.2.0", "1.2.0")
        assert not updates.is_newer("garbage", "1.0.0")

    def test_the_build_for_this_computer(self, monkeypatch):
        monkeypatch.setattr(updates.sys, "platform", "darwin")
        monkeypatch.setattr(updates.platform, "machine", lambda: "arm64")
        assert updates.asset_name() == "obomoboe-mac-apple-silicon.zip"
        monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
        assert updates.asset_name() == "obomoboe-mac-intel.zip"
        monkeypatch.setattr(updates.sys, "platform", "win32")
        assert updates.asset_name() == "obomoboe-windows.zip"


class TestChecking:
    def test_newer_release_is_offered(self):
        updater = updates.Updater(fetch=lambda: release(bump(__version__)))
        updater.check()
        assert updater.available.version == bump(__version__)

    def test_same_version_is_not(self):
        updater = updates.Updater(fetch=lambda: release(__version__))
        updater.check()
        assert updater.available is None

    def test_a_recent_answer_is_reused(self):
        calls = []

        def fetch():
            calls.append(1)
            return release(__version__)
        updater = updates.Updater(fetch=fetch)
        updater.check()
        updater.check()
        assert len(calls) == 1
        updater.check(force=True)
        assert len(calls) == 2

    def test_no_network_is_reported_not_raised(self):
        def offline():
            raise requests.ConnectionError("no route")
        updater = updates.Updater(fetch=offline)
        assert updater.check() is None
        assert "GitHub" in updater.status()["error"]

    def test_installing_needs_the_packaged_mac_app(self):
        """Run from source (as the tests are), there is nothing to swap."""
        updater = updates.Updater(fetch=lambda: release(bump(__version__)))
        updater.check()
        assert updates.install_blocker() == "not-packaged"
        assert updater.start_install() is False


class TestSwapScript:
    def test_new_app_replaces_the_old(self, tmp_path):
        bundle = tmp_path / "Applications" / "obomoboe.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "old").write_text("1")
        work = tmp_path / "work"
        new_app = work / "unpacked" / "obomoboe.app"
        (new_app / "Contents").mkdir(parents=True)
        (new_app / "Contents" / "new").write_text("2")
        finished = subprocess.Popen(["true"])
        finished.wait()

        script = updates.swap_script(finished.pid, bundle, new_app, work,
                                     relaunch="true")
        subprocess.run(["/bin/sh", "-c", script], check=True, timeout=10)
        assert (bundle / "Contents" / "new").exists()
        assert not (bundle / "Contents" / "old").exists()
        assert not work.exists()


@pytest.fixture()
def offered(app):
    updater = app.extensions[UPDATES_KEY]
    updater._fetch = lambda: release(bump(__version__))
    updater.check(force=True)
    return updater


class TestPages:
    def test_banner_when_a_newer_version_is_out(self, client, offered):
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        banner = soup.select_one("#update-banner")
        assert not banner.has_attr("hidden")
        assert bump(__version__) in banner.get_text()

    def test_no_banner_otherwise(self, app, client):
        app.extensions[UPDATES_KEY]._fetch = lambda: release(__version__)
        app.extensions[UPDATES_KEY].check(force=True)
        soup = BeautifulSoup(client.get("/").get_data(as_text=True), "lxml")
        assert soup.select_one("#update-banner").has_attr("hidden")

    def test_updates_page_offers_a_download_when_it_cannot_install(
            self, client, offered):
        page = client.get("/updates").get_data(as_text=True)
        assert f"obomoboe {bump(__version__)} is out" in page
        # Run from source: no in-place install, a download instead.
        assert "Update and" not in page and "Download" in page

    def test_no_banner_on_the_updates_page_itself(self, client, offered):
        soup = BeautifulSoup(client.get("/updates").get_data(as_text=True), "lxml")
        assert soup.select_one("#update-banner").has_attr("hidden")

    def test_up_to_date(self, app, client):
        app.extensions[UPDATES_KEY]._fetch = lambda: release(__version__)
        page = client.get("/updates?check=1").get_data(as_text=True)
        assert "You have the latest version" in page

    def test_another_site_cannot_start_an_install(self, client, offered):
        resp = client.post("/updates/install",
                           headers={"Origin": "https://elsewhere.example"})
        assert resp.status_code == 403

    def test_settings_shows_the_version(self, client):
        page = client.get("/settings").get_data(as_text=True)
        assert f"This is obomoboe {__version__}" in page
