#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

readonly REPO_URL="https://github.com/raebaexxx/tguserbot.git"
readonly APP_DIR="/opt/tguserbot"
readonly DATA_DIR="/var/lib/tguserbot"
readonly CONFIG_DIR="/etc/tguserbot"
readonly SERVICE_USER="tguserbot"

id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd --system --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 700 "${APP_DIR}" "${DATA_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 750 "${CONFIG_DIR}"

if [[ -d "${APP_DIR}/.git" ]]; then
  git -C "${APP_DIR}" pull --ff-only
else
  git clone "${REPO_URL}" "${APP_DIR}"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
fi

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
  "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
fi
if [[ -f "${APP_DIR}/requirements.lock" ]]; then
  (cd "${APP_DIR}" && "${APP_DIR}/.venv/bin/python" -m pip install -r requirements.lock)
else
  "${APP_DIR}/.venv/bin/python" -m pip install -e "${APP_DIR}"
fi

if [[ ! -f "${CONFIG_DIR}/userbot.env" ]]; then
  install -o root -g "${SERVICE_USER}" -m 640 "${APP_DIR}/.env.example" "${CONFIG_DIR}/userbot.env"
  echo "Created ${CONFIG_DIR}/userbot.env; add Telegram credentials before starting." >&2
fi

install -o root -g root -m 644 "${APP_DIR}/deploy/systemd/tguserbot.service" /etc/systemd/system/tguserbot.service
systemctl daemon-reload
systemctl enable tguserbot.service

echo "Install complete. Configure ${CONFIG_DIR}/userbot.env, then run:"
echo "  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python -m userbot auth"
echo "  systemctl start tguserbot"
