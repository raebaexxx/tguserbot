#!/usr/bin/env bash
# Install tguserbot as a systemd service. Idempotent: safe to re-run.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

readonly REPO_URL="${TGUSERBOT_REPO_URL:-https://github.com/raebaexxx/tguserbot.git}"
readonly APP_DIR="${TGUSERBOT_APP_DIR:-/opt/tguserbot}"
readonly DATA_DIR="${TGUSERBOT_DATA_DIR:-/var/lib/tguserbot}"
readonly CONFIG_DIR="${TGUSERBOT_CONFIG_DIR:-/etc/tguserbot}"
readonly SERVICE_USER="${TGUSERBOT_SERVICE_USER:-tguserbot}"
readonly SERVICE_NAME="tguserbot.service"

log() { printf '[install] %s\n' "$*"; }
fail() { printf '[install] error: %s\n' "$*" >&2; exit 1; }

preflight() {
  local missing=()
  command -v git >/dev/null || missing+=("git")
  command -v python3 >/dev/null || missing+=("python3")
  # Debian/Ubuntu ship venv support in a separate package.
  python3 -c 'import venv' >/dev/null 2>&1 || missing+=("python3-venv")
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    || missing+=("python3 >= 3.12")
  if [[ ${#missing[@]} -gt 0 ]]; then
    fail "missing prerequisites: ${missing[*]}"
    # shellcheck disable=SC2317  # reached indirectly: fail() runs before this
    log "on Debian/Ubuntu: apt-get install -y git python3 python3-venv"
  fi
}

ensure_service_user() {
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    log "creating the system user ${SERVICE_USER}"
    useradd --system --home-dir "${DATA_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
}

ensure_directories() {
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 700 "${APP_DIR}" "${DATA_DIR}"
  install -d -o root -g "${SERVICE_USER}" -m 750 "${CONFIG_DIR}"
  install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 700 "${DATA_DIR}/logs"
}

fetch_code() {
  if [[ -d "${APP_DIR}/.git" ]]; then
    log "updating the existing checkout"
    # The checkout stays root-owned; only the venv and the runtime data belong
    # to the service user. Handing the service user write access to its own
    # code let a compromised bot persist across `tguserbotctl update`.
    git -C "${APP_DIR}" pull --ff-only
  else
    log "cloning ${REPO_URL}"
    git clone "${REPO_URL}" "${APP_DIR}"
  fi
}

install_dependencies() {
  local python="${APP_DIR}/.venv/bin/python"
  if [[ ! -x "${python}" ]]; then
    log "creating the virtualenv"
    runuser -u "${SERVICE_USER}" -- python3 -m venv "${APP_DIR}/.venv"
    runuser -u "${SERVICE_USER}" -- "${python}" -m pip install --upgrade pip
  fi
  # Only the venv is chowned back: the rest of the tree stays root-owned.
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.venv"
  if [[ -f "${APP_DIR}/requirements.lock" ]]; then
    # The lock carries hashes and omits the project itself (a local directory
    # cannot be hashed), so dependencies are verified and the project is
    # installed separately without touching the resolver.
    runuser -u "${SERVICE_USER}" -- "${python}" -m pip install \
      --require-hashes -r "${APP_DIR}/requirements.lock"
    runuser -u "${SERVICE_USER}" -- "${python}" -m pip install --no-deps -e "${APP_DIR}"
  else
    runuser -u "${SERVICE_USER}" -- "${python}" -m pip install -e "${APP_DIR}"
  fi
}

install_configuration() {
  if [[ ! -f "${CONFIG_DIR}/userbot.env" ]]; then
    install -o root -g "${SERVICE_USER}" -m 640 \
      "${APP_DIR}/.env.example" "${CONFIG_DIR}/userbot.env"
    log "created ${CONFIG_DIR}/userbot.env; add the Telegram credentials next"
  else
    log "keeping the existing ${CONFIG_DIR}/userbot.env"
  fi
}

install_unit() {
  install -o root -g root -m 644 \
    "${APP_DIR}/deploy/systemd/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"
  rm -f /usr/local/bin/tguserbotctl
  ln -s "${APP_DIR}/userbotctl" /usr/local/bin/tguserbotctl
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  # A revoked or missing session makes the process exit immediately. Without
  # rate limiting systemd would restart it every 5 seconds forever, spamming
  # the journal and risking a Telegram rate limit on the account.
  systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
}

preflight
ensure_service_user
ensure_directories
fetch_code
install_dependencies
install_configuration
install_unit

log "install complete"
cat <<EOF
Next steps:
  1) sudoedit ${CONFIG_DIR}/userbot.env      # set TGUSERBOT_API_ID / TGUSERBOT_API_HASH
  2) sudo chmod 640 ${CONFIG_DIR}/userbot.env
  3) sudo tguserbotctl first-run            # install, authorize, and start
  4) sudo tguserbotctl status
EOF
