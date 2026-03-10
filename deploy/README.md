# Raspberry Pi Deployment

Automated headless deployment for Raspberry Pi 3B+ (also works on Pi 4/5).

## Quick Start

### 1. Flash the SD card

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to write
**Raspberry Pi OS Lite (64-bit, Bookworm)** to a microSD card (16 GB+).

In Imager's settings (gear icon), configure:
- **Hostname**: `sdr-rx` (or your preference)
- **Enable SSH**: password or key-based
- **Username/password**: your login user
- **WiFi**: configure if needed (Ethernet recommended for reliability)
- **Locale**: your timezone

### 2. Boot and connect

Insert the SD card, plug in Ethernet + power, then SSH in:

```bash
ssh <your-user>@sdr-rx.local
```

### 3. Run the bootstrap

**From the repo (if cloned on the Pi):**
```bash
cd sdr-rx
sudo bash deploy/bootstrap.sh
```

**One-liner from GitHub:**
```bash
curl -fsSL https://raw.githubusercontent.com/<owner>/sdr-rx/main/deploy/bootstrap.sh | sudo bash
```

**With channel/gain presets:**
```bash
SDR_CHANNELS="frs1 frs2" SDR_GAIN=30 SDR_SQUELCH=-28 sudo -E bash deploy/bootstrap.sh
```

### 4. Plug in RTL-SDR and reboot

```bash
sudo reboot
```

The service starts automatically. Check status:

```bash
sudo systemctl status sdr-rx
sudo journalctl -u sdr-rx -f
```

Dashboard: `http://sdr-rx.local:8080`

## Environment Variables

Override defaults when running `bootstrap.sh`:

| Variable | Default | Description |
|---|---|---|
| `SDR_USER` | `sdr` | Service account username |
| `INSTALL_DIR` | `/opt/sdr-rx` | Application install path |
| `DATA_DIR` | `/var/lib/sdr-rx` | Recordings + logs path |
| `REPO_URL` | _(auto-detect)_ | Git repo URL to clone from |
| `REPO_BRANCH` | `main` | Branch to clone |
| `LOCAL_SOURCE` | _(auto-detect)_ | Local path to copy instead of cloning |
| `SWAP_MB` | `256` | Swap file size in MB |
| `DISABLE_RADIOS` | `false` | Set `true` to disable WiFi + Bluetooth (use when on Ethernet) |
| `SDR_CHANNELS` | _(from config)_ | Space-separated channel IDs |
| `SDR_GAIN` | _(from config)_ | RTL-SDR tuner gain (dB) |
| `SDR_SQUELCH` | _(from config)_ | RF squelch threshold (dB) |

## What the Bootstrap Does

1. **System packages** — gnuradio, gr-osmosdr, sox, etc.
2. **Service user** — creates a locked-down `sdr` account in `plugdev` group
3. **Application** — clones/copies repo to `/opt/sdr-rx`, creates venv
4. **Udev rules** — grants USB access to RTL-SDR without root
5. **Blacklists DVB-T driver** — prevents kernel from claiming the dongle
6. **Systemd service** — auto-start, watchdog, memory limits, restart-on-failure
7. **Firmware tuning** — `gpu_mem=16`, hardware watchdog, optionally disables WiFi/BT
8. **Swap** — 256 MB safety net for GNU Radio memory spikes

## Managing the Service

```bash
# Status / logs
sudo systemctl status sdr-rx
sudo journalctl -u sdr-rx -f
sudo journalctl -u sdr-rx --since "1 hour ago"

# Restart after config changes
sudo systemctl restart sdr-rx

# Stop
sudo systemctl stop sdr-rx

# Disable auto-start
sudo systemctl disable sdr-rx
```

## Upgrading

Re-run the bootstrap. It's idempotent — pulls latest code, rebuilds venv,
reloads the service:

```bash
cd /opt/sdr-rx
sudo git pull
sudo bash deploy/bootstrap.sh
sudo systemctl restart sdr-rx
```

## Changing Channels

Edit the systemd service `ExecStart` line:

```bash
sudo systemctl edit sdr-rx
```

Add an override:

```ini
[Service]
ExecStart=
ExecStart=/opt/sdr-rx/.venv/bin/python /opt/sdr-rx/main.py --data-dir /var/lib/sdr-rx -c frs1 -c gmrs15 -g 30
```

Then: `sudo systemctl restart sdr-rx`

Or edit `/var/lib/sdr-rx` config and restart (the web UI settings modal also
writes to config.yaml and can trigger a restart).

## Storing Recordings on USB Drive

To save SD card wear and get more storage:

```bash
# Find your USB drive
lsblk

# Mount it (example: /dev/sda1)
sudo mkdir -p /mnt/sdr-data
sudo mount /dev/sda1 /mnt/sdr-data
echo '/dev/sda1 /mnt/sdr-data ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab

# Re-run bootstrap pointing data there
DATA_DIR=/mnt/sdr-data sudo -E bash deploy/bootstrap.sh
sudo systemctl restart sdr-rx
```

## Pi 3B+ Performance Notes

- **1 channel**: comfortable (~40-50% one core for DSP)
- **2 channels**: feasible but leaves little headroom
- **RAM**: ~200-300 MB typical with GNU Radio + Python + FastAPI
- WiFi works fine for the dashboard — the data rates are low (~16 kbit/s audio + telemetry)
- If on Ethernet, consider `DISABLE_RADIOS=true` to free USB bandwidth (shared bus on Pi 3B+) and reduce RF interference near the SDR antenna
