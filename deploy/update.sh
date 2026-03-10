#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────
# SDR-RX Update — pull latest code, rebuild venv, restart service
#
# Usage:
#   sudo bash /opt/sdr-rx/deploy/update.sh
#
# Also callable from the web dashboard via /api/update.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/sdr-rx}"
LOG_TAG="sdr-rx-update"

log() { echo "[$(date '+%H:%M:%S')] $*"; logger -t "${LOG_TAG}" "$*" 2>/dev/null || true; }

# ── Preflight ──
if [[ $EUID -ne 0 ]]; then
    echo "Error: run as root (sudo bash update.sh)" >&2
    exit 1
fi

if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
    echo "Error: ${INSTALL_DIR}/main.py not found. Is INSTALL_DIR correct?" >&2
    exit 1
fi

log "Starting sdr-rx update..."

# ── Pull latest code ──
if [[ -d "${INSTALL_DIR}/.git" ]]; then
    log "Pulling latest code..."
    cd "${INSTALL_DIR}"

    # Stash any local changes (e.g. config edits in tracked files)
    if ! git diff --quiet 2>/dev/null; then
        log "Stashing local changes..."
        git stash --quiet
    fi

    BEFORE=$(git rev-parse HEAD)
    git pull --ff-only origin 2>&1 | while read -r line; do log "  git: $line"; done
    AFTER=$(git rev-parse HEAD)

    if [[ "${BEFORE}" == "${AFTER}" ]]; then
        log "Already up to date (${BEFORE:0:8})."
    else
        COMMITS=$(git log --oneline "${BEFORE}..${AFTER}" | wc -l)
        log "Updated ${BEFORE:0:8} -> ${AFTER:0:8} (${COMMITS} new commit(s))"
    fi
else
    log "Not a git repo — skipping pull. Copy new files manually or set REPO_URL in bootstrap."
fi

# ── Rebuild venv ──
log "Updating Python dependencies..."
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# ── Fix ownership ──
SDR_USER="${SDR_USER:-sdr}"
if id "${SDR_USER}" &>/dev/null; then
    chown -R "${SDR_USER}:${SDR_USER}" "${INSTALL_DIR}"
fi

# ── Restart service ──
if systemctl is-active --quiet sdr-rx; then
    log "Restarting sdr-rx service..."
    systemctl restart sdr-rx
    sleep 2
    if systemctl is-active --quiet sdr-rx; then
        log "Service restarted successfully."
    else
        log "Warning: service failed to start after update."
        log "Check: journalctl -u sdr-rx -n 30"
        exit 1
    fi
else
    log "Service not running — skipping restart."
    log "Start manually: sudo systemctl start sdr-rx"
fi

log "Update complete."
