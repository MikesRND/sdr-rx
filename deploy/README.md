# Raspberry Pi Deployment

Automated headless deployment for Raspberry Pi 3B+ (also works on Pi 4/5).

## Flash Once, Boot Ready

Two steps on your computer, then just apply power.

### 1. Flash Pi OS with Raspberry Pi Imager

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to write
**Raspberry Pi OS Lite (64-bit, Bookworm)** to a microSD card (16 GB+).

In Imager's settings (gear icon), configure:
- **Hostname**: `sdr-rx`
- **Enable SSH**: password or key-based
- **Username/password**: your login user
- **WiFi**: your SSID + password
- **Locale**: your timezone

**Do not eject the SD card yet.**

### 2. Prepare the SD card

With the SD card still inserted in your computer, run from the repo:

```bash
# Basic — uses default channel config
sudo bash deploy/prepare-sd.sh /dev/sdX

# With channel presets
sudo bash deploy/prepare-sd.sh /dev/sdX --channels "frs1 frs2" --gain 30

# With squelch override
sudo bash deploy/prepare-sd.sh /dev/sdX --channels frs1 --gain 30 --squelch -28

# Ethernet-only (disables WiFi/BT for less RF interference)
sudo bash deploy/prepare-sd.sh /dev/sdX --channels frs1 --disable-radios
```

Replace `/dev/sdX` with your SD card device (`/dev/sdb`, `/dev/mmcblk0`, etc.).
Use `lsblk` to find it.

### 3. Boot

1. Eject the SD card
2. Insert into Pi with RTL-SDR dongle plugged in
3. Apply power

**That's it.** On first boot the Pi will:
- Connect to WiFi
- Install all packages (~10-15 minutes)
- Configure the sdr-rx service
- Reboot automatically

After the second boot, the dashboard is live at: `http://sdr-rx.local:8080`

### Monitor first-boot progress

If you want to watch it work (optional):

```bash
ssh <user>@sdr-rx.local
journalctl -u sdr-rx-firstboot -f
```

---

## Alternative: SSH + Bootstrap

If you prefer to SSH in and run it manually, or if you're on macOS/Windows
where mounting ext4 isn't straightforward:

```bash
ssh <user>@sdr-rx.local
git clone https://github.com/<owner>/sdr-rx.git
cd sdr-rx
SDR_CHANNELS="frs1" SDR_GAIN=30 sudo -E bash deploy/bootstrap.sh
sudo reboot
```

---

## prepare-sd.sh Options

| Option | Description |
|---|---|
| `<device>` | SD card block device (required) |
| `--channels "ch1 ch2"` | Channel IDs to monitor |
| `--gain <dB>` | RTL-SDR tuner gain |
| `--squelch <dB>` | RF squelch threshold |
| `--disable-radios` | Disable WiFi + Bluetooth |

## bootstrap.sh Environment Variables

Override defaults when running `bootstrap.sh` directly:

| Variable | Default | Description |
|---|---|---|
| `SDR_USER` | `sdr` | Service account username |
| `INSTALL_DIR` | `/opt/sdr-rx` | Application install path |
| `DATA_DIR` | `/var/lib/sdr-rx` | Recordings + logs path |
| `REPO_URL` | _(auto-detect)_ | Git repo URL to clone from |
| `REPO_BRANCH` | `main` | Branch to clone |
| `LOCAL_SOURCE` | _(auto-detect)_ | Local path to copy instead of cloning |
| `SWAP_MB` | `256` | Swap file size in MB |
| `DISABLE_RADIOS` | `false` | Set `true` to disable WiFi + Bluetooth |
| `SDR_CHANNELS` | _(from config)_ | Space-separated channel IDs |
| `SDR_GAIN` | _(from config)_ | RTL-SDR tuner gain (dB) |
| `SDR_SQUELCH` | _(from config)_ | RF squelch threshold (dB) |

## What Gets Installed

1. **System packages** — gnuradio, gr-osmosdr, sox, etc.
2. **Service user** — locked-down `sdr` account in `plugdev` group
3. **Application** — `/opt/sdr-rx` with Python venv
4. **Udev rules** — RTL-SDR USB access without root
5. **DVB-T blacklist** — prevents kernel from claiming the dongle
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

Or use the web UI settings modal to change channels and trigger a restart.

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
- If on Ethernet, consider `--disable-radios` to free USB bandwidth (shared bus on Pi 3B+) and reduce RF interference near the SDR antenna
