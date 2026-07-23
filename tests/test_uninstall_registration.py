from pathlib import Path


def test_uninstall_unregisters_configured_bitrix_bot_before_removing_files():
    script = Path(__file__).parents[1] / "install.sh"
    source = script.read_text(encoding="utf-8")

    assert "unregister_bitrix_bot()" in source
    assert "${webhook_base%/}/imbot.v2.Bot.unregister" in source
    assert '\\"botId\\":${bot_id}' in source
    assert '\\"botToken\\":\\"${bot_token}\\"' in source
    assert source.index("unregister_bitrix_bot") < source.index('rm -rf "$INSTALL_DIR"')


def test_install_initializes_database_before_starting_services_and_requires_webhook():
    script = Path(__file__).parents[1] / "install.sh"
    source = script.read_text(encoding="utf-8")
    install_flow = source[source.index("        install)") :]

    assert "step_create_services --no-start" in source
    assert install_flow.index("step_init_db") < install_flow.index('run_cmd systemctl start "$svc"')
    assert "step_setup_telegram_webhook || exit 1" in source
    assert "git -C \"$INSTALL_DIR\" pull --ff-only" in source
