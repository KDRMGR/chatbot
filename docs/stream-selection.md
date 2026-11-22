# Which stream the bot comments in

**Status (2026-11-22): built and tested with fakes (63 tests; 533 in the whole project pass), and the dashboard tab was checked on the real bot with Kick only.
The YouTube side has NOT been run against the real YouTube** (the Google setup is not done yet).

## What it does
- **A watch list** of the streams the bot may comment in (dashboard tab **Streams**).
- **Live checks:** the bot looks at every switched-on stream every 60 seconds (setting) and shows a dot: green = live, red = offline,
  grey = not checked yet or the check failed (that is *unknown*, not *offline*).
- **Selecting a stream:** press **Use this stream** and the bot comments there as soon as it is live.
- **Fail-safe (on by default):** if you select nothing, the bot starts with the live stream on the list that has the highest priority
  (list order breaks ties). If the stream you selected is not live, the bot waits 3 minutes (it may just be starting), then moves to
  a live stream from the list, and moves back as soon as your selected stream is live. With the fail-safe **off** the bot only ever
  uses the stream you selected and idles otherwise.
- **No flapping:** a live stream must fail two checks in a row before it counts as offline, and the bot stays in the stream it is in
  while that stream is live, even if a higher-priority one starts.
- **Nothing to set up for the old behaviour:** each connected platform has an "own stream" entry (YouTube: the signed-in account's
  live stream, found as before; Kick: the signed-in channel). With an empty list the bot behaves exactly as it did.

## What is on the list
| Entry | How it is checked | Cost |
|---|---|---|
| YouTube, signed-in account's own stream | read from the YouTube connection itself while it is connected (free); `liveBroadcasts.list` (1 unit) only while the bot is in another stream | none / 1 unit per check |
| YouTube video (paste a watch / youtu.be / live link or the 11-character id) | `videos.list` (1 unit) per check; free while the bot is already in that video | 1 unit per minute |
| YouTube channel (paste a youtube.com/channel/UC... link or the id) | `search.list`, which has its own small daily allowance (the quota guard enforces it), so each channel is checked only every 15 minutes (setting); with 2 channels each is checked every 30 minutes | 100 units of the separate search allowance |
| Kick, the default channel (`STEALTHBOT_KICK_TARGET_CHANNEL`, or the signed-in account's own channel) | Kick's channel lookup | none |
| Kick channel (paste `kick.com/name`) | the same lookup: ONE call (`GET /channels?slug=a&slug=b`) covers every Kick channel on the list | none (about one call a minute) |

Every call goes through the existing YouTube quota guard; a blocked check is shown as *unknown* with the reason.

## Limits you should know about
- **Kick: other channels work, with a different mechanism.** Chat of a channel other than the signed-in account's is read through
  the app's own token (an app token CAN subscribe to another channel; verified live), and the bot's answers are posted there as the
  signed-in account (a normal user message, verified live). Kick has no documented "stream went live" event, so the live flag comes
  from polling the channel lookup. Moving to another Kick channel reconnects the chat listener (a few seconds).
  When no Kick channel on the list is live the bot is **not** switched off: it stays where it is (or in the default channel), so it keeps
  listening.
- **YouTube channel handles (@name) cannot be looked up.** Paste the youtube.com/channel/UC... address instead.
- **Only add streams you own or have the owner's permission to chat in.** An automatic bot in someone else's chat can break the
  platform's rules and annoy people.
- **Every stream has its own topic and the chat memory is cleared when the bot moves** (see `stream-topic.md`).
- **Not verified live:** moving the real YouTube connection to another video, the real search/videos checks, and quota use over a
  whole stream. Tested only against a fake Google.
- A short wait applies after the bot starts (15 s) so the connections can come up before the first look.

## Manual connect/disconnect is respected
The director acts only when its *decision changes*. If you press Disconnect for a platform in the dashboard, it will not switch it
back on by itself unless the decision for that platform changes (for example another stream becomes the choice).

## API (all need the X-API-Key like the rest)
| Request | Does |
|---|---|
| `GET /api/streams` | the list, live state, the current choice per platform and why, settings |
| `POST /api/streams/targets` `{"platform":"youtube","ref":"<link or id>","label":"...","priority":50}` | add a YouTube video/channel |
| `POST /api/streams/targets/{id}` `{"label","priority","enabled"}` | change an entry |
| `POST /api/streams/targets/{id}/remove` | remove (own streams cannot be removed: switch them off) |
| `POST /api/streams/select` `{"target_id": "<id>" or null}` | select a stream / clear the selection |
| `POST /api/streams/settings` `{"auto_select","failover_after_s","poll_interval_s","youtube_channel_probe_s"}` | the fail-safe settings (all or nothing) |
| `POST /api/streams/check` | look at every switched-on stream now |

Saved in the database (`streams.config`), so the list and the selection survive restarts. Decisions are written to the connection log
(History & logs) with the reason.

## Where the code is
`backend/app/streams/` (`model.py` list, validation and pasted links; `choose.py` the decision rules as pure functions;
`director.py` the live checks, the loop and the control of the adapters), `YouTubeAdapter.retarget()`, routes in `api/routes.py`,
the dashboard tab in `dashboard/`, tests in `tests/test_streams.py` and `tests/test_youtube_adapter.py`.
