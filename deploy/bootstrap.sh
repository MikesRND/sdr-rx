#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────
# SDR-RX Raspberry Pi Bootstrap — fully automated provisioning
#
# Run on a fresh Raspberry Pi OS Lite (Bookworm 64-bit) image:
#   curl -fsSL https://raw.githubusercontent.com/<you>/sdr-rx/main/deploy/bootstrap.sh | sudo bash
#   — or —
#   sudo bash /path/to/bootstrap.sh
#
# What it does:
#   1. Installs system packages (GNU Radio, gr-osmosdr, sox, etc.)
#   2. Creates a dedicated 'sdr' service user
#   3. Clones the repo (or copies from local) and sets up a venv
#   4. Installs RTL-SDR udev rules (no manual blacklisting needed)
#   5. Installs + enables a systemd service
#   6. Applies Pi firmware tuning (gpu_mem, watchdog, USB power)
#   7. Enables a small swap file (safety net for GNU Radio spikes)
#
# Idempotent — safe to re-run for upgrades.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configurable defaults (override via environment) ──
SDR_USER="${SDR_USER:-sdr}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sdr-rx}"
DATA_DIR="${DATA_DIR:-/var/lib/sdr-rx}"
REPO_URL="${REPO_URL:-}"
REPO_BRANCH="${REPO_BRANCH:-main}"
LOCAL_SOURCE="${LOCAL_SOURCE:-}"          # set to a path to copy instead of clone
SWAP_MB="${SWAP_MB:-256}"
DISABLE_RADIOS="${DISABLE_RADIOS:-false}" # set to "true" to disable WiFi+BT
SDR_CHANNELS="${SDR_CHANNELS:-}"         # e.g. "frs1" or "frs1 frs2"
SDR_GAIN="${SDR_GAIN:-}"
SDR_SQUELCH="${SDR_SQUELCH:-}"

# ── Preflight ──
if [[ $EUID -ne 0 ]]; then
    echo "Error: run as root (sudo bash bootstrap.sh)" >&2
    exit 1
fi

if ! grep -qi 'raspberry\|aarch64\|arm' /proc/cpuinfo 2>/dev/null &&
   [[ "$(uname -m)" != "aarch64" ]]; then
    echo "Warning: does not look like a Raspberry Pi. Continuing anyway..."
fi

echo "═══════════════════════════════════════════════════════"
echo "  SDR-RX Raspberry Pi Bootstrap"
echo "═══════════════════════════════════════════════════════"

# ── 1. System packages ──
echo ""
echo "── [1/7] Installing system packages ──"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    gnuradio gr-osmosdr librtlsdr-dev sox \
    python3-venv python3-pip python3-numpy \
    git usbutils jq \
    > /dev/null
echo "  Done."

# ── 2. Service user ──
echo ""
echo "── [2/7] Creating service user '${SDR_USER}' ──"
if id "${SDR_USER}" &>/dev/null; then
    echo "  User '${SDR_USER}' already exists."
else
    useradd --system --create-home --shell /usr/sbin/nologin \
        --groups plugdev,dialout "${SDR_USER}"
    echo "  Created."
fi

# ── 3. Application code ──
echo ""
echo "── [3/7] Installing application to ${INSTALL_DIR} ──"
mkdir -p "${INSTALL_DIR}"

if [[ -n "${LOCAL_SOURCE}" ]]; then
    # Copy from local path (useful when script is inside the repo)
    echo "  Copying from ${LOCAL_SOURCE}..."
    rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
        "${LOCAL_SOURCE}/" "${INSTALL_DIR}/"
elif [[ -n "${REPO_URL}" ]]; then
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        echo "  Pulling latest from ${REPO_BRANCH}..."
        git -C "${INSTALL_DIR}" fetch origin "${REPO_BRANCH}"
        git -C "${INSTALL_DIR}" reset --hard "origin/${REPO_BRANCH}"
    else
        echo "  Cloning ${REPO_URL} (branch: ${REPO_BRANCH})..."
        git clone --branch "${REPO_BRANCH}" --depth 1 \
            "${REPO_URL}" "${INSTALL_DIR}"
    fi
else
    # Auto-detect: if this script lives inside the repo, copy from there
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
    if [[ -f "${REPO_ROOT}/main.py" && -f "${REPO_ROOT}/gr_engine.py" ]]; then
        echo "  Copying from detected repo at ${REPO_ROOT}..."
        rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
            "${REPO_ROOT}/" "${INSTALL_DIR}/"
    else
        echo "Error: no source found. Set REPO_URL or LOCAL_SOURCE." >&2
        exit 1
    fi
fi

# Create venv with system-site-packages (for GNU Radio bindings)
echo "  Setting up Python venv..."
python3 -m venv --system-site-packages "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

# Data directory
mkdir -p "${DATA_DIR}"
chown -R "${SDR_USER}:${SDR_USER}" "${DATA_DIR}"
chown -R "${SDR_USER}:${SDR_USER}" "${INSTALL_DIR}"
echo "  Done."

# ── 4. RTL-SDR udev rules ──
echo ""
echo "── [4/7] Installing RTL-SDR udev rules ──"
UDEV_FILE="/etc/udev/rules.d/20-rtlsdr.rules"
SCRIPT_DIR_UDEV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${INSTALL_DIR}/deploy/20-rtlsdr.rules" ]]; then
    cp "${INSTALL_DIR}/deploy/20-rtlsdr.rules" "${UDEV_FILE}"
elif [[ -f "${SCRIPT_DIR_UDEV}/20-rtlsdr.rules" ]]; then
    cp "${SCRIPT_DIR_UDEV}/20-rtlsdr.rules" "${UDEV_FILE}"
else
    cat > "${UDEV_FILE}" <<'RULES'
# RTL-SDR USB dongle — allow plugdev access, unbind dvb-t driver
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", \
  GROUP="plugdev", MODE="0660", ENV{ID_SOFTWARE_RADIO}="1"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", \
  GROUP="plugdev", MODE="0660", ENV{ID_SOFTWARE_RADIO}="1"
RULES
fi

# Blacklist the kernel DVB-T driver so it doesn't grab the device
BLACKLIST="/etc/modprobe.d/blacklist-rtlsdr.conf"
if [[ ! -f "${BLACKLIST}" ]]; then
    cat > "${BLACKLIST}" <<'BL'
# Prevent kernel DVB-T driver from claiming RTL-SDR dongles
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BL
fi

udevadm control --reload-rules
udevadm trigger
echo "  Done. (unplug/replug dongle if already inserted)"

# ── 5. Systemd service ──
echo ""
echo "── [5/7] Installing systemd service ──"
SERVICE_FILE="/etc/systemd/system/sdr-rx.service"

# Build ExecStart command line
EXEC_CMD="${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/main.py --data-dir ${DATA_DIR}"
if [[ -n "${SDR_CHANNELS}" ]]; then
    for ch in ${SDR_CHANNELS}; do
        EXEC_CMD+=" -c ${ch}"
    done
fi
[[ -n "${SDR_GAIN}" ]]    && EXEC_CMD+=" -g ${SDR_GAIN}"
[[ -n "${SDR_SQUELCH}" ]] && EXEC_CMD+=" -s ${SDR_SQUELCH}"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=SDR-RX NFM Monitor
Documentation=https://github.com/MikesRND/sdr-rx
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SDR_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${EXEC_CMD}
Restart=on-failure
RestartSec=10
# Wait for USB device on boot
ExecStartPre=/bin/sleep 5

# ── Resource limits (Pi 3B+ tuned) ──
MemoryMax=450M
MemoryHigh=350M
CPUQuota=200%

# ── Hardening ──
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${DATA_DIR}
PrivateTmp=true
NoNewPrivileges=true
SupplementaryGroups=plugdev

# ── Watchdog (systemd restarts if stuck) ──
WatchdogSec=60
# GNU Radio can take a moment to initialize
TimeoutStartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable sdr-rx.service
echo "  Installed and enabled. Will start on next boot."
echo "  Manual control: systemctl {start|stop|restart|status} sdr-rx"

# ── 6. Firmware / boot tuning ──
echo ""
echo "── [6/7] Applying Pi firmware tuning ──"
CONFIG_TXT="/boot/firmware/config.txt"
# Fallback for older Pi OS layout
[[ ! -f "${CONFIG_TXT}" ]] && CONFIG_TXT="/boot/config.txt"

if [[ -f "${CONFIG_TXT}" ]]; then
    CHANGED=false

    apply_setting() {
        local key="$1" value="$2" file="$3"
        if grep -q "^${key}=" "${file}" 2>/dev/null; then
            if ! grep -q "^${key}=${value}" "${file}" 2>/dev/null; then
                sed -i "s/^${key}=.*/${key}=${value}/" "${file}"
                CHANGED=true
                echo "  Updated: ${key}=${value}"
            else
                echo "  Already set: ${key}=${value}"
            fi
        else
            echo "${key}=${value}" >> "${file}"
            CHANGED=true
            echo "  Added: ${key}=${value}"
        fi
    }

    # Minimal GPU memory — headless, no display needed
    apply_setting "gpu_mem" "16" "${CONFIG_TXT}"

    # Enable hardware watchdog
    apply_setting "dtparam=watchdog" "on" "${CONFIG_TXT}"

    # Optionally disable wifi + bluetooth to free USB bandwidth and reduce interference
    if [[ "${DISABLE_RADIOS}" == "true" ]]; then
        if ! grep -q "^dtoverlay=disable-wifi" "${CONFIG_TXT}" 2>/dev/null; then
            echo "# SDR-RX: disable radios to reduce interference" >> "${CONFIG_TXT}"
            echo "dtoverlay=disable-wifi" >> "${CONFIG_TXT}"
            echo "dtoverlay=disable-bt" >> "${CONFIG_TXT}"
            CHANGED=true
            echo "  Added: disable-wifi, disable-bt overlays"
        else
            echo "  Already set: wifi/bt disabled"
        fi
    else
        echo "  WiFi/BT kept enabled (set DISABLE_RADIOS=true to disable)"
    fi

    if $CHANGED; then
        echo "  (Changes take effect after reboot)"
    fi
else
    echo "  Warning: config.txt not found, skipping firmware tuning."
fi

# ── 7. Swap file ──
echo ""
echo "── [7/7] Configuring ${SWAP_MB}MB swap ──"
SWAP_FILE="/var/swap-sdr"
if [[ -f "${SWAP_FILE}" ]]; then
    echo "  Swap file already exists."
else
    dd if=/dev/zero of="${SWAP_FILE}" bs=1M count="${SWAP_MB}" status=none
    chmod 600 "${SWAP_FILE}"
    mkswap "${SWAP_FILE}" > /dev/null
    echo "  Created ${SWAP_MB}MB swap file."
fi

if ! swapon --show | grep -q "${SWAP_FILE}"; then
    swapon "${SWAP_FILE}"
fi

if ! grep -q "${SWAP_FILE}" /etc/fstab 2>/dev/null; then
    echo "${SWAP_FILE}  none  swap  sw  0  0" >> /etc/fstab
    echo "  Added to fstab."
fi
echo "  Swap active."

# ── Summary ──
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Bootstrap complete!"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  Install dir:  ${INSTALL_DIR}"
echo "  Data dir:     ${DATA_DIR}"
echo "  Service user: ${SDR_USER}"
echo "  Web UI:       http://<pi-ip>:8080"
echo ""
echo "  Next steps:"
echo "    1. Plug in your RTL-SDR dongle"
echo "    2. Reboot:  sudo reboot"
echo "    3. Check:   sudo systemctl status sdr-rx"
echo "    4. Logs:    sudo journalctl -u sdr-rx -f"
echo ""
echo "  To configure channels before first run:"
echo "    sudo -u ${SDR_USER} ${INSTALL_DIR}/.venv/bin/python \\"
echo "      ${INSTALL_DIR}/main.py -c frs1 --help"
echo ""
echo "  Or override at install time:"
echo "    SDR_CHANNELS='frs1 frs2' SDR_GAIN=30 sudo -E bash bootstrap.sh"
echo ""
