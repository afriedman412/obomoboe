"""Start a built app's server, check it answers, and quit it.

    python scripts/smoke_test.py dist/obomoboe.app/Contents/MacOS/obomoboe

Run by the release workflow on each platform before anything is uploaded.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 5099
BASE = f"http://127.0.0.1:{PORT}"


def main(executable: str) -> None:
    with tempfile.TemporaryDirectory() as data_dir:
        env = dict(os.environ, OBOMOBOE_DATA_DIR=data_dir)
        server = subprocess.Popen([executable, "--serve", str(PORT)], env=env)
        try:
            body = _wait_for_ping(server)
            assert body == {"app": "obomoboe"}, body
            with urllib.request.urlopen(BASE + "/", timeout=10) as response:
                assert b"obomoboe" in response.read()
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/quit", method="POST", headers={"Origin": BASE}), timeout=10)
            server.wait(timeout=30)
        except BaseException:
            server.kill()
            log = os.path.join(data_dir, "obomoboe.log")
            if os.path.exists(log):
                print(open(log, encoding="utf-8", errors="replace").read())
            raise
    print("smoke test passed")


def _wait_for_ping(server: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"server exited with {server.returncode}")
        try:
            with urllib.request.urlopen(BASE + "/ping", timeout=2) as response:
                return json.load(response)
        except OSError:
            time.sleep(0.5)
    raise TimeoutError("server never answered /ping")


if __name__ == "__main__":
    main(sys.argv[1])
