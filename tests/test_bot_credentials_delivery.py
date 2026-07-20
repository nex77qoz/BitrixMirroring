from pathlib import Path


def test_installation_displays_and_sends_new_bitrix_bot_credentials():
    source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "notify_telegram_admins_about_bitrix_bot()" in source
    assert "${BITRIX_BOT_ID}" in source
    assert "${BITRIX_BOT_CLIENT_ID}" in source
    assert 'api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage' in source
    assert 'BITRIX_BOT_ID:' in source
    assert 'BITRIX_BOT_CLIENT_ID:' in source
    registration_result = source.index('print_ok "$msg (BOT_ID=$BITRIX_BOT_ID)"')
    assert source.index('print_info "  BOT_TOKEN=$BITRIX_BOT_CLIENT_ID"', registration_result) > registration_result
    assert source.index('notify_telegram_admins_about_bitrix_bot', source.index('ask_optional TG_ADMIN_IDS')) > source.index('ask_optional TG_ADMIN_IDS')
