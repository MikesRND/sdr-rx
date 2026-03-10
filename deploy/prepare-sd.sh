#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────
# SDR-RX SD Card Preparation — flash once, boot ready
#
# Run this on your computer AFTER flashing Pi OS Lite with
# Raspberry Pi Imager (which sets up SSH, WiFi, user, hostname).
#
# Usage:
#   sudo bash deploy/prepare-sd.sh /dev/sdX
#   sudo bash deploy/prepare-sd.sh /dev/sdX --channels "frs1 frs2" --gain 30
#
# What it does:
#   1. Mounts the SD card's rootfs partition
#   2. Copies the sdr-rx repo onto it
#   3. Writes a config file with your channel/gain/squelch settings
#   4. Installs a first-boot systemd service
#   5. On first boot, the Pi provisions itself and reboots into sdr-rx
#
# The Pi will:
#   Boot → connect WiFi → install packages → configure → reboot → running
#   First boot takes ~10-15 min (package downloads). After that, ~30s.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──
CHANNELS=""
GAIN=""
SQUELCH=""
DISABLE_RADIOS="false"
DEVICE=""

# ── Parse arguments ──
usage() {
    cat <<'USAGE'
Usage: sudo bash prepare-sd.sh <device> [options]

Arguments:
  <device>               SD card block device (e.g. /dev/sdb, /dev/mmcblk0)

Options:
  --channels "ch1 ch2"   Channel IDs to monitor (space-separated)
  --gain <dB>            RTL-SDR tuner gain
  --squelch <dB>         RF squelch threshold
  --disable-radios       Disable WiFi + Bluetooth (Ethernet-only deploy)
  --help                 Show this help

Examples:
  sudo bash prepare-sd.sh /dev/sdb
  sudo bash prepare-sd.sh /dev/sdb --channels "frs1 frs2" --gain 30
  sudo bash prepare-sd.sh /dev/mmcblk0 --channels frs1 --squelch -28
USAGE
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --channels)   CHANNELS="$2";       shift 2 ;;
        --gain)       GAIN="$2";           shift 2 ;;
        --squelch)    SQUELCH="$2";        shift 2 ;;
        --disable-radios) DISABLE_RADIOS="true"; shift ;;
        --help|-h)    usage ;;
        -*)           echo "Unknown option: $1" >&2; usage ;;
        *)
            if [[ -z "${DEVICE}" ]]; then
                DEVICE="$1"; shift
            else
                echo "Unexpected argument: $1" >&2; usage
            fi
            ;;
    esac
done

if [[ -z "${DEVICE}" ]]; then
    echo "Error: no device specified." >&2
    echo ""
    usage
fi

# ── Preflight ──
if [[ $EUID -ne 0 ]]; then
    echo "Error: run as root (sudo bash prepare-sd.sh ...)" >&2
    exit 1
fi

# Resolve repo root (script lives in deploy/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

if [[ ! -f "${REPO_ROOT}/main.py" ]]; then
    echo "Error: can't find repo root (expected main.py in ${REPO_ROOT})" >&2
    exit 1
fi

# Determine partition naming (sdb1 vs mmcblk0p1)
if [[ "${DEVICE}" == *mmcblk* || "${DEVICE}" == *nvme* ]]; then
    PART_PREFIX="${DEVICE}p"
else
    PART_PREFIX="${DEVICE}"
fi

BOOT_PART="${PART_PREFIX}1"
ROOT_PART="${PART_PREFIX}2"

for part in "${BOOT_PART}" "${ROOT_PART}"; do
    if [[ ! -b "${part}" ]]; then
        echo "Error: partition ${part} not found." >&2
        echo "  Make sure you've already flashed Pi OS with Raspberry Pi Imager." >&2
        echo "  Expected partitions: ${BOOT_PART} (boot) and ${ROOT_PART} (rootfs)" >&2
        exit 1
    fi
done

# Check the device isn't the system disk
ROOT_DEV="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
if [[ -n "${ROOT_DEV}" && "${ROOT_DEV}" == "${DEVICE}"* ]]; then
    echo "Error: ${DEVICE} appears to be your system disk. Aborting." >&2
    exit 1
fi

echo "═══════════════════════════════════════════════════════"
echo "  SDR-RX SD Card Preparation"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  Device:    ${DEVICE}"
echo "  Channels:  ${CHANNELS:-"(default from config)"}"
echo "  Gain:      ${GAIN:-"(default)"}"
echo "  Squelch:   ${SQUELCH:-"(default)"}"
echo "  Radios:    $(if [[ "${DISABLE_RADIOS}" == "true" ]]; then echo "WiFi/BT disabled"; else echo "WiFi/BT enabled"; fi)"
echo ""

# ── Mount rootfs ──
MOUNT_ROOT="$(mktemp -d /tmp/sdr-rootfs.XXXXXX)"
MOUNT_BOOT="$(mktemp -d /tmp/sdr-boot.XXXXXX)"

cleanup() {
    echo ""
    echo "Cleaning up mounts..."
    umount "${MOUNT_ROOT}" 2>/dev/null || true
    umount "${MOUNT_BOOT}" 2>/dev/null || true
    rmdir "${MOUNT_ROOT}" 2>/dev/null || true
    rmdir "${MOUNT_BOOT}" 2>/dev/null || true
}
trap cleanup EXIT

echo "── Mounting SD card partitions ──"
mount "${ROOT_PART}" "${MOUNT_ROOT}"
mount "${BOOT_PART}" "${MOUNT_BOOT}"
echo "  rootfs: ${MOUNT_ROOT}"
echo "  boot:   ${MOUNT_BOOT}"

# Verify this looks like a Pi OS image
if [[ ! -d "${MOUNT_ROOT}/etc" ]]; then
    echo "Error: rootfs doesn't look like a Linux filesystem." >&2
    exit 1
fi

# ── Copy application ──
echo ""
echo "── Copying sdr-rx to SD card ──"
DEST="${MOUNT_ROOT}/opt/sdr-rx"
mkdir -p "${DEST}"
rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
    "${REPO_ROOT}/" "${DEST}/"
echo "  Copied to ${DEST}"

# ── Write firstboot config ──
echo ""
echo "── Writing firstboot configuration ──"
cat > "${DEST}/.firstboot-env" <<EOF
# SDR-RX firstboot configuration — generated by prepare-sd.sh
SDR_CHANNELS="${CHANNELS}"
SDR_GAIN="${GAIN}"
SDR_SQUELCH="${SQUELCH}"
DISABLE_RADIOS="${DISABLE_RADIOS}"
LOCAL_SOURCE="/opt/sdr-rx"
EOF

# Sentinel file triggers the firstboot service
touch "${DEST}/.firstboot"
echo "  Config written."

# ── Patch bootstrap.sh to read firstboot-env ──
# Add env file sourcing near the top of bootstrap if not already present
if ! grep -q 'firstboot-env' "${DEST}/deploy/bootstrap.sh"; then
    # Insert after "set -euo pipefail"
    sed -i '/^set -euo pipefail$/a\
\
# Source firstboot config if present (written by prepare-sd.sh)\
FIRSTBOOT_ENV="/opt/sdr-rx/.firstboot-env"\
if [[ -f "${FIRSTBOOT_ENV}" ]]; then\
    source "${FIRSTBOOT_ENV}"\
fi' "${DEST}/deploy/bootstrap.sh"
fi

# ── Install firstboot systemd service ──
echo ""
echo "── Installing firstboot service ──"
cp "${DEST}/deploy/sdr-rx-firstboot.service" \
    "${MOUNT_ROOT}/etc/systemd/system/sdr-rx-firstboot.service"

# Enable the service by creating the symlink directly
mkdir -p "${MOUNT_ROOT}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/sdr-rx-firstboot.service \
    "${MOUNT_ROOT}/etc/systemd/system/multi-user.target.wants/sdr-rx-firstboot.service"
echo "  Firstboot service enabled."

# ── Summary ──
echo ""
echo "═══════════════════════════════════════════════════════"
echo "  SD card ready!"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "  1. Eject the SD card safely"
echo "  2. Insert into Pi 3B+ with RTL-SDR dongle plugged in"
echo "  3. Apply power"
echo ""
echo "  First boot will:"
echo "    - Connect to WiFi (configured via Pi Imager)"
echo "    - Install all packages (~10-15 min)"
echo "    - Configure sdr-rx service"
echo "    - Reboot automatically"
echo ""
echo "  After reboot, dashboard is at:"
echo "    http://<hostname>.local:8080"
echo ""
echo "  Monitor first-boot progress (after Pi is on WiFi):"
echo "    ssh <user>@<hostname>.local journalctl -u sdr-rx-firstboot -f"
echo ""
