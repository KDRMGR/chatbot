import json
import time

import httpx
import pytest

from app.platforms.youtube.api import (
    AuthError, BadRequest, Forbidden, LiveChatDisabled, LiveChatEnded, LiveChatNotFound, NetworkError, QuotaExceeded,
    RateLimited, ServerError, YouTubeApi, classify_error,
)
from app.platforms.youtube.credentials import NotAuthorized, ReauthRequired, YouTubeAuth
from app.platforms.youtube.oauth import ClientConfig, TokenSet
from app.platforms.youtube.quota import QuotaTracker
from app.security.credential_store import CredentialStore
from tests.youtube_fakes import FakeGoogle, chat_item, chat_page, error_body


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
async def rig(tmp_path, google):
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    auth = YouTubeAuth(CredentialStore(tmp_path), http)
    auth.save_login(ClientConfig("cid", "csecret"), TokenSet("access-1", "refresh-1", time.time() + 3600))
    quota = QuotaTracker()
    api = YouTubeApi(auth, quota, transport=httpx.MockTransport(google.handler))
    yield api, quota, auth, google
    await api.aclose()
    await http.aclose()


async def test_list_messages_request_and_parsing(rig):
    api, quota, _, google = rig
    google.chat_pages.append(chat_page([chat_item("m1")], token="NEXT", interval_ms=6500, offlineAt="2026-11-20T11:00:00Z"))
    page = await api.list_messages("CHAT1", "PREV")
    request = google.calls("GET", "/liveChat/messages")[0]
    assert request.headers["authorization"] == "Bearer access-1"
    assert str(request.url).startswith("https://youtube.googleapis.com/youtube/v3/liveChat/messages?")
    assert google.list_params[0] == {"liveChatId": "CHAT1", "part": "id,snippet,authorDetails", "maxResults": "200",
                                     "profileImageSize": "16", "pageToken": "PREV"}
    assert (page.next_page_token, page.polling_interval_s, page.offline_at) == ("NEXT", 6.5, "2026-11-20T11:00:00Z")
    assert len(page.items) == 1 and quota.used == 5


async def test_first_page_sends_no_page_token(rig):
    api, _, _, google = rig
    await api.list_messages("CHAT1")
    assert "pageToken" not in google.list_params[0]


async def test_insert_payload_and_cost(rig):
    api, quota, _, google = rig
    message_id = await api.insert_message("CHAT1", "hello chat")
    sent = google.inserted[0]
    assert sent["params"] == {"part": "snippet"}
    assert sent["body"] == {"snippet": {"liveChatId": "CHAT1", "type": "textMessageEvent",
                                        "textMessageDetails": {"messageText": "hello chat"}}}
    assert message_id == "MSG-1" and quota.used == 20


async def test_an_expired_access_token_is_refreshed_once_and_the_call_retried(rig):
    api, _, auth, google = rig
    google.valid_tokens = set()  # Google no longer accepts access-1
    google.valid_tokens.add("access-r1")  # ... but will accept the refreshed token
    page = await api.list_messages("CHAT1")
    assert google.refresh_count == 1 and page.items == []
    assert [r.headers["authorization"] for r in google.calls("GET", "/liveChat/messages")] == [
        "Bearer access-1", "Bearer access-r1"]


async def test_a_token_that_stays_rejected_fails_without_looping(rig):
    api, _, _, google = rig
    google.reject_all = True  # even freshly refreshed tokens are refused
    with pytest.raises(AuthError):
        await api.own_channel()
    assert len(google.calls("GET", "/channels")) == 2  # one try + exactly one retry
    assert google.refresh_count == 1


async def test_dead_refresh_token_surfaces_as_reauth_required(rig):
    api, _, auth, google = rig
    google.valid_tokens = set()
    google.refresh_error = (400, {"error": "invalid_grant"})
    with pytest.raises(ReauthRequired):
        await api.own_channel()
    assert auth.reauth_required


async def test_missing_credentials_raise_not_authorized(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(FakeGoogle().handler))
    api = YouTubeApi(YouTubeAuth(CredentialStore(tmp_path), http), QuotaTracker())
    with pytest.raises(NotAuthorized):
        await api.own_channel()
    await api.aclose()
    await http.aclose()


@pytest.mark.parametrize("status,reason,exc", [
    (403, "liveChatEnded", LiveChatEnded), (403, "liveChatDisabled", LiveChatDisabled),
    (404, "liveChatNotFound", LiveChatNotFound), (403, "quotaExceeded", QuotaExceeded),
    (403, "dailyLimitExceeded", QuotaExceeded), (403, "rateLimitExceeded", RateLimited),
    (403, "userRateLimitExceeded", RateLimited), (403, "forbidden", Forbidden),
    (400, "messageTextInvalid", BadRequest), (400, "pageTokenInvalid", BadRequest),
    (500, "backendError", ServerError), (503, "x", ServerError), (429, "x", RateLimited), (401, "x", AuthError),
])
async def test_error_mapping(rig, status, reason, exc):
    api, _, _, google = rig
    google.errors["GET /youtube/v3/liveChat/messages"] = error_body(status, reason)
    with pytest.raises(exc) as info:
        await api.list_messages("CHAT1")
    assert info.value.status == status


async def test_quota_exceeded_from_google_marks_the_local_tracker_exhausted(rig):
    api, quota, _, google = rig
    google.errors["POST /youtube/v3/liveChat/messages"] = error_body(403, "quotaExceeded")
    with pytest.raises(QuotaExceeded):
        await api.insert_message("CHAT1", "hi")
    assert quota.exhausted and not quota.can_send()


async def test_failed_requests_still_cost_quota(rig):
    api, quota, _, google = rig
    google.errors["GET /youtube/v3/liveChat/messages"] = error_body(400, "pageTokenInvalid")
    with pytest.raises(BadRequest):
        await api.list_messages("CHAT1")
    assert quota.used == 5  # Google bills every request that reaches it


@pytest.mark.parametrize("raiser,expected", [(httpx.ConnectError("x"), NetworkError), (httpx.ReadTimeout("x"), NetworkError)])
async def test_transport_failures_become_network_errors(tmp_path, raiser, expected):
    google = FakeGoogle()
    api_handler = google.handler

    def handler(request):
        if request.url.host == "youtube.googleapis.com":
            raise raiser
        return api_handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = YouTubeAuth(CredentialStore(tmp_path), http)
    auth.save_login(ClientConfig("c", "s"), TokenSet("access-1", "r", time.time() + 3600))
    api = YouTubeApi(auth, QuotaTracker(), transport=httpx.MockTransport(handler))
    with pytest.raises(expected):
        await api.list_messages("CHAT1")
    await api.aclose()
    await http.aclose()


async def test_tokens_never_appear_in_error_text(rig):
    api, _, _, google = rig
    google.errors["GET /youtube/v3/channels"] = error_body(403, "forbidden", "nope")
    with pytest.raises(Forbidden) as info:
        await api.own_channel()
    assert "access-1" not in str(info.value) and "refresh-1" not in repr(info.value)


async def test_discovery_methods(rig):
    api, quota, _, google = rig
    assert await api.own_channel() == {"id": "UC_BOT", "title": "StealthBot"}
    broadcast = await api.active_broadcast()
    assert (broadcast.video_id, broadcast.live_chat_id) == ("VID1", "CHAT1")
    assert await api.video_chat_id("VID1") == "CHAT1"
    assert await api.video_chat_id("UNKNOWN") is None
    google.videos["OFFLINE"] = None
    assert await api.video_chat_id("OFFLINE") is None
    request = google.calls("GET", "/liveBroadcasts")[0]
    assert dict(request.url.params) == {"part": "id,snippet,status", "broadcastStatus": "active", "maxResults": "5"}


async def test_active_broadcast_ignores_broadcasts_without_chat_and_handles_none(rig):
    api, _, _, google = rig
    google.broadcasts = [{"id": "V0", "snippet": {}}]
    assert await api.active_broadcast() is None
    google.broadcasts = []
    assert await api.active_broadcast() is None


async def test_channel_search_uses_its_own_quota_bucket(rig):
    api, quota, _, google = rig
    google.search_video = "VIDX"
    assert await api.find_live_video("UC_STREAMER") == "VIDX"
    params = dict(google.calls("GET", "/search")[0].url.params)
    assert params["channelId"] == "UC_STREAMER" and params["eventType"] == "live" and params["type"] == "video"
    assert quota.used == 0 and quota.snapshot()["search_calls"] == 1
    google.search_video = None
    assert await api.find_live_video("UC_STREAMER") is None


async def test_experimental_stream_yields_pages_and_costs_5_units(rig):
    api, quota, _, google = rig
    google.stream_body = json.dumps([chat_page([chat_item("a")], "n1"), chat_page([chat_item("b")], "n2")]).encode()
    pages = [p async for p in api.stream_messages("CHAT1", "RESUME")]
    assert [p.next_page_token for p in pages] == ["n1", "n2"] and quota.used == 5
    request = google.calls("GET", "/liveChat/messages/stream")[0]
    assert request.url.params["pageToken"] == "RESUME" and request.url.params["liveChatId"] == "CHAT1"


async def test_experimental_stream_errors_are_classified(rig):
    api, _, _, google = rig
    google.stream_status = 404
    google.stream_body = json.dumps(error_body(404, "notFound")[1]).encode()
    with pytest.raises(BadRequest):
        _ = [p async for p in api.stream_messages("CHAT1")]
    google.stream_status = 200
    google.stream_body = b"this is not json"
    with pytest.raises(BadRequest) as info:
        _ = [p async for p in api.stream_messages("CHAT1")]
    assert info.value.reason == "streamFraming"


def test_classify_error_is_robust_to_garbage_bodies():
    assert isinstance(classify_error(500, None), ServerError)
    assert isinstance(classify_error(403, "text"), Forbidden)
    assert isinstance(classify_error(400, {"error": "oops"}), BadRequest)
