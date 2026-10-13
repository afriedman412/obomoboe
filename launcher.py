"""Desktop entry point -- what the packaged app runs.

Opening the app starts the server inside it and shows the list in a window
of its own: no address bar, no tabs, and a File menu to bring the window
back or quit. The server lives exactly as long as the app does, so the
bookmarklet works while the app is open and quitting really stops it.

On a Mac, closing the window only hides it -- the app stays in the Dock,
and the Dock icon or File > Show obomoboe brings it back. On Windows,
closing the window quits, as Windows apps do.

`--serve PORT` runs just the server with no window, which is what the
release smoke test starts. `--check-window` imports the window toolkit and
exits, so the smoke test can tell the build left nothing out.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
DEFAULT_PORT = 5001
PORTS_TO_TRY = 100


def main() -> None:
    args = sys.argv[1:]
    if "--serve" in args:
        serve(int(args[args.index("--serve") + 1]))
    elif "--check-window" in args:
        check_window()
    else:
        launch()


def launch() -> None:
    """Find the running server or start one, then show it.

    The bookmarklet has the address baked into it, so the port is kept
    between runs (see _saved_port) rather than chosen afresh each time.
    Only when that port has been taken by something else does it move, and
    then the bookmarklet page is opened to say it needs adding again.

    A server that is already up belongs to another copy of the app (or to
    an older version, which ran its server in the background): this one
    opens a window onto it rather than starting a second.
    """
    saved = _saved_port()
    port = saved or _wanted_port()
    state = _probe(port)

    moved_from = None
    server = None
    if state != "up":
        if state == "other" or not _port_free(port):
            moved_from = port if saved else None  # nothing to move on first run
            port = _free_port_after(port)
        try:
            server = _start_server_thread(port)
        except Exception as exc:  # noqa: BLE001 -- shown to the person instead
            _fail(f"obomoboe could not start: {exc}\n\n" + _log_tail())

    start = _url(port) + (f"bookmarklet?moved_from={moved_from}"
                          if moved_from else "")
    try:
        import webview  # noqa: F401
    except ImportError:
        # Run from a checkout without the window toolkit: use the browser,
        # and keep serving until interrupted.
        webbrowser.open(start)
        if server is not None:
            server.done.wait()
        return
    _run_window(port, start, server)


def _run_window(port: int, start: str, server: Any) -> None:
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    base = _url(port)
    quitting = threading.Event()

    def show() -> None:
        window.show()

    def reading_list() -> None:
        window.show()
        window.load_url(base)

    def quit_app() -> None:
        quitting.set()
        if server is not None:
            server.stop()
        else:
            _post_quit(port)
        window.destroy()

    window = webview.create_window(
        "obomoboe", start, width=1100, height=840, min_size=(420, 480),
        text_select=True)

    def on_closing() -> bool:
        if quitting.is_set() or sys.platform != "darwin":
            return True
        # Hidden, not closed: a Mac app outlives its window.
        threading.Thread(target=window.hide, daemon=True).start()
        return False

    window.events.closing += on_closing

    if server is not None:
        # The page's own quit button ends the app, not just the server.
        server.on_quit = quit_app
        server.check_for_updates()

    def check_for_updates() -> None:
        window.show()
        window.load_url(base + "updates?check=1")

    menu = [Menu("File", [
        MenuAction("Show obomoboe", show),
        MenuAction("Go to reading list", reading_list),
        MenuSeparator(),
        MenuAction("Set up the bookmarklet in your browser\u2026",
                   lambda: webbrowser.open(base + "bookmarklet/install")),
        MenuAction("Open this page in your browser",
                   lambda: webbrowser.open(window.get_current_url() or base)),
        MenuSeparator(),
        MenuAction("Check for updates\u2026", check_for_updates),
        MenuSeparator(),
        MenuAction("Quit obomoboe", quit_app),
    ])]

    if sys.platform == "darwin":
        _mac_app_hooks(show, quitting)

    webview.start(menu=menu, private_mode=False,
                  storage_path=str(_data_dir() / "window"))

    # The window is gone, however that happened: take the server with it.
    if server is not None:
        server.stop()
        server.done.wait(10)


def _mac_app_hooks(show: Any, quitting: threading.Event) -> None:
    """Teach pywebview's Cocoa app two things a Mac app does.

    Clicking the Dock icon brings back a hidden window. And Quit really
    quits: pywebview asks each window whether it may close before the app
    terminates, and ours says no so the close button can hide it -- so mark
    the app as quitting first.
    """
    import objc
    from webview.platforms.cocoa import BrowserView

    delegate = BrowserView.AppDelegate
    should_terminate = delegate.applicationShouldTerminate_

    def terminate(self: Any, app: Any) -> Any:
        quitting.set()
        return should_terminate(self, app)

    def reopen(self: Any, app: Any, has_visible_windows: bool) -> bool:
        show()
        return True

    objc.classAddMethods(delegate, [
        objc.selector(terminate, selector=b"applicationShouldTerminate:",
                      signature=should_terminate.signature),
        objc.selector(reopen,
                      selector=b"applicationShouldHandleReopen:hasVisibleWindows:",
                      signature=b"Z@:@Z"),
    ])


def check_window() -> None:
    """Import what the window needs on this platform, then exit. A frozen
    build that left part of it out fails here instead of on someone's
    desktop."""
    import webview  # noqa: F401

    if sys.platform == "darwin":
        import webview.platforms.cocoa  # noqa: F401
    elif sys.platform == "win32":
        import webview.platforms.winforms  # noqa: F401
    print("window support ok")


def _post_quit(port: int) -> None:
    request = urllib.request.Request(
        _url(port) + "quit", method="POST",
        headers={"Origin": _url(port).rstrip("/")})
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except (urllib.error.URLError, OSError):
        pass


def serve(port: int) -> None:
    """Run just the server, in this process, until /quit."""
    server = _start_server_thread(port)
    server.done.wait()


class _Server:
    """The app's server on a background thread.

    on_quit is what the page's quit button does: by default it stops the
    server; the window replaces it with quitting the whole app.
    """

    def __init__(self, port: int) -> None:
        data_dir = _data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        log = data_dir / "obomoboe.log"
        # A windowed build has nowhere to print to; on Windows it has no
        # stdout at all.
        sys.stdout = sys.stderr = open(log, "w", buffering=1, encoding="utf-8")

        from werkzeug.serving import make_server

        from src import create_app
        from src.routes import SHUTDOWN_KEY, UPDATES_KEY

        app = create_app()
        self.app = app
        # Installing an update ends with this copy quitting, the same way
        # the page's quit button does.
        app.extensions[UPDATES_KEY].quit_app = lambda: self.on_quit()
        self._server = make_server(HOST, port, app, threaded=True)
        # Keep track of request threads so server_close() waits for them;
        # otherwise the process can exit under the /quit request and the
        # page saying it stopped never arrives.
        self._server.daemon_threads = False
        # Written once the port is ours, so the next launch looks here.
        (data_dir / "port").write_text(str(port))
        self.done = threading.Event()
        self.on_quit = self.stop
        app.extensions[SHUTDOWN_KEY] = lambda: threading.Thread(
            target=self.on_quit, daemon=True).start()
        print(f"obomoboe serving on {_url(port)} -- data in {data_dir}")

    def run(self) -> None:
        try:
            self._server.serve_forever()
        finally:
            self._server.server_close()
            self.done.set()

    def check_for_updates(self) -> None:
        """Ask GitHub about a newer version in the background, unless
        that is switched off in settings."""
        from src import db
        from src.routes import UPDATES_KEY

        with self.app.app_context():
            settings = db.get_settings(db.get_db())
            db.close_db()
        if settings.get("CHECK_UPDATES", self.app.config["CHECK_UPDATES"]):
            self.app.extensions[UPDATES_KEY].check_in_background()

    def stop(self) -> None:
        if not self.done.is_set():
            self._server.shutdown()


def _start_server_thread(port: int) -> _Server:
    server = _Server(port)
    threading.Thread(target=server.run, name="obomoboe-server",
                     daemon=True).start()
    return server


def _url(port: int) -> str:
    return f"http://{HOST}:{port}/"


def _wanted_port() -> int:
    try:
        return int(os.environ["OBOMOBOE_PORT"])
    except (KeyError, ValueError):
        return DEFAULT_PORT


def _saved_port() -> int | None:
    """The port the server last ran on. OBOMOBOE_PORT, if set, wins."""
    if "OBOMOBOE_PORT" in os.environ:
        return _wanted_port()
    try:
        return int((_data_dir() / "port").read_text().strip())
    except (OSError, ValueError):
        return None


def _probe(port: int) -> str:
    """'up' if obomoboe answers on the port, 'down' if nothing does,
    'other' if something else does."""
    try:
        with urllib.request.urlopen(_url(port) + "ping", timeout=2) as response:
            body = json.load(response)
    except urllib.error.HTTPError:
        return "other"
    except (urllib.error.URLError, OSError):
        return "down"
    except ValueError:
        return "other"
    return "up" if isinstance(body, dict) and body.get("app") == "obomoboe" else "other"


def _port_free(port: int) -> bool:
    # Without SO_REUSEADDR, so a listener on all interfaces counts as taken
    # even though the server itself could bind 127.0.0.1 alongside it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def _free_port_after(port: int) -> int:
    for candidate in range(port + 1, port + 1 + PORTS_TO_TRY):
        if _port_free(candidate):
            return candidate
    _fail(f"Ports {port} to {port + PORTS_TO_TRY} are all in use, so obomoboe "
          "has nowhere to run.")
    raise AssertionError  # unreachable; _fail exits


def _data_dir() -> Path:
    from src.config import build_config

    return Path(build_config()["DATA_DIR"])


def _log_tail(lines: int = 12) -> str:
    try:
        text = (_data_dir() / "obomoboe.log").read_text(encoding="utf-8",
                                                        errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _fail(message: str) -> None:
    """Say what went wrong somewhere a person will see it. Opened from
    Finder or Explorer, there is no terminal to print to."""
    print(message, file=sys.stderr)
    try:
        if sys.platform == "darwin":
            script = ('display dialog item 1 of argv with title "obomoboe" '
                      'buttons {"OK"} default button 1 with icon stop')
            subprocess.run(["osascript", "-e", "on run argv", "-e", script,
                            "-e", "end run", message], check=False)
        elif sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "obomoboe", 0x10)
    except Exception:
        pass
    sys.exit(1)


if __name__ == "__main__":
    main()
