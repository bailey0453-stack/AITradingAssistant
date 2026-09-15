"""API routers."""

# Import for side effects: this module attaches the human-triggered market-data
# conformance endpoints to the existing fix_admin router before main includes it.
from app.routers import fix_md_conformance as _fix_md_conformance  # noqa: F401,E402
