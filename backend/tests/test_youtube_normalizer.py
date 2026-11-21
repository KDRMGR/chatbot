from datetime import timezone

from app.chat.models import EventType, Platform
from app.platforms.youtube.normalizer import is_chat_ended, normalize, parse_time, stable_id
from tests.youtube_fakes import chat_item

BOT = "UC_BOT"


def test_text_message():
    msg = normalize(chat_item("m1", "Try the supercar!", channel="UC1", name="Alice"), BOT)
    assert msg is not None
    assert (msg.platform, msg.viewer_id, msg.username, msg.message, msg.message_id) == (
        Platform.YOUTUBE, "UC1", "Alice", "Try the supercar!", "m1")
    assert msg.event_type is EventType.MESSAGE and not msg.is_bot_self and msg.timestamp.tzinfo is not None


def test_our_own_messages_are_marked_as_self_echoes():
    assert normalize(chat_item("m1", "hi", channel=BOT), BOT).is_bot_self
    assert not normalize(chat_item("m2", "hi", channel="UC9"), BOT).is_bot_self
    assert not normalize(chat_item("m3", "hi", channel=BOT), None).is_bot_self


def test_super_chat_is_a_donation_with_amount_and_comment():
    item = chat_item("m1", "", kind="superChatEvent",
                     superChatDetails={"amountDisplayString": "$5.00", "userComment": "great stream", "tier": 2})
    msg = normalize(item, BOT)
    assert (msg.event_type, msg.amount, msg.message) == (EventType.DONATION, "$5.00", "great stream")


def test_super_sticker_and_memberships():
    sticker = normalize(chat_item("a", kind="superStickerEvent", superStickerDetails={"amountDisplayString": "EUR 2"}), BOT)
    assert (sticker.event_type, sticker.amount, sticker.message) == (EventType.DONATION, "EUR 2", "")
    assert normalize(chat_item("b", kind="newSponsorEvent", newSponsorDetails={"memberLevelName": "Crew"}), BOT).event_type \
        is EventType.MEMBERSHIP
    milestone = normalize(chat_item("c", kind="memberMilestoneChatEvent",
                                    memberMilestoneChatDetails={"userComment": "6 months!", "memberMonth": 6}), BOT)
    assert (milestone.event_type, milestone.message) == (EventType.MEMBERSHIP, "6 months!")
    gifting = normalize(chat_item("d", kind="membershipGiftingEvent", membershipGiftingDetails={"giftMembershipsCount": 5}), BOT)
    assert (gifting.event_type, gifting.amount) == (EventType.MEMBERSHIP, "5")


def test_everything_else_is_ignored():
    for kind in ("userBannedEvent", "tombstone", "chatEndedEvent", "sponsorOnlyModeStartedEvent",
                 "sponsorOnlyModeEndedEvent", "giftMembershipReceivedEvent", "giftEvent", "pollEvent", "somethingNew"):
        assert normalize(chat_item("x", kind=kind), BOT) is None, kind


def test_chat_ended_detection():
    assert is_chat_ended(chat_item("e", kind="chatEndedEvent"))
    assert not is_chat_ended(chat_item("t"))


def test_items_without_an_author_or_id_are_dropped():
    item = chat_item("m1")
    item["authorDetails"] = {}
    item["snippet"].pop("authorChannelId")
    assert normalize(item, BOT) is None
    assert normalize({**chat_item("m1"), "id": None}, BOT) is None


def test_streamer_flag_comes_from_is_chat_owner():
    item = chat_item("m1", "hello chat")
    item["authorDetails"]["isChatOwner"] = True
    assert normalize(item, BOT).is_streamer


def test_hostile_text_is_sanitised_by_the_message_type():
    msg = normalize(chat_item("m1", "hi​ there‮!"), BOT)
    assert msg.message == "hi there!"


def test_long_ids_are_hashed_deterministically_and_fit_the_column():
    long_id = "LCC." + "x" * 300
    assert stable_id("short") == "short"
    hashed = stable_id(long_id)
    assert len(hashed) <= 128 and hashed == stable_id(long_id) and hashed != stable_id(long_id + "y")
    assert normalize(chat_item(long_id, "hello there"), BOT).message_id == hashed


def test_time_parsing():
    assert parse_time("2026-11-20T10:00:00.123456+00:00").isoformat().startswith("2026-11-20T10:00:00.123456")
    assert parse_time("2026-11-20T10:00:00Z").tzinfo == timezone.utc
    assert parse_time("garbage").tzinfo is not None and parse_time(None).tzinfo is not None
