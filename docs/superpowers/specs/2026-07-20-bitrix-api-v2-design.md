# Bitrix API 2.0 Migration

## Goal

Make the production path use only Bitrix Chatbot API 2.0 in `supervisor + fetch` mode.

## Design

- `register_bot.py` uses only `imbot.v2.Bot.list`, `imbot.v2.Bot.register`, and `imbot.v2.Bot.unregister`.
- Unregister requests include the registered `botId` and the same `botToken`.
- Runtime receives Bitrix events only through `imbot.v2.Event.get` and sends data through `imbot.v2.*` methods.
- The legacy Bitrix webhook service, unit, nginx route, configuration bridge, and legacy event handlers are removed.
- Installer writes `BITRIX_WEBHOOK_BRIDGE_ENABLED=false` and documents fetch mode as the only mode.
- Tests assert the absence of legacy Bitrix methods and the required v2 payloads.
