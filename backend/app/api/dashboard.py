"""Serves the control panel: three static files, no build step and no Node (Smart App Control may block a Node
toolchain here, and a control panel does not need one). The page contains no data and no secrets: everything it shows
comes from the protected API, using a key it gets by redeeming a one-time ticket (see api/security.TicketStore)."""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.routing import Route

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard"

# Only these files are ever served, so there is no path to traverse.
_FILES: dict[str, tuple[str, str]] = {
    "/dashboard": ("index.html", "text/html; charset=utf-8"),
    "/dashboard/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/dashboard/panels.js": ("panels.js", "text/javascript; charset=utf-8"),
    "/dashboard/style.css": ("style.css", "text/css; charset=utf-8"),
}

# No inline script or style, nothing from other sites, and the page may not be framed: a hostile web page cannot
# overlay or embed the control panel, and an injected chat message could not run code even if it reached the DOM.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _serve(path: str):  # noqa: ANN202
    filename, media_type = _FILES[path]

    async def handler(request: Request) -> Response:
        return FileResponse(STATIC_DIR / filename, media_type=media_type, headers=SECURITY_HEADERS)

    return handler


async def root(request: Request) -> Response:
    return RedirectResponse("/dashboard", status_code=307)


routes = [Route("/", root, methods=["GET"])] + [Route(p, _serve(p), methods=["GET"]) for p in _FILES]
