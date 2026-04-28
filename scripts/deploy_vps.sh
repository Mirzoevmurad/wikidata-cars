#!/usr/bin/env bash
# Деплой wikidata-cars (FastAPI + SQLite + еженедельный скрап) на Ubuntu/Debian VPS.
#
# Что делает:
#   1. Ставит system-пакеты (python3-venv, git, ufw, curl).
#   2. Клонирует/обновляет репозиторий в APP_DIR.
#   3. Создаёт venv и ставит зависимости.
#   4. Пишет systemd-юниты: веб-сервис (uvicorn) + oneshot скрапа + timer (вс 03:00 UTC).
#   5. (Опц.) Ставит Caddy и настраивает HTTPS для DOMAIN.
#   6. Если БД ещё нет — запускает первичный скрап в фоне (журнал: systemd).
#
# Запуск под root:
#   curl -fsSL https://raw.githubusercontent.com/Mirzoevmurad/wikidata-cars/main/scripts/deploy_vps.sh -o deploy.sh
#   sudo bash deploy.sh                                 # HTTP, порт 8510
#   sudo DOMAIN=cars.play2go.cloud bash deploy.sh       # HTTPS через Caddy
#
# Веб-сервис читает БД через SQLite at connection-per-request: рестарт веба
# после скрапа НЕ нужен — новые данные видны сразу.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Mirzoevmurad/wikidata-cars.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/wikidata-cars}"
APP_USER="${APP_USER:-cars}"
APP_PORT="${APP_PORT:-8510}"
SERVICE_NAME="${SERVICE_NAME:-wikidata-cars}"
SCRAPE_NAME="${SCRAPE_NAME:-wikidata-cars-scrape}"
DOMAIN="${DOMAIN:-}"

log()  { echo -e "\033[1;34m[deploy]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m  $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите скрипт под root (sudo)."

log "apt update + base deps..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git ufw ca-certificates curl

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    log "создаю пользователя $APP_USER"
    useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if [[ -d "$APP_DIR/.git" ]]; then
    log "обновляю код в $APP_DIR"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "FETCH_HEAD"
else
    log "клонирую $REPO_URL -> $APP_DIR"
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR/data"

log "ставлю python deps в venv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

BIND_ADDR="0.0.0.0"
if [[ -n "$DOMAIN" ]]; then
    BIND_ADDR="127.0.0.1"
fi

log "пишу systemd-юнит /etc/systemd/system/${SERVICE_NAME}.service"
cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=Wikidata Cars web (FastAPI)
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=CARS_DB=${APP_DIR}/data/cars.db
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app \\
    --host ${BIND_ADDR} --port ${APP_PORT} --workers 2
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}/data

[Install]
WantedBy=multi-user.target
UNIT

log "пишу systemd-юнит скрапера /etc/systemd/system/${SCRAPE_NAME}.service"
cat >"/etc/systemd/system/${SCRAPE_NAME}.service" <<UNIT
[Unit]
Description=Wikidata Cars weekly scrape
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/scraper.py --db ${APP_DIR}/data/cars.db
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=6h
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}/data
UNIT

log "пишу таймер /etc/systemd/system/${SCRAPE_NAME}.timer (вс 03:00 UTC)"
cat >"/etc/systemd/system/${SCRAPE_NAME}.timer" <<TIMER
[Unit]
Description=Weekly Wikidata Cars scrape (Sunday 03:00 UTC)

[Timer]
OnCalendar=Sun *-*-* 03:00:00 UTC
Persistent=true
Unit=${SCRAPE_NAME}.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"
systemctl enable --now "${SCRAPE_NAME}.timer"

if ufw status 2>/dev/null | grep -q "Status: active"; then
    if [[ -n "$DOMAIN" ]]; then
        log "ufw: 80/tcp + 443/tcp"
        ufw allow 80/tcp  || true
        ufw allow 443/tcp || true
    else
        log "ufw: ${APP_PORT}/tcp"
        ufw allow "${APP_PORT}/tcp" || true
    fi
fi

if [[ -n "$DOMAIN" ]]; then
    log "ставлю Caddy + HTTPS для ${DOMAIN}"
    if ! command -v caddy >/dev/null 2>&1; then
        apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
        curl -1sSLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sSLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        apt-get update -y
        apt-get install -y caddy
    fi
    cat >/etc/caddy/Caddyfile <<CADDY
${DOMAIN} {
    encode zstd gzip
    reverse_proxy 127.0.0.1:${APP_PORT}
}
CADDY
    systemctl restart caddy
    systemctl enable caddy
fi

if [[ ! -s "${APP_DIR}/data/cars.db" ]]; then
    log "первичный скрап (фоном, логи: journalctl -u ${SCRAPE_NAME}.service -f)"
    systemctl start "${SCRAPE_NAME}.service" --no-block || true
fi

log "готово"
if [[ -n "$DOMAIN" ]]; then
    log "открывай https://${DOMAIN}"
else
    log "открывай http://<IP_VPS>:${APP_PORT}/"
fi
log "логи веба:   journalctl -u ${SERVICE_NAME}.service -f"
log "логи скрапа: journalctl -u ${SCRAPE_NAME}.service -f"
log "таймер:      systemctl list-timers | grep ${SCRAPE_NAME}"
