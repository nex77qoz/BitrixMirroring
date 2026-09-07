#!/usr/bin/env python3
"""Terminal menu for the standard /opt/bitrix-bot installation."""

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path

INSTALL_DIR = Path('/opt/bitrix-bot')
SERVICES = ('bitrix-telegram-mirror', 'bitrix-monitor')
MENU = '''
Mirroring
1. Установить Mirroring
2. Удалить Mirroring и все данные
3. Обновить Mirroring
4. Проверить статус
5. Показать ID бота
6. Показать маппинги
7. Удалить регистрацию бота
0. Выход
'''


def read_config(directory: Path) -> dict[str, str]:
    config = {}
    env_file = directory / '.env'
    try:
        lines = env_file.read_text(encoding='utf-8').splitlines()
    except PermissionError:
        if os.geteuid() == 0 or shutil.which('sudo') is None:
            raise
        result = subprocess.run(['sudo', 'cat', str(env_file)], capture_output=True, text=True, check=True)
        lines = result.stdout.splitlines()
    for line in lines:
        key, sep, value = line.partition('=')
        if sep and key.strip() in {'BITRIX_BOT_ID', 'MIRROR_STATE_DB_PATH'}:
            parts = shlex.split(value, comments=True)
            config[key.strip()] = ' '.join(parts)
    return config


def show_mappings(directory: Path, config: dict[str, str]) -> None:
    path = directory / (config.get('MIRROR_STATE_DB_PATH') or 'mirror_state.sqlite3')
    query = 'SELECT id, tg_chat_id, bitrix_dialog_id, topic_ids, label FROM chat_mappings ORDER BY id'
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            rows = connection.execute(query).fetchall()
    except (PermissionError, sqlite3.OperationalError):
        if not path.is_file() or os.geteuid() == 0 or shutil.which('sudo') is None:
            raise
        result = subprocess.run(
            ['sudo', 'sqlite3', '-readonly', '-separator', '\t', str(path), query],
            capture_output=True, text=True, check=True,
        )
        rows = [tuple(line.split('\t')) for line in result.stdout.splitlines() if line]
    print('ID | Telegram | Bitrix | Темы | Название')
    for row in rows:
        # Escape control characters from remote chat labels before terminal output.
        print(' | '.join(ascii(str(value))[1:-1] if not str(value).isprintable() else str(value)
                         for value in row))
    if not rows:
        print('Маппингов нет.')


def run_action(choice: str, directory: Path) -> None:
    if choice in {'1', '2', '3', '7'}:
        script = (Path(__file__).resolve().parent if choice == '1' else directory) / 'install.sh'
        if not script.is_file():
            raise FileNotFoundError(f'Установщик не найден: {script}')
        if choice in {'1', '3'}:
            if input('Изменить установку на сервере? Введите yes: ').strip() != 'yes':
                print('Отменено.')
                return
        flag = {'1': [], '2': ['--uninstall'], '3': ['--update'], '7': ['--unregister-bot']}[choice]
        command = ['bash', str(script), *flag]
        if os.geteuid() != 0:
            if shutil.which('sudo') is None:
                raise OSError('Для этого действия нужен root или установленный sudo.')
            command.insert(0, 'sudo')
        subprocess.run(command, check=True)
    elif choice == '4':
        result = subprocess.run(['systemctl', '--no-pager', '--full', 'status', *SERVICES], check=False)
        if result.returncode:
            print(f'Проверка systemctl завершилась с кодом {result.returncode}: см. вывод выше.')  # noqa: RUF001
    elif choice in {'5', '6'}:
        config = read_config(directory)
        if choice == '5':
            bot_id = config.get('BITRIX_BOT_ID', '')
            print(f'BITRIX_BOT_ID={bot_id}' if bot_id.isascii() and bot_id.isdecimal()
                  else 'ID бота не задан или некорректен.')
        else:
            show_mappings(directory, config)
    else:
        print('Выберите пункт от 0 до 7.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    while True:
        try:
            print(MENU)
            choice = input('Выбор: ').strip()
            if choice == '0':
                return
            try:
                run_action(choice, INSTALL_DIR)
            except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as exc:
                print(f'Ошибка: {exc}')
            input('\nEnter — вернуться в меню...')
        except (EOFError, KeyboardInterrupt):
            print('\nВыход.')  # noqa: RUF001
            return


if __name__ == '__main__':
    main()
