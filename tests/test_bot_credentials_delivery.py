from pathlib import Path


def test_installation_displays_and_sends_new_bitrix_bot_credentials():
    source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "notify_telegram_admins_about_bitrix_bot()" in source
    assert "${BITRIX_BOT_ID}" in source
    assert "${BITRIX_BOT_CLIENT_ID}" in source
    assert 'api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage' in source
    assert 'BITRIX_BOT_ID:' in source
    assert 'BITRIX_BOT_CLIENT_ID:' in source
