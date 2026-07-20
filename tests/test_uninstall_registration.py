from pathlib import Path


def test_uninstall_unregisters_configured_bitrix_bot_before_removing_files():
    script = Path(__file__).parents[1] / "install.sh"
    source = script.read_text(encoding="utf-8")

    assert "unregister_bitrix_bot()" in source
    assert "${webhook_base%/}/imbot.v2.Bot.unregister" in source
    assert '\\"botId\\":${bot_id}' in source
    assert '\\"botToken\\":\\"${bot_token}\\"' in source
    assert source.index("unregister_bitrix_bot") < source.index('rm -rf "$INSTALL_DIR"')
