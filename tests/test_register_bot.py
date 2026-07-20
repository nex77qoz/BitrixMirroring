from pathlib import Path


def test_bot_lifecycle_uses_only_v2_methods_and_unregisters_with_token():
    source = (Path(__file__).parents[1] / "server-side/register_bot.py").read_text(encoding="utf-8")

    assert '"imbot.v2.Bot.register"' in source
    assert '"botToken": new_token' in source
    assert "imbot.bot.list" not in source
    assert "imbot.unregister" not in source
