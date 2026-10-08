"""Desktop entry point -- what the packaged app runs.

Opening the app makes sure a server is running, opens the list in the
browser, and exits. The server is this same program started again with
--serve, detached, so it keeps running until you press quit in the page.

The split matters on a Mac: opening an app that is still running only brings
it to the front, which for an app with no window would do nothing at all.
Because the launcher has always exited by then, opening the app a second time
runs it again, it finds the server already up, and the browser opens.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 5001
PORTS_TO_TRY = 100
STARTUP_TIMEOUT = 90  # first launch of a fresh download is slow on macOS


def main() -> None:
    if "--serve" in sys.argv[1:]:
        serve(int(sys.argv[sys.argv.index("--serve") + 1]))
    else:
        launch()


def launch() -> None:
    """Find the running server or start one, then open it.

    The bookmarklet has the address baked into it, so the port is kept
    between runs (see _saved_port) rather than chosen afresh each time.
    Only when that port has been taken by something else does it move, and
    then the bookmarklet page is opened to say it needs adding again.
    """
    saved = _saved_port()
    port = saved or _wanted_port()
    state = _probe(port)
    if state == "up":
        webbrowser.open(_url(port))
        return

    moved_from = None
    if state == "other" or not _port_free(port):
        moved_from = port if saved else None  # nothing to move on first run
        port = _free_port_after(port)

    server = _start_server(port)
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while _probe(port) != "up":
        if server.poll() is not None:
            _fail("obomoboe could not start.\n\n" + _log_tail())
        if time.monotonic() > deadline:
            _fail("obomoboe is taking too long to start.\n\n"
                  f"The log is at {_data_dir() / 'obomoboe.log'}")
        time.sleep(0.25)

    if moved_from:
        webbrowser.open(_url(port) + f"bookmarklet?moved_from={moved_from}")
    else:
        webbrowser.open(_url(port))


def serve(port: int) -> None:
    """Run the server in this process until /quit."""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log = data_dir / "obomoboe.log"
    # A detached process has nowhere to print to; a windowed build on
    # Windows has no stdout at all.
    sys.stdout = sys.stderr = open(log, "w", buffering=1, encoding="utf-8")

    from werkzeug.serving import make_server

    from src import create_app
    from src.routes import SHUTDOWN_KEY

    app = create_app()
    server = make_server(HOST, port, app, threaded=True)
    # Written once the port is ours, so the next launch looks here.
    (data_dir / "port").write_text(str(port))
    # Keep track of request threads so server_close() waits for them;
    # otherwise the process exits under the /quit request and the page
    # saying it stopped never reaches the browser.
    server.daemon_threads = False
    app.extensions[SHUTDOWN_KEY] = lambda: threading.Thread(
        target=server.shutdown, daemon=True).start()
    print(f"obomoboe serving on {_url(port)} -- data in {data_dir}")
    server.serve_forever()
    server.server_close()


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


def _start_server(port: int) -> subprocess.Popen:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--serve", str(port)]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)]
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                     "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        options["creationflags"] = (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


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
