"""Entrypoint. `python app.py` for dev, `gunicorn app:app` for anything else."""
from src import create_app

app = create_app()

if __name__ == "__main__":
    import os

    app.run(
        host=os.environ.get("OBOMOBOE_HOST", "127.0.0.1"),
        port=int(os.environ.get("OBOMOBOE_PORT", "5001")),
        debug=True,
        # The reloader forks a second process, which would mean two archive
        # worker pools racing on the same rows.
        use_reloader=False,
    )
