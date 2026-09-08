# Bitrix Bot API 2.0 Fetch Migration

## Goal

Allow employees to add the Bitrix bot to group chats and mirror those chats to
Telegram without requiring the owner of the incoming webhook to be a chat
member.

## Registration

The bot is registered manually with `imbot.v2.Bot.register` using:

- `fields.type: "supervisor"` so it receives every message in chats where the
  bot is a member;
- `fields.eventMode: "fetch"`;
- `fields.isHidden: false`;
- the existing `BITRIX_BOT_CLIENT_ID` value as `fields.botToken`.

The returned bot ID is stored in `BITRIX_BOT_ID`. Re-registration may produce a
new bot ID, so mappings are restored manually after registration.

## Architecture

`BitrixClient` fetches event pages with `imbot.v2.Event.get`, passing
`botId`, `botToken`, the persisted offset, and a bounded page size.

`MirrorService` runs one Bitrix event loop instead of polling every mapped
dialog through `im.dialog.messages.get`. Events are routed using
`data.chat.dialogId`. Event payloads are converted to the existing
`BitrixMessage`, `BitrixUser`, and `BitrixFile` models so current rendering and
Telegram delivery code remains reusable.

The live Bitrix-to-Telegram path must not call user-scoped `im.*` methods.
`imbot.v2.Chat.Message.get` may be used only when a valid event omits data
required for processing.

The legacy `server-side/app.py` service remains installable for compatibility
and monitoring, but it is not part of Bot API 2.0 fetch delivery.

## Offset and Delivery Semantics

The fetch offset is stored in the existing SQLite `runtime_settings` table
under a key containing `BITRIX_BOT_ID`. A newly registered bot therefore cannot
inherit the previous bot's event offset.

Events are processed sequentially. After an event is successfully handled or
intentionally ignored, its acknowledgement offset is persisted. A transient
Bitrix, Telegram, or SQLite error leaves the offset unchanged and the existing
backoff retries the event.

Malformed events that cannot identify their type or required identifiers are
logged and acknowledged so one invalid event cannot block the queue.

Delivery is at-least-once. Existing message links make normal retries
idempotent. A duplicate remains possible if Telegram accepts a new message and
SQLite fails before its link is committed; cross-system transactions are out
of scope.

## Event Handling

### `ONIMBOTV2MESSAGEADD`

- Resolve the mapping from `data.chat.dialogId`.
- Ignore unmapped chats.
- Suppress bot-originated loopback messages using the bot ID and existing
  message links.
- Handle `/tg_connect` locally by generating the existing one-time token,
  saving it with `MirrorStateStore.save_pending_connection()`, and replying
  through `imbot.v2.Chat.Message.send`.
- Convert the event message, user, and file metadata to an in-memory snapshot.
- Download attachments through `imbot.v2.File.download`.
- Send the message to Telegram and save the Bitrix/Telegram message link.

### `ONIMBOTV2MESSAGEUPDATE`

- Resolve the existing message link.
- Ignore messages without links.
- Edit Telegram text or caption using the existing formatting behavior.
- Save the new Bitrix revision.

### `ONIMBOTV2MESSAGEDELETE`

- Resolve the existing message link from `data.messageId`.
- Delete the Telegram message and then delete the local link.
- Treat Telegram's “message not found” response as successful cleanup.

### `ONIMBOTV2REACTIONCHANGE`

- Update the stored set of Bitrix reactions using `data.user.id`,
  `data.reaction`, and `data.action`.
- Preserve the current project behavior: Telegram shows 👍 when the resulting
  Bitrix reaction set is non-empty and removes it when empty.
- Preserve suppression of reactions produced by Telegram-to-Bitrix mirroring.

### `ONIMBOTV2JOINCHAT`

Send the existing greeting through the bot API. No mapping is created
automatically; `/tg_connect` remains the explicit pairing flow.

Unknown event types and valid events for unmapped chats are acknowledged
without side effects.

## Lifecycle and Configuration

The existing `BITRIX_POLL_INTERVAL_SECONDS`, retry, concurrency, and backoff
settings are reused for the fetch loop. No new dependency is introduced.

Startup loads the bot-specific offset and starts one fetch task after Telegram
is ready. Shutdown cancels and awaits that task. `SYNC_BITRIX_TO_TELEGRAM=false`
prevents delivery while still safely acknowledging events so disabled periods
do not replay later.

Documentation and environment templates describe the required `supervisor`
and `fetch` registration fields. Legacy webhook event settings are marked as
compatibility-only.

## Testing

Tests must cover:

- parsing and validation of `imbot.v2.Event.get` pages;
- bot-specific offset persistence;
- successful offset advancement and retry without advancement;
- add, update, delete, reaction, join-chat, and `/tg_connect` events;
- unknown and unmapped events;
- bot loop suppression;
- text, reply, and file payload conversion;
- absence of `im.dialog.messages.get` and `im.dialog.messages.search` calls from
  the event delivery path.

Verification consists of the full unittest suite, Python syntax compilation,
and `git diff --check`.

## References

- [Bitrix24 Bot API 2.0 overview](https://apidocs.bitrix24.ru/api-reference/chat-bots/chat-bots-v2/index.html)
- [`imbot.v2.Event.get`](https://apidocs.bitrix24.ru/api-reference/chat-bots/chat-bots-v2/imbot.v2/events/event-get.html)
- [Bot API 2.0 event formats](https://apidocs.bitrix24.ru/api-reference/chat-bots/chat-bots-v2/imbot.v2/events/events.html)
