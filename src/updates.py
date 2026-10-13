"""Finding and installing newer versions.

Releases are the GitHub releases of the obomoboe repository, each carrying a
zip per platform. Checking asks GitHub for the latest one; nothing about the
library or its articles is sent.

Installing in place is done for the Mac app only: download the build for
this Mac, check Apple's signature and that it is ours, then hand over to a
small script that waits for this app to quit, swaps the new one in and
opens it. On Windows a running program cannot replace its own folder that
simply, so there the zip is offered as a download instead.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .version import __version__

log = logging.getLogger(__name__)

REPO = "afriedman412/obomoboe"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

# Apple Developer team the release builds are signed by. A download signed
# by anyone else is not installed.
TEAM_ID = "ZF5X258ZFY"

# How long a check is trusted before asking GitHub again.
CHECK_EVERY = 6 * 60 * 60


@dataclass
class Release:
    version: str
    url: str
    notes: str = ""
    assets: dict[str, str] = field(default_factory=dict)


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.12.0' -> (1, 12, 0). Anything unparseable sorts as oldest."""
    numbers = re.findall(r"\d+", (text or "").split("-")[0])
    return tuple(int(n) for n in numbers[:3]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def asset_name() -> str | None:
    """The zip built for this computer, or None if there is none."""
    if sys.platform == "darwin":
        arch = "apple-silicon" if platform.machine() == "arm64" else "intel"
        return f"obomoboe-mac-{arch}.zip"
    if sys.platform == "win32":
        return "obomoboe-windows.zip"
    return None


def fetch_latest(timeout: float = 10) -> Release:
    resp = requests.get(LATEST_API, timeout=timeout,
                        headers={"Accept": "application/vnd.github+json",
                                 "User-Agent": f"obomoboe/{__version__}"})
    resp.raise_for_status()
    data = resp.json()
    return Release(
        version=str(data.get("tag_name", "")).lstrip("v"),
        url=str(data.get("html_url") or RELEASES_PAGE),
        notes=str(data.get("body") or ""),
        assets={a["name"]: a["browser_download_url"]
                for a in data.get("assets", [])
                if a.get("name") and a.get("browser_download_url")},
    )


def app_bundle() -> Path | None:
    """The .app this is running from, when it is the packaged Mac app."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    bundle = Path(sys.executable).resolve().parents[2]
    return bundle if bundle.suffix == ".app" else None


def install_blocker() -> str | None:
    """Why this copy cannot update itself in place, or None if it can."""
    bundle = app_bundle()
    if bundle is None:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            return "windows"
        return "not-packaged"
    # Opened straight from Downloads, macOS runs the app from a read-only
    # copy at a random path; there is nothing there to replace.
    if "/AppTranslocation/" in str(bundle):
        return "translocated"
    if not os.access(bundle.parent, os.W_OK):
        return "not-writable"
    return None


BLOCKER_MESSAGES = {
    "windows": "Download the new version and replace the obomoboe folder "
               "with it.",
    "not-packaged": "This copy runs from source; update it with git.",
    "translocated": "Move obomoboe into your Applications folder, open it "
                    "from there, and try again.",
    "not-writable": "obomoboe's folder can't be written to. Move it into "
                    "your Applications folder and try again.",
}


class Updater:
    """What the app knows about updates, and the one install it may run.

    `quit_app` is how the installer gets this copy out of the way; the
    launcher provides it.
    """

    def __init__(self, quit_app: Callable[[], None] | None = None,
                 fetch: Callable[[], Release] = fetch_latest) -> None:
        self.quit_app = quit_app
        self._fetch = fetch
        self._lock = threading.Lock()
        self.latest: Release | None = None
        self.checked_at = 0.0
        self.error: str | None = None
        # idle, downloading, installing, failed
        self.phase = "idle"
        self.detail = ""

    # -- checking ----------------------------------------------------------

    def check(self, force: bool = False) -> Release | None:
        with self._lock:
            fresh = time.time() - self.checked_at < CHECK_EVERY
            if fresh and not force:
                return self.latest
        try:
            latest = self._fetch()
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.info("update check failed: %s", exc)
            with self._lock:
                self.error = "couldn't reach GitHub to check"
                self.checked_at = time.time()
            return self.latest
        with self._lock:
            self.latest, self.error = latest, None
            self.checked_at = time.time()
        return latest

    def check_in_background(self) -> None:
        threading.Thread(target=self.check, daemon=True,
                         name="update-check").start()

    @property
    def available(self) -> Release | None:
        latest = self.latest
        return latest if latest and is_newer(latest.version) else None

    def download_url(self) -> str:
        latest, name = self.latest, asset_name()
        if latest and name and name in latest.assets:
            return latest.assets[name]
        return latest.url if latest else RELEASES_PAGE

    def status(self) -> dict[str, Any]:
        available = self.available
        return {
            "current": __version__,
            "latest": self.latest.version if self.latest else None,
            "available": available.version if available else None,
            "checked": bool(self.checked_at),
            "error": self.error,
            "phase": self.phase,
            "detail": self.detail,
        }

    # -- installing --------------------------------------------------------

    def start_install(self) -> bool:
        """Begin installing the available update. False if one is already
        under way, there is nothing to install, or this copy can't."""
        with self._lock:
            if self.phase in ("downloading", "installing"):
                return False
            release = self.available
            if release is None or install_blocker() is not None:
                return False
            self.phase, self.detail = "downloading", ""
        threading.Thread(target=self._install, args=(release,), daemon=True,
                         name="update-install").start()
        return True

    def _fail(self, message: str) -> None:
        log.warning("update failed: %s", message)
        with self._lock:
            self.phase, self.detail = "failed", message

    def _install(self, release: Release) -> None:
        bundle = app_bundle()
        name = asset_name()
        url = release.assets.get(name or "")
        if bundle is None or url is None:
            self._fail("this release has no build for this Mac")
            return

        work = Path(tempfile.mkdtemp(prefix="obomoboe-update-"))
        try:
            archive = work / name
            with requests.get(url, stream=True, timeout=30,
                              headers={"User-Agent": f"obomoboe/{__version__}"}) as resp:
                resp.raise_for_status()
                with open(archive, "wb") as out:
                    for chunk in resp.iter_content(256 * 1024):
                        out.write(chunk)

            unpacked = work / "unpacked"
            subprocess.run(["ditto", "-x", "-k", str(archive), str(unpacked)],
                           check=True, capture_output=True)
            new_app = unpacked / "obomoboe.app"
            if not new_app.is_dir():
                self._fail("the download didn't contain the app")
                return
            problem = verify_signature(new_app)
            if problem:
                self._fail(problem)
                return
        except (requests.RequestException, OSError,
                subprocess.CalledProcessError) as exc:
            self._fail(f"couldn't download the update ({exc})")
            shutil.rmtree(work, ignore_errors=True)
            return

        with self._lock:
            self.phase, self.detail = "installing", release.version
        swap_and_relaunch(bundle, new_app, work)
        # Leave time for the page to learn it is restarting.
        time.sleep(1.5)
        if self.quit_app is not None:
            self.quit_app()


def verify_signature(app: Path) -> str | None:
    """None if the app is intact, notarized and signed by our team."""
    checks = [
        (["codesign", "--verify", "--deep", "--strict", str(app)],
         "the download's signature is broken"),
        (["spctl", "--assess", "--type", "execute", str(app)],
         "macOS doesn't accept the download"),
    ]
    for command, message in checks:
        if subprocess.run(command, capture_output=True).returncode != 0:
            return message
    shown = subprocess.run(["codesign", "-dv", str(app)], capture_output=True,
                           text=True)
    if f"TeamIdentifier={TEAM_ID}" not in (shown.stderr + shown.stdout):
        return "the download isn't signed by obomoboe's developer"
    return None


def swap_script(pid: int, bundle: Path, new_app: Path, work: Path,
                relaunch: str = "open") -> str:
    """Shell that waits for `pid` to exit, swaps `new_app` in at `bundle`,
    and reopens it.

    The old app is moved aside, not deleted, until the new one is in place,
    so a failed move leaves it where it was.
    """
    old = work / "previous.app"
    q = shlex.quote
    return f"""
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
if mv {q(str(bundle))} {q(str(old))}; then
  if mv {q(str(new_app))} {q(str(bundle))}; then
    rm -rf {q(str(work))}
  else
    mv {q(str(old))} {q(str(bundle))}
  fi
fi
{relaunch} {q(str(bundle))}
"""


def swap_and_relaunch(bundle: Path, new_app: Path, work: Path) -> None:
    """Start the swap in the background; it runs on after this app quits."""
    subprocess.Popen(["/bin/sh", "-c",
                      swap_script(os.getpid(), bundle, new_app, work)],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
