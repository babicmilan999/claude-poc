"""Entry point: `uv run uvicorn main:app --port 8000`."""

import logging

from core.app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
