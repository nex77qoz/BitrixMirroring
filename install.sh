#!/usr/bin/env bash
# ==============================================================================
#  Bitrix-Telegram Mirror Bot — Auto Installer
#  Usage:
#    ./install.sh            — full installation
#    ./install.sh --update   — pull latest code and restart services
#    ./install.sh --uninstall — remove everything
# ==============================================================================

set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/bitrix-bot"
REPO_URL=""       # auto-detected from the cloned repo's remote.origin.url
REPO_BRANCH=""   # auto-detected from the current branch of the cloned repo
VENV="$INSTALL_DIR/.venv"
ENV_FILE="$INSTALL_DIR/.env"
DB_FILE="$INSTALL_DIR/mirror_state.sqlite3"
LOG_FILE="/var/log/bitrix-bot-install.log"
NGINX_CONF="/etc/nginx/sites-available/bitrix-bot"
NGINX_LINK="/etc/nginx/sites-enabled/bitrix-bot"
SSL_DIR="/etc/ssl/bitrix-bot"
SSL_CERT="${SSL_DIR}/cert.pem"
SSL_KEY="${SSL_DIR}/key.pem"

SERVICES=("bitrix-telegram-mirror" "bitrix-bot" "bitrix-monitor")

# Set to true by step_setup_ssl when user skips SSL
SKIP_SSL=false

# Python binary used throughout the script
PYTHON_BIN="python3"

# Resolved script path (avoids /dev/fd/XX when run via pipe)
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "$0")"

# Auto-detect repo URL and branch from the directory the script is running from.
# This works when the user clones the repo and runs: sudo bash install.sh
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
if git -C "$SCRIPT_DIR" rev-parse --git-dir > /dev/null 2>&1; then
    REPO_URL="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || true)"
    REPO_BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Colours
# ──────────────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
log()          { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }
print_step()   { echo -e "\n${BLUE}${BOLD}▶ $*${RESET}"; log "STEP: $*"; }
print_ok()     { echo -e "  ${GREEN}✓ $*${RESET}"; log "OK: $*"; }
print_warn()   { echo -e "  ${YELLOW}⚠ $*${RESET}"; log "WARN: $*"; }
print_error()  { echo -e "  ${RED}✗ $*${RESET}"; log "ERROR: $*"; }
print_info()   { echo -e "  ${CYAN}ℹ $*${RESET}"; log "INFO: $*"; }

ask_input() {
    # ask_input VAR "Prompt text" [default]
    local var="$1" prompt="$2" default="${3-}"
    local value=""
    while [[ -z "$value" ]]; do
        if [[ -n "$default" ]]; then
            echo -en "  ${YELLOW}${prompt} [${default}]: ${RESET}"
            read -r value
            value="${value:-$default}"
        else
            echo -en "  ${YELLOW}${prompt}: ${RESET}"
            read -r value
        fi
        if [[ -z "$value" ]]; then
            print_error "Значение не может быть пустым."
        fi
    done
    printf -v "$var" '%s' "$value"
}

ask_optional() {
    local var="$1" prompt="$2"
    echo -en "  ${YELLOW}${prompt} (Enter чтобы пропустить): ${RESET}"
    read -r value
    printf -v "$var" '%s' "$value"
}

ask_password() {
    local var="$1" prompt="$2"
    local pw="" pw2=""
    while true; do
        echo -en "  ${YELLOW}${prompt}: ${RESET}"
        read -rs pw; echo
        echo -en "  ${YELLOW}Повторите пароль ${CYAN}(ввод скрыт)${YELLOW}: ${RESET}"
        read -rs pw2; echo
        if [[ -z "$pw" ]]; then
            print_error "Пароль не может быть пустым."
        elif [[ "$pw" != "$pw2" ]]; then
            print_error "Пароли не совпадают. Повторите."
        else
            break
        fi
    done
    printf -v "$var" '%s' "$pw"
}

ask_secret() {
    # Single secret input (no confirmation)
    local var="$1" prompt="$2"
    local value=""
    while [[ -z "$value" ]]; do
        echo -en "  ${YELLOW}${prompt}: ${RESET}"
        read -rs value; echo
        if [[ -z "$value" ]]; then
            print_error "Значение не может быть пустым."
        fi
    done
    printf -v "$var" '%s' "$value"
}

run_cmd() {
    log "CMD: $*"
    if ! "$@" >> "$LOG_FILE" 2>&1; then
        print_error "Команда завершилась с ошибкой: $*"
        print_info "Подробности: $LOG_FILE"
        exit 1
    fi
}

banner() {
    echo -e "${CYAN}${BOLD}"
    cat << 'EOF'
╔══════════════════════════════════════════════════════════╗
║         Bitrix  ↔  Telegram  Mirror  Bot                 ║
║                  Auto Installer v1.5                     ║
╚══════════════════════════════════════════════════════════╝
EOF
    echo -e "${RESET}"
}

# ──────────────────────────────────────────────────────────────────────────────
# Guard: root only
# ──────────────────────────────────────────────────────────────────────────────
check_root() {
    if [[ "$EUID" -ne 0 ]]; then
        echo -e "${RED}Скрипт должен запускаться от root. Используйте: sudo $0${RESET}"
        exit 1
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1 — System update
# ──────────────────────────────────────────────────────────────────────────────
step_update_system() {
    print_step "Обновление системы"
    run_cmd apt-get update -y
    run_cmd apt-get upgrade -y
    print_ok "Система обновлена"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 2 — Install system packages
# ──────────────────────────────────────────────────────────────────────────────
step_install_packages() {
    print_step "Установка системных зависимостей"

    local pkgs=(git nginx curl sqlite3 openssl python3 python3-venv python3-dev)
    run_cmd apt-get install -y "${pkgs[@]}"

    PYTHON_BIN="python3"
    print_info "Будет использован Python: ${BOLD}$(python3 --version 2>&1)${RESET}"
    print_ok "Все системные зависимости установлены"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 3 — Clone / pull repository
# ──────────────────────────────────────────────────────────────────────────────
step_clone_repo() {
    print_step "Получение исходного кода"

    if [[ -z "$REPO_URL" ]]; then
        print_error "Не удалось определить URL репозитория."
        print_info "Запустите скрипт из склонированного репозитория:"
        print_info "  git clone <repo-url>"
        print_info "  cd <repo-dir>"
        print_info "  sudo bash install.sh"
        exit 1
    fi

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        print_info "Репозиторий уже существует — выполняем git pull"
        run_cmd git -C "$INSTALL_DIR" pull
    else
        print_info "Клонирование из $REPO_URL (ветка: ${REPO_BRANCH:-default})"
        if [[ -n "$REPO_BRANCH" && "$REPO_BRANCH" != "HEAD" ]]; then
            run_cmd git clone -b "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
        else
            run_cmd git clone "$REPO_URL" "$INSTALL_DIR"
        fi
    fi
    print_ok "Код загружен в $INSTALL_DIR"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 4 — Python venv + pip dependencies
# ──────────────────────────────────────────────────────────────────────────────
step_python_deps() {
    print_step "Настройка Python-окружения и установка зависимостей"

    if [[ ! -d "$VENV" ]]; then
        run_cmd "$PYTHON_BIN" -m venv "$VENV"
    fi
    run_cmd "$VENV/bin/pip" install --upgrade pip
    run_cmd "$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
    print_ok "Python-зависимости установлены"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 5 — Interactive configuration
# ──────────────────────────────────────────────────────────────────────────────
step_collect_config() {
    print_step "Сбор конфигурации"

    echo -e "\n${BOLD}  Введите параметры бота (все поля обязательны):${RESET}\n"

    # Bitrix webhook
    while true; do
        ask_input BITRIX_WEBHOOK_BASE "URL вебхука Битрикс (https://company.bitrix24.ru/rest/1/CODE)"
        if [[ "$BITRIX_WEBHOOK_BASE" == https://* ]]; then
            break
        fi
        print_error "URL должен начинаться с https://"
    done

    # Domain
    ask_input DOMAIN "Домен сервера (например: bot.example.com)"
    BOT_HANDLER_URL="https://${DOMAIN}/bitrix/bot"
    print_info "URL обработчика бота: ${BOLD}${BOT_HANDLER_URL}${RESET}"
    print_info "Укажите этот URL при регистрации бота в Битрикс (поле handler_url)"

    # Bot IDs
    ask_input BITRIX_BOT_ID    "BOT_ID бота в Битрикс"
    ask_input BITRIX_BOT_CLIENT_ID "CLIENT_ID бота в Битрикс"

    # Email for SSL certificate (acme.sh)
    ask_input ACME_EMAIL "Email для SSL-сертификата (Let's Encrypt уведомления)"

    # Telegram token
    print_info "Ввод скрыт — символы не отображаются"
    ask_secret TELEGRAM_BOT_TOKEN "Telegram Bot Token"

    # Monitor password
    print_info "Ввод скрыт — символы не отображаются"
    ask_password MONITOR_PASSWORD "Пароль для мониторинг-дашборда (/monitor)"

    # Optional proxy
    echo ""
    echo -en "  ${YELLOW}Использовать SOCKS5-прокси? (y/N): ${RESET}"
    read -r use_proxy
    if [[ "${use_proxy,,}" == "y" ]]; then
        ENABLE_SOCKS5_PROXY="true"
        ask_input SOCKS5_PROXY_URL "URL SOCKS5-прокси (socks5://user:pass@host:port)"
    else
        ENABLE_SOCKS5_PROXY="false"
        SOCKS5_PROXY_URL=""
    fi

    print_ok "Конфигурация собрана"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 6 — Write .env file
# ──────────────────────────────────────────────────────────────────────────────
step_write_env() {
    print_step "Создание файла .env"

    cat > "$ENV_FILE" << EOF
# ============================================================
#  Bitrix-Telegram Mirror Bot — конфигурация
#  Сгенерировано: $(date '+%Y-%m-%d %H:%M:%S')
# ============================================================

# Telegram
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}

# SOCKS5 proxy
ENABLE_SOCKS5_PROXY=${ENABLE_SOCKS5_PROXY}
SOCKS5_PROXY_URL=${SOCKS5_PROXY_URL}

# Bitrix
BITRIX_WEBHOOK_BASE=${BITRIX_WEBHOOK_BASE}
BITRIX_BOT_ID=${BITRIX_BOT_ID}
BITRIX_BOT_CLIENT_ID=${BITRIX_BOT_CLIENT_ID}

# Маппинг чатов (добавляется в следующем шаге)

# Форматирование
PREFIX_WITH_CHAT_TITLE=true
PREFIX_WITH_SENDER=true
PREFIX_WITH_TIMESTAMP=true
BITRIX_DISABLE_LINK_PREVIEW=true

# Синхронизация
SYNC_TELEGRAM_TO_BITRIX=true
SYNC_BITRIX_TO_TELEGRAM=true
BITRIX_POLL_INTERVAL_SECONDS=5

# Хранилище
MIRROR_STATE_DB_PATH=${INSTALL_DIR}/mirror_state.sqlite3
BITRIX_CURSOR_STATE_PATH=${INSTALL_DIR}/bitrix_cursor_state.json

# Retry / backoff
BITRIX_RETRY_ATTEMPTS=4
BITRIX_RETRY_BASE_DELAY_SECONDS=1
BITRIX_RETRY_MAX_DELAY_SECONDS=15
BITRIX_POLL_ERROR_BACKOFF_SECONDS=2
BITRIX_POLL_MAX_BACKOFF_SECONDS=30

# Производительность
BITRIX_USER_CACHE_TTL_SECONDS=300
BITRIX_MAX_CONCURRENT_REQUESTS=5
BITRIX_SEND_QUEUE_MAXSIZE=1000
BITRIX_SEND_WORKERS=2
BITRIX_RESCAN_RECENT_MESSAGES_LIMIT=100
REQUEST_TIMEOUT_SECONDS=20

# Мониторинг-дашборд
MONITOR_USERNAME=admin
MONITOR_PASSWORD=${MONITOR_PASSWORD}

# Логирование
LOG_LEVEL=INFO
EOF

    chmod 600 "$ENV_FILE"
    print_ok ".env создан ($ENV_FILE)"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 7a — Obtain SSL certificate via acme.sh
# ──────────────────────────────────────────────────────────────────────────────
step_setup_ssl() {
    print_step "Получение SSL-сертификата через acme.sh (Let's Encrypt)"

    echo -en "  ${YELLOW}Выпустить получение SSL-сертификата? (Y/n): ${RESET}"
    read -r ssl_answer
    if [[ "${ssl_answer,,}" == "n" ]]; then
        print_warn "Получение сертификата пропущено. nginx будет настроен на HTTP."
        SKIP_SSL=true
        return 0
    fi
    SKIP_SSL=false

    mkdir -p "$SSL_DIR"
    chmod 700 "$SSL_DIR"

    # Install acme.sh if not already installed
    if [[ ! -f "$HOME/.acme.sh/acme.sh" ]]; then
        print_info "Установка acme.sh..."
        curl -fsSL https://get.acme.sh | sh -s email="${ACME_EMAIL}" >> "$LOG_FILE" 2>&1
        [[ -f "$HOME/.acme.sh/acme.sh.env" ]] && source "$HOME/.acme.sh/acme.sh.env" || true
        print_ok "acme.sh установлен"
    else
        print_info "acme.sh уже установлен"
    fi

    local ACME="$HOME/.acme.sh/acme.sh"

    # Switch default CA to Let's Encrypt and register account
    print_info "Переключение CA на Let's Encrypt..."
    "$ACME" --set-default-ca --server letsencrypt >> "$LOG_FILE" 2>&1
    "$ACME" --register-account -m "${ACME_EMAIL}" --server letsencrypt >> "$LOG_FILE" 2>&1
    print_ok "CA настроен: Let's Encrypt"

    # Check if certificate already exists and is valid
    local cert_exists=false
    if [[ -f "$SSL_CERT" ]] && openssl x509 -checkend 86400 -noout -in "$SSL_CERT" 2>/dev/null; then
        print_info "Действующий сертификат уже существует (срок > 24ч) — пропускаем выпуск"
        cert_exists=true
    fi

    if [[ "$cert_exists" == false ]]; then
        # Stop nginx temporarily so acme.sh standalone can bind port 80
        print_info "Временно останавливаем nginx для standalone-проверки..."
        systemctl stop nginx >> "$LOG_FILE" 2>&1 || true

        print_info "Выпуск сертификата для ${DOMAIN} (standalone на порту 80)..."
        if "$ACME" --issue -d "${DOMAIN}" --standalone >> "$LOG_FILE" 2>&1; then
            print_ok "Сертификат успешно выпущен"
        else
            local acme_exit=$?
            if [[ $acme_exit -eq 2 ]]; then
                # Exit code 2 = already issued, not due for renewal
                print_info "Сертификат уже актуален (acme.sh exit 2) — продолжаем установку"
            else
                print_error "acme.sh завершился с ошибкой (exit code $acme_exit)"
                print_info "Полный лог: $LOG_FILE"
                print_info "Команды для ручного запуска:"
                echo -e "    ${CYAN}systemctl stop nginx${RESET}"
                echo -e "    ${CYAN}$ACME --issue -d ${DOMAIN} --standalone${RESET}"
                echo -e "    ${CYAN}systemctl start nginx${RESET}"
                systemctl start nginx >> "$LOG_FILE" 2>/dev/null || true
                exit 1
            fi
        fi
    fi

    # Install / re-install cert to SSL_DIR
    print_info "Копирование сертификата в $SSL_DIR..."
    "$ACME" --install-cert -d "${DOMAIN}" \
        --key-file  "$SSL_KEY" \
        --fullchain-file "$SSL_CERT" \
        --reloadcmd "systemctl reload nginx" \
        >> "$LOG_FILE" 2>&1
    print_ok "Сертификат скопирован в $SSL_DIR"

    chmod 600 "$SSL_KEY" "$SSL_CERT"

    # Restart nginx
    systemctl start nginx >> "$LOG_FILE" 2>&1 || true

    print_ok "SSL-сертификат установлен: $SSL_CERT"
    print_info "acme.sh настроит автообновление через cron"
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 7b — Configure nginx
# ──────────────────────────────────────────────────────────────────────────────
step_configure_nginx() {
    print_step "Настройка nginx"

    if [[ "$SKIP_SSL" == true ]]; then
        # HTTP-only config (no certificate)
        cat > "$NGINX_CONF" << EOF
server {
    listen 80;
    server_name ${DOMAIN};

    location /bitrix/bot {
        proxy_pass http://127.0.0.1:8081/bitrix/bot;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /monitor {
        proxy_pass http://127.0.0.1:8082;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
        proxy_buffering off;
    }

    location /health {
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }
}
EOF
        print_warn "nginx настроен на HTTP (без SSL). Для добавления HTTPS выполните: sudo bash ${INSTALL_DIR}/install.sh --renew-ssl"
    else
        # HTTPS config
        cat > "$NGINX_CONF" << EOF
# HTTP → HTTPS redirect
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

# HTTPS
server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     ${SSL_CERT};
    ssl_certificate_key ${SSL_KEY};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    location /bitrix/bot {
        proxy_pass http://127.0.0.1:8081/bitrix/bot;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location /monitor {
        proxy_pass http://127.0.0.1:8082;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
        proxy_buffering off;
    }

    location /health {
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }
}
EOF
    fi

    ln -sf "$NGINX_CONF" "$NGINX_LINK"

    if nginx -t >> "$LOG_FILE" 2>&1; then
        run_cmd systemctl reload nginx
        if [[ "$SKIP_SSL" == true ]]; then
            print_ok "nginx настроен и перезагружен (HTTP на порту 80)"
        else
            print_ok "nginx настроен и перезагружен (HTTPS на порту 443)"
        fi
    else
        print_error "Ошибка в конфиге nginx. Проверьте: $LOG_FILE"
        exit 1
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 8 — Create and enable systemd services
# ──────────────────────────────────────────────────────────────────────────────
step_create_services() {
    print_step "Создание systemd-сервисов"

    # 1 — main mirror process
    cat > /etc/systemd/system/bitrix-telegram-mirror.service << EOF
[Unit]
Description=Telegram Bitrix Mirror Bot
After=network.target

[Service]
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV}/bin/python ${INSTALL_DIR}/main.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # 2 — webhook handler
    cat > /etc/systemd/system/bitrix-bot.service << EOF
[Unit]
Description=Bitrix Bot Webhook Handler
After=network.target

[Service]
User=root
WorkingDirectory=${INSTALL_DIR}/server-side
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV}/bin/uvicorn app:app --host 127.0.0.1 --port 8081
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # 3 — monitoring dashboard
    cat > /etc/systemd/system/bitrix-monitor.service << EOF
[Unit]
Description=Bitrix Bot Monitoring Dashboard
After=network.target

[Service]
User=root
WorkingDirectory=${INSTALL_DIR}/server-side
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV}/bin/uvicorn monitor_app:app --host 127.0.0.1 --port 8082
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    run_cmd systemctl daemon-reload
    for svc in "${SERVICES[@]}"; do
        run_cmd systemctl enable "$svc"
        print_ok "Сервис $svc включён в автозапуск"
    done

    for svc in "${SERVICES[@]}"; do
        run_cmd systemctl start "$svc"
        print_ok "Сервис $svc запущен"
    done

    sleep 3
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 9 — Chat mapping setup
# ──────────────────────────────────────────────────────────────────────────────
step_chat_mapping() {
    print_step "Настройка маппинга чатов Telegram ↔ Bitrix"

    # Create DB and chat_mappings table (idempotent)
    sqlite3 "$DB_FILE" << 'SQL'
CREATE TABLE IF NOT EXISTS cursor_state (
    bitrix_dialog_id TEXT PRIMARY KEY,
    last_seen_bitrix_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS message_links (
    telegram_chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    bitrix_message_id INTEGER NOT NULL UNIQUE,
    origin TEXT NOT NULL,
    telegram_message_date_unix INTEGER,
    bitrix_author_id INTEGER,
    last_seen_bitrix_revision TEXT NOT NULL,
    created_at_unix INTEGER NOT NULL,
    updated_at_unix INTEGER NOT NULL,
    bitrix_liked_by_bot INTEGER DEFAULT 0,
    last_seen_bitrix_likes TEXT DEFAULT '',
    PRIMARY KEY (telegram_chat_id, telegram_message_id)
);
CREATE TABLE IF NOT EXISTS chat_mappings (
    tg_chat_id INTEGER PRIMARY KEY,
    bitrix_dialog_id TEXT NOT NULL,
    label TEXT DEFAULT '',
    created_at_unix INTEGER NOT NULL
);
SQL

    local counter=1
    while true; do
        echo ""
        echo -en "  ${YELLOW}Добавить маппинг чата? (y/N): ${RESET}"
        read -r answer
        [[ "${answer,,}" != "y" ]] && break

        # Telegram chat ID
        local tg_id=""
        while true; do
            ask_input tg_id "ID чата Telegram (например: -1001234567890)"
            if [[ "$tg_id" =~ ^-?[0-9]+$ ]]; then
                break
            fi
            print_error "ID должен быть числом (может быть отрицательным)"
        done

        # Bitrix dialog ID
        local bx_id=""
        ask_input bx_id "ID диалога Bitrix (например: chat2941 или sg123)"

        # Label
        local label=""
        ask_optional label "Метка для этого маппинга"

        # Insert into DB
        sqlite3 "$DB_FILE" \
            "INSERT OR REPLACE INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix) \
             VALUES ($tg_id, '$(echo "$bx_id" | sed "s/'/''/g")', '$(echo "$label" | sed "s/'/''/g")', $(date +%s));"

        # Append to .env
        echo "CHAT_MAPPING_${counter}=${tg_id}:${bx_id}" >> "$ENV_FILE"

        print_ok "Маппинг #${counter} добавлен: Telegram ${tg_id} → Bitrix ${bx_id}"
        (( counter++ ))
    done

    if [[ $counter -eq 1 ]]; then
        print_warn "Маппинги не добавлены. Добавьте их вручную позднее в $ENV_FILE"
    fi

    # Restart services to pick up new env/db
    if [[ $counter -gt 1 ]]; then
        print_info "Перезапускаем сервисы для применения маппингов..."
        for svc in "${SERVICES[@]}"; do
            systemctl restart "$svc" >> "$LOG_FILE" 2>&1 || true
        done
        sleep 2
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# STEP 10 — Health checks
# ──────────────────────────────────────────────────────────────────────────────
step_health_checks() {
    print_step "Проверка работоспособности"

    local all_ok=true

    # Service status
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc"; then
            print_ok "Сервис $svc активен"
        else
            print_error "Сервис $svc не запущен"
            print_info "Журнал: journalctl -u $svc -n 50 --no-pager"
            all_ok=false
        fi
    done

    # Webhook /health endpoint
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8081/health 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
        print_ok "Webhook-сервис отвечает на /health (HTTP $code)"
    else
        print_error "Webhook-сервис не отвечает на /health (HTTP $code)"
        all_ok=false
    fi

    # Webhook /bitrix/bot endpoint (POST empty body → 200)
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        -X POST http://127.0.0.1:8081/bitrix/bot \
        -H "Content-Type: application/json" \
        -d '{}' 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
        print_ok "Endpoint /bitrix/bot доступен (HTTP $code)"
    else
        print_error "Endpoint /bitrix/bot недоступен (HTTP $code)"
        all_ok=false
    fi

    # Monitor dashboard (прямое обращение к upstream, минуя nginx)
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        -u "admin:${MONITOR_PASSWORD}" \
        http://127.0.0.1:8082/monitor 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
        print_ok "Мониторинг-дашборд доступен (HTTP $code)"
    else
        print_error "Мониторинг-дашборд недоступен (HTTP $code)"
        all_ok=false
    fi

    # nginx HTTPS proxy check (-k для самоподписного сертификата)
    code=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 \
        --resolve "${DOMAIN}:443:127.0.0.1" \
        "https://${DOMAIN}/health" 2>/dev/null || echo "000")
    if [[ "$code" == "200" ]]; then
        print_ok "nginx HTTPS проксирует запросы (HTTP $code)"
    else
        print_warn "nginx HTTPS: /health вернул HTTP $code (возможно ещё не готов)"
    fi

    # SQLite check
    local count
    count=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM chat_mappings;" 2>/dev/null || echo "ошибка")
    print_ok "SQLite доступна, маппингов в БД: $count"

    echo ""
    if $all_ok; then
        print_ok "Все проверки пройдены успешно"
    else
        print_warn "Некоторые проверки не прошли. Подробности: $LOG_FILE"
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
print_summary() {
    echo ""
    echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════╗"
    echo -e "║              Установка завершена!                        ║"
    echo -e "╚══════════════════════════════════════════════════════════╝${RESET}"
    echo ""
    echo -e "${BOLD}  Пути:${RESET}"
    echo -e "    Установка:      ${CYAN}${INSTALL_DIR}${RESET}"
    echo -e "    Конфиг:         ${CYAN}${ENV_FILE}${RESET}"
    echo -e "    База данных:    ${CYAN}${DB_FILE}${RESET}"
    echo -e "    Лог установки:  ${CYAN}${LOG_FILE}${RESET}"
    echo -e "    nginx-конфиг:   ${CYAN}${NGINX_CONF}${RESET}"
    echo ""
    echo -e "${BOLD}  URL:${RESET}"
    echo -e "    Обработчик бота:   ${CYAN}https://${DOMAIN}/bitrix/bot${RESET}"
    echo -e "    Мониторинг:        ${CYAN}https://${DOMAIN}/monitor${RESET}"
    echo -e "    Логин/пароль:      admin / ***"
    echo ""
    echo -e "${BOLD}  Управление сервисами:${RESET}"
    for svc in "${SERVICES[@]}"; do
        echo -e "    ${CYAN}systemctl status|restart|stop ${svc}${RESET}"
    done
    echo ""
    echo -e "${BOLD}  Логи:${RESET}"
    for svc in "${SERVICES[@]}"; do
        echo -e "    ${CYAN}journalctl -u ${svc} -f${RESET}"
    done
    echo ""
    echo -e "${YELLOW}${BOLD}  ⚠  Не забудьте зарегистрировать бота в Битрикс!${RESET}"
    echo -e "  URL для поля handler_url при вызове imbot.register:"
    echo -e "    ${CYAN}https://${DOMAIN}/bitrix/bot${RESET}"
    echo ""
    echo -e "  Обновление:    ${CYAN}sudo bash ${INSTALL_DIR}/install.sh --update${RESET}"
    echo -e "  Удаление:      ${CYAN}sudo bash ${INSTALL_DIR}/install.sh --uninstall${RESET}"
    echo ""
}

# ──────────────────────────────────────────────────────────────────────────────
# --update mode
# ──────────────────────────────────────────────────────────────────────────────
do_update() {
    banner
    check_root
    print_step "Обновление бота"

    if [[ ! -d "$INSTALL_DIR/.git" ]]; then
        print_error "Репозиторий не найден в $INSTALL_DIR. Запустите полную установку."
        exit 1
    fi

    run_cmd git -C "$INSTALL_DIR" pull
    run_cmd "$VENV/bin/pip" install --upgrade pip
    run_cmd "$VENV/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

    run_cmd systemctl daemon-reload
    for svc in "${SERVICES[@]}"; do
        run_cmd systemctl restart "$svc"
        print_ok "$svc перезапущен"
    done

    sleep 2
    echo ""
    for svc in "${SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc"; then
            print_ok "$svc активен"
        else
            print_error "$svc не запущен — проверьте: journalctl -u $svc -n 50"
        fi
    done

    print_ok "Обновление завершено"
}

# ──────────────────────────────────────────────────────────────────────────────
# --uninstall mode
# ──────────────────────────────────────────────────────────────────────────────
do_uninstall() {
    banner
    check_root

    echo -e "${RED}${BOLD}Это действие удалит бота и все его данные!${RESET}"
    echo -en "${YELLOW}Вы уверены? Введите 'yes' для подтверждения: ${RESET}"
    read -r confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Отменено."
        exit 0
    fi

    print_step "Удаление бота"

    for svc in "${SERVICES[@]}"; do
        systemctl stop "$svc" 2>/dev/null || true
        systemctl disable "$svc" 2>/dev/null || true
        rm -f "/etc/systemd/system/${svc}.service"
        print_ok "Сервис $svc удалён"
    done
    systemctl daemon-reload

    rm -f "$NGINX_LINK"
    rm -f "$NGINX_CONF"
    rm -rf "$SSL_DIR"
    nginx -t >> "$LOG_FILE" 2>&1 && systemctl reload nginx || true
    print_ok "nginx-конфиг и SSL-сертификаты удалены"

    rm -rf "$INSTALL_DIR"
    print_ok "Файлы бота удалены ($INSTALL_DIR)"

    print_ok "Удаление завершено"
}

# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────
main() {
    # Ensure log file exists
    mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"

    case "${1-}" in
        --update)
            do_update
            ;;
        --uninstall)
            do_uninstall
            ;;
        "")
            banner
            check_root

            step_update_system
            step_install_packages
            step_clone_repo
            step_python_deps
            step_collect_config
            step_write_env
            step_setup_ssl
            step_configure_nginx
            step_create_services
            step_chat_mapping
            step_health_checks
            print_summary
            ;;
        *)
            echo "Использование: $0 [--update | --uninstall]"
            exit 1
            ;;
    esac
}

main "$@"
