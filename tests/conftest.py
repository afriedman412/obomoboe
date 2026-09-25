import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import create_app  # noqa: E402
from src import db  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    application = create_app({
        "DATA_DIR": tmp_path,
        "DB_PATH": tmp_path / "test.db",
        "ARCHIVE_DIR": tmp_path / "archives",
        "START_WORKERS": False,
        "ARCHIVE_IMAGES": False,
        "ARCHIVE_PH_ENABLED": False,
    })
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def conn(app):
    connection = db.connect(app.config["DB_PATH"])
    yield connection
    connection.close()
