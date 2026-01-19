#!/usr/bin/env bash
set -euo pipefail

# PrinterPal installer for Debian/Raspberry Pi OS.
# Installs CUPS + Avahi, deploys PrinterPal under /opt/printerpal, and enables a systemd service bound to port 80.

log() { printf '[PrinterPal] %s\n' "$*"; }
die() { printf '[PrinterPal] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF_USAGE'
Usage: ./install.sh [--update]

Options:
  --update    Skip OS package installation and update the app in place.
  --help      Show this help message.
EOF_USAGE
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    die "This installer must be run as root. Try: sudo ./install.sh"
  fi
}

cmd_exists() {
  command -v "$1" >/dev/null 2>&1
}

check_python_module() {
  local module="$1"
  python3 - <<PY 2>/dev/null
import importlib
import sys
try:
    importlib.import_module("${module}")
except Exception:
    sys.exit(1)
PY
}

check_runtime_deps() {
  local missing=()
  local missing_cmds=()
  local modules=(flask gunicorn PIL img2pdf)
  local cmds=(lp lpstat pdfinfo pdftoppm gs)

  for module in "${modules[@]}"; do
    if ! check_python_module "${module}"; then
      missing+=("${module}")
    fi
  done

  for cmd in "${cmds[@]}"; do
    if ! cmd_exists "${cmd}"; then
      missing_cmds+=("${cmd}")
    fi
  done

  if [[ "${#missing[@]}" -gt 0 || "${#missing_cmds[@]}" -gt 0 ]]; then
    printf '[PrinterPal] Missing runtime dependencies detected.\n' >&2
    if [[ "${#missing[@]}" -gt 0 ]]; then
      printf '[PrinterPal] Missing Python modules: %s\n' "${missing[*]}" >&2
    fi
    if [[ "${#missing_cmds[@]}" -gt 0 ]]; then
      printf '[PrinterPal] Missing commands: %s\n' "${missing_cmds[*]}" >&2
    fi
    if [[ "${UPDATE_ONLY}" == "true" ]]; then
      die "Update mode skips package installation. Re-run without --update to install dependencies."
    fi
  fi
}

UPDATE_ONLY=false
for arg in "$@"; do
  case "${arg}" in
    --update)
      UPDATE_ONLY=true
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: ${arg}"
      ;;
  esac
done

require_root

if ! cmd_exists apt-get; then
  die "apt-get not found. This installer supports Debian-family systems only."
fi

# Resolve paths relative to installer location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_USER="printerpal"
APP_GROUP="printerpal"
APP_HOME="/opt/printerpal"

ETC_DIR="/etc/printerpal"
CFG_FILE="${ETC_DIR}/config.json"

DATA_DIR="/var/lib/printerpal"
UPLOAD_DIR="${DATA_DIR}/uploads"
CACHE_DIR="${DATA_DIR}/cache"

ROOT_HELPER_SRC="${SCRIPT_DIR}/scripts/printerpal-root"
ROOT_HELPER_DST="/usr/local/sbin/printerpal-root"

SYSTEMD_UNIT_SRC="${SCRIPT_DIR}/systemd/printerpal.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/printerpal.service"


if [[ "${UPDATE_ONLY}" == "false" ]]; then
  log "Updating package index..."
  apt-get update -y

  log "Installing OS packages..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3 \
    python3-flask \
    python3-gunicorn \
    python3-pil \
    python3-img2pdf \
    cups \
    cups-client \
    avahi-daemon \
    avahi-utils \
    poppler-utils \
    ghostscript \
    sudo \
    rsync
else
  log "Update mode: skipping OS package installation."
fi

if ! cmd_exists python3; then
  die "python3 not installed (unexpected)."
fi

check_runtime_deps

PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
log "Python version: ${PYVER}"

# Ensure group exists.
if ! getent group "${APP_GROUP}" >/dev/null 2>&1; then
  log "Creating system group ${APP_GROUP}..."
  groupadd --system "${APP_GROUP}"
fi

# Create service user.
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  log "Creating system user ${APP_USER}..."
  useradd --system --create-home --home-dir "${APP_HOME}" --shell /usr/sbin/nologin --gid "${APP_GROUP}" "${APP_USER}"
fi

log "Creating directories..."
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${APP_HOME}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0750 "${DATA_DIR}" "${UPLOAD_DIR}" "${CACHE_DIR}"
install -d -o root -g "${APP_GROUP}" -m 0770 "${ETC_DIR}"

log "Deploying application to ${APP_HOME}..."
rsync -a --delete \
  "${SCRIPT_DIR}/" "${APP_HOME}/"
chown -R "${APP_USER}:${APP_GROUP}" "${APP_HOME}"

log "Installing root helper to ${ROOT_HELPER_DST}..."
install -o root -g root -m 0750 "${ROOT_HELPER_SRC}" "${ROOT_HELPER_DST}"

log "Installing sudoers rule for restricted privilege actions..."
SUDOERS_FILE="/etc/sudoers.d/printerpal"
cat > "${SUDOERS_FILE}" <<EOF_SUDO
# Allow PrinterPal service user to run only the PrinterPal root helper actions without a password.
${APP_USER} ALL=(root) NOPASSWD: ${ROOT_HELPER_DST} ensure-airprint, ${ROOT_HELPER_DST} restart-host
EOF_SUDO
chmod 0440 "${SUDOERS_FILE}"

log "Creating default config if needed..."
if [[ ! -f "${CFG_FILE}" ]]; then
  # Generate using the app's default loader.
  log "Generating ${CFG_FILE}..."
  sudo -u "${APP_USER}" -g "${APP_GROUP}" bash -lc \
    "cd '${APP_HOME}' && env PRINTERPAL_CONFIG='${CFG_FILE}' PRINTERPAL_UPLOAD_DIR='${UPLOAD_DIR}' PRINTERPAL_CACHE_DIR='${CACHE_DIR}' python3 -c 'from printerpal.config import ConfigStore; ConfigStore().load()'"

  if [[ ! -f "${CFG_FILE}" ]]; then
    die "Failed to create ${CFG_FILE}"
  fi
fi

# Ensure config ownership allows web UI edits.
chown root:"${APP_GROUP}" "${CFG_FILE}" || true
chmod 0660 "${CFG_FILE}" || true

log "Installing systemd unit..."
install -o root -g root -m 0644 "${SYSTEMD_UNIT_SRC}" "${SYSTEMD_UNIT_DST}"

if [[ ! -f "${SYSTEMD_UNIT_DST}" ]]; then
  die "Failed to install systemd unit to ${SYSTEMD_UNIT_DST}"
fi

log "Reloading systemd..."
systemctl daemon-reload

log "Enabling CUPS + Avahi..."
systemctl enable --now cups
systemctl enable --now avahi-daemon

log "Attempting AirPrint advertising setup..."
"${ROOT_HELPER_DST}" ensure-airprint || true

log "Enabling and starting PrinterPal service..."
systemctl enable --now printerpal

# Smoke check.
log "Verifying service is enabled..."
if ! systemctl is-enabled --quiet printerpal; then
  die "printerpal.service is not enabled. Re-run installer without --update."
fi

log "Verifying service is active..."
if ! systemctl is-active --quiet printerpal; then
  log "printerpal.service is not active. Recent logs:"
  journalctl -u printerpal -n 80 --no-pager || true
  die "PrinterPal did not start successfully."
fi

log "Install complete. Open: http://<pi-ip>/"
log "Service: systemctl status printerpal"
