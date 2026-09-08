import sqlite3
import subprocess
from unittest.mock import patch

import pytest

import tui


@pytest.mark.parametrize(('choice', 'flag'), [('1', []), ('2', ['--uninstall']),
                                            ('3', ['--update']), ('7', ['--unregister-bot'])])
def test_installer_dispatch(tmp_path, choice, flag):
    (tmp_path / 'install.sh').touch()
    with patch('builtins.input', return_value='yes'), patch('tui.subprocess.run') as run, \
            patch('tui.os.geteuid', return_value=0):
        tui.run_action(choice, tmp_path)
    script = tui.Path(tui.__file__).resolve().parent / 'install.sh' if choice == '1' else tmp_path / 'install.sh'
    if choice == '3':
        assert [call.args[0] for call in run.call_args_list] == [
            ['git', '-c', f'safe.directory={tmp_path}', '-C', str(tmp_path), 'fetch', '--all', '--prune'],
            ['git', '-c', f'safe.directory={tmp_path}', '-C', str(tmp_path), 'pull', '--ff-only'],
            ['bash', str(script), *flag],
        ]
    else:
        run.assert_called_once_with(['bash', str(script), *flag], check=True)


def test_installer_dispatch_uses_sudo_for_non_root(tmp_path):
    (tmp_path / 'install.sh').touch()
    with patch('builtins.input', return_value='yes'), patch('tui.subprocess.run') as run, \
            patch('tui.os.geteuid', return_value=1000), patch('tui.shutil.which', return_value='/usr/bin/sudo'):
        tui.run_action('1', tmp_path)
    script = tui.Path(tui.__file__).resolve().parent / 'install.sh'
    run.assert_called_once_with(['sudo', 'bash', str(script)], check=True)


def test_update_fetches_and_pulls_before_running_installer(tmp_path):
    (tmp_path / 'install.sh').touch()
    with patch('builtins.input', return_value='yes'), patch('tui.subprocess.run') as run, \
            patch('tui.os.geteuid', return_value=1000), patch('tui.shutil.which', return_value='/usr/bin/sudo'):
        tui.run_action('3', tmp_path)
    assert [call.args[0] for call in run.call_args_list] == [
        ['sudo', 'git', '-c', f'safe.directory={tmp_path}', '-C', str(tmp_path), 'fetch', '--all', '--prune'],
        ['sudo', 'git', '-c', f'safe.directory={tmp_path}', '-C', str(tmp_path), 'pull', '--ff-only'],
        ['sudo', 'bash', str(tmp_path / 'install.sh'), '--update'],
    ]


def test_read_views_do_not_execute_config_or_modify_database(tmp_path, capsys):
    database = tmp_path / 'custom.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE chat_mappings (id, tg_chat_id, bitrix_dialog_id, topic_ids, label)')
        connection.execute('INSERT INTO chat_mappings VALUES (1, -100, "chat42", "3,4", "Название")')
    (tmp_path / '.env').write_text('BITRIX_BOT_ID="42"\nMIRROR_STATE_DB_PATH=custom.sqlite3\n'
                                 'SECRET=$(exit 99)\n', encoding='utf-8')
    before = database.read_bytes()
    tui.run_action('5', tmp_path)
    tui.run_action('6', tmp_path)
    output = capsys.readouterr().out
    assert 'BITRIX_BOT_ID=42' in output
    assert '1 | -100 | chat42 | 3,4 | Название' in output
    assert 'SECRET' not in output
    assert before == database.read_bytes()


def test_read_config_uses_sudo_when_env_is_not_readable(tmp_path):
    env_file = tmp_path / '.env'
    env_file.write_text('BITRIX_BOT_ID="42"\nMIRROR_STATE_DB_PATH=state.sqlite3\n', encoding='utf-8')
    with patch.object(type(env_file), 'read_text', side_effect=PermissionError), \
            patch('tui.os.geteuid', return_value=1000), patch('tui.shutil.which', return_value='/usr/bin/sudo'), \
            patch('tui.subprocess.run') as run:
        run.return_value.stdout = 'BITRIX_BOT_ID="42"\nMIRROR_STATE_DB_PATH=state.sqlite3\n'
        assert tui.read_config(tmp_path) == {'BITRIX_BOT_ID': '42', 'MIRROR_STATE_DB_PATH': 'state.sqlite3'}
    run.assert_called_once_with(['sudo', 'cat', str(env_file)], capture_output=True, text=True, check=True)


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        tui.show_mappings(tmp_path, {})
    assert not (tmp_path / 'mirror_state.sqlite3').exists()


def test_status_checks_both_services(tmp_path):
    with patch('tui.subprocess.run') as run:
        tui.run_action('4', tmp_path)
    assert run.call_args.args[0] == ['systemctl', '--no-pager', '--full', 'status', *tui.SERVICES]


def test_cancel_does_not_install(tmp_path):
    with patch('builtins.input', return_value='no'), patch('tui.subprocess.run') as run:
        tui.run_action('1', tmp_path)
    run.assert_not_called()


def test_menu_recovers_from_error_and_exits():
    with patch('sys.argv', ['tui.py']), patch('builtins.input', side_effect=['5', '', '0']), \
            patch('tui.run_action', side_effect=OSError('missing config')) as action:
        tui.main()
    action.assert_called_once()


@pytest.mark.parametrize(('answer', 'code', 'called'), [('no', 0, False), ('yes', 1, True)])
def test_unregister_confirmation_and_failure(tmp_path, answer, code, called):
    source = (tui.Path(tui.__file__).resolve().parent / 'install.sh').read_text()
    main = source[source.index('main() {'):]
    script = ('set -euo pipefail\nLOG_FILE=' + str(tmp_path / 'install.log') + '\n'
              'check_root() { :; }\n'
              'unregister_bitrix_bot() { echo CALLED; return "$1"; }\n' + main)
    result = subprocess.run(['bash', '-c', script, 'test', '--unregister-bot'],
                            input=answer + '\n', text=True, capture_output=True, check=False)
    assert result.returncode == code
    assert ('CALLED' in result.stdout) == called
