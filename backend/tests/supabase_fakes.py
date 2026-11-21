"""A fake Supabase (Auth password/refresh grants + the few PostgREST calls the relay makes), as an httpx MockTransport handler.
It enforces what the real project enforces for the relay: the public apikey, a valid bearer token, and the 'bot' role."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

PUBLIC_KEY = "sb_publishable_testkey_0123456789abcdef"
BOT_EMAIL, BOT_PASSWORD = "bot@example.test", "correct horse battery staple"
BOT_ID, ADMIN_ID, STREAMER_ID, STRANGER_ID = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3, 4))


def iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


class FakeSupabase:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.users = {BOT_EMAIL: {"password": BOT_PASSWORD, "id": BOT_ID}}
        self.profiles: dict[str, str] = {BOT_ID: "bot", ADMIN_ID: "admin", STREAMER_ID: "streamer"}  # STRANGER_ID has no profile
        self.commands: list[dict[str, Any]] = []
        self.status_row: dict[str, Any] | None = None
        self.valid_tokens: dict[str, str] = {}  # access token -> user id
        self.refresh_tokens: dict[str, str] = {}
        self.refresh_works = True
        self.down: Exception | None = None  # raise this for every request (an outage)
        self.reject_rest_with: int | None = None
        self.block_claims: set[int] = set()  # commands another taker wins
        self.logins = 0
        self.refreshes = 0
        self._n = 0
        self._cid = 0

    # ------------------------------------------------------------------ test helpers
    def add_command(self, kind: str, payload: dict[str, Any] | None = None, *, by: str = ADMIN_ID, age_s: float = 0.0,
                    status: str = "pending") -> int:
        self._cid += 1
        created = datetime.now(timezone.utc).timestamp() - age_s
        self.commands.append({"id": self._cid, "created_at": iso(datetime.fromtimestamp(created, timezone.utc)), "created_by": by,
                              "kind": kind, "payload": payload or {}, "status": status, "result": None, "handled_at": None})
        return self._cid

    def command(self, cid: int) -> dict[str, Any]:
        return next(c for c in self.commands if c["id"] == cid)

    def expire_tokens(self) -> None:
        self.valid_tokens.clear()

    def calls(self, method: str, path_part: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and path_part in r.url.path]

    # ------------------------------------------------------------------ the HTTP surface
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise self.down
        path = request.url.path
        if request.headers.get("apikey") != PUBLIC_KEY:
            return httpx.Response(401, json={"message": "Invalid API key"})
        if path == "/auth/v1/token" and request.method == "POST":
            return self._auth(request)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        user = self.valid_tokens.get(token)
        if user is None:
            return httpx.Response(401, json={"message": "JWT expired"})
        if self.reject_rest_with:
            return httpx.Response(self.reject_rest_with, json={"message": "forced failure"})
        if path.startswith("/rest/v1/"):
            return self._rest(request, path.removeprefix("/rest/v1/"), user)
        return httpx.Response(404, json={"message": "unknown"})

    def _auth(self, request: httpx.Request) -> httpx.Response:
        grant = request.url.params.get("grant_type")
        body = json.loads(request.content)
        if grant == "password":
            account = self.users.get(body.get("email", ""))
            if not account or account["password"] != body.get("password"):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid login credentials"})
            self.logins += 1
            user_id = account["id"]
        elif grant == "refresh_token":
            user_id = self.refresh_tokens.get(body.get("refresh_token", ""))
            if user_id is None or not self.refresh_works:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid Refresh Token"})
            self.refreshes += 1
        else:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        self._n += 1
        access, refresh = f"access-{self._n}", f"refresh-{self._n}"
        self.valid_tokens[access], self.refresh_tokens[refresh] = user_id, user_id
        return httpx.Response(200, json={"access_token": access, "refresh_token": refresh, "expires_in": 3600, "token_type": "bearer"})

    def _filters(self, request: httpx.Request) -> list[tuple[str, str, str]]:
        out = []
        for key, value in request.url.params.multi_items():
            if key in ("select", "order", "limit", "on_conflict"):
                continue
            op, _, rest = value.partition(".")
            out.append((key, op, rest))
        return out

    @staticmethod
    def _match(row: dict[str, Any], filters: list[tuple[str, str, str]]) -> bool:
        for col, op, val in filters:
            have = row.get(col)
            if op == "eq" and str(have) != val:
                return False
            if op == "lt" and not (have is not None and str(have) < val):
                return False
        return True

    def _rest(self, request: httpx.Request, table: str, user: str) -> httpx.Response:
        role = self.profiles.get(user)
        if role != "bot":  # what RLS would do to anyone else on these tables
            return httpx.Response(403, json={"message": "permission denied for this role"})
        filters = self._filters(request)
        prefer = request.headers.get("prefer", "")
        if table == "profiles" and request.method == "GET":
            rows = [{"user_id": u, "role": r} for u, r in self.profiles.items()]
            return httpx.Response(200, json=[{"role": r["role"]} for r in rows if self._match(r, filters)])
        if table == "bot_commands":
            if request.method == "GET":
                rows = [c for c in self.commands if self._match(c, filters)]
                rows.sort(key=lambda c: c["id"])
                limit = int(request.url.params.get("limit", "1000"))
                return httpx.Response(200, json=[{k: c[k] for k in ("id", "created_at", "created_by", "kind", "payload")} for c in rows[:limit]])
            if request.method == "PATCH":
                patch = json.loads(request.content)
                if not set(patch) <= {"status", "result", "handled_at"}:  # the column grants in schema.sql
                    return httpx.Response(403, json={"message": "permission denied for column"})
                rows = [c for c in self.commands if self._match(c, filters)]
                if any(c["id"] in self.block_claims and patch.get("status") == "running" for c in rows):
                    rows = []  # somebody else claimed it first
                for c in rows:
                    c.update(patch)
                return httpx.Response(200 if "representation" in prefer else 204, json=rows) if "representation" in prefer else httpx.Response(204)
            if request.method == "DELETE":
                before = len(self.commands)
                self.commands = [c for c in self.commands if not self._match(c, filters)]
                return httpx.Response(204, headers={"x-deleted": str(before - len(self.commands))})
        if table == "bot_status" and request.method == "POST":
            row = json.loads(request.content)
            if row.get("id") != 1:
                return httpx.Response(400, json={"message": "id must be 1"})
            self.status_row = row
            return httpx.Response(201)
        return httpx.Response(404, json={"message": "unknown table"})
