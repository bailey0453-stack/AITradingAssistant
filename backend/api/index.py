"""Vercel serverless entrypoint for the FastAPI backend.

Vercel's `@vercel/python` runtime serves the module-level ASGI ``app``. The
project's Root Directory must be set to ``backend`` so that ``app`` is importable
as a top-level package (see backend/README.md → "Deploy to Vercel").

The serverless runtime may not execute ASGI lifespan startup, so we create the
database tables explicitly here (idempotent). On Vercel the DB lives under
``/tmp`` (see app/database.py).
"""

from app.database import init_db
from app.main import app  # noqa: F401  (exported for the Vercel Python runtime)

init_db()
