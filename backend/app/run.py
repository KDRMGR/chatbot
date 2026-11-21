"""Production entry point:  python -m app.run   (this is what scripts\\start.bat runs).

It runs the web server in a loop so the dashboard's "Restart bot" works: the app asks the lifecycle to stop and says whether to
come back; settings (``.env``) are read again on every start. Running ``uvicorn app.main:create_app --factory`` directly still
works (tests and the chaos soak do that), but then the dashboard can stop the bot only, not restart it.
"""
from __future__ import annotations

import sys

import uvicorn

from .config import load_settings
from .main import create_app


class Lifecycle:
    """What the dashboard's Stop/Restart buttons talk to."""

    def __init__(self, server: uvicorn.Server) -> None:
        self._server = server
        self.restart_requested = False

    def request_stop(self, *, restart: bool) -> None:
        self.restart_requested = restart
        self._server.should_exit = True  # uvicorn finishes open requests, runs the app's shutdown, then returns


def main() -> int:
    while True:
        settings = load_settings()
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info"))
        lifecycle = Lifecycle(server)
        app.state.lifecycle = lifecycle
        server.run()
        if not lifecycle.restart_requested:
            return 0
        print("Restarting STEALTHBOT...", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
