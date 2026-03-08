"""Constants and configuration defaults for SDR monitor."""

import dataclasses
import os
import re
import sys


# ── Application Identity ──────────────────────────────
APP_NAME = "sdr-rx"


# ── Receiver (physical SDR hardware) ──────────────────
@dataclasses.dataclass(frozen=True)
class Receiver:
    """Physical SDR hardware properties."""
    device_index: int = 0
    sample_rate: int = 240_000         # ADC capture rate (Hz)
    max_channels: int = 2              # Max simultaneous channels
    if_gain: int = 20                  # IF gain (dB) — R820T2 hardware stage
    bb_gain: int = 20                  # Baseband gain (dB) — R820T2 hardware stage


DEFAULT_RECEIVER = Receiver()


# ── DSP Chain (application design choices) ────────────
CHANNEL_RATE = 24_000             # After decimation (Hz)
AUDIO_RATE = 8_000                # After NBFM demod (Hz)
DECIMATION = DEFAULT_RECEIVER.sample_rate // CHANNEL_RATE  # 10

# ── RF Defaults ────────────────────────────────────────
FREQ_HZ = 462_562_500             # Default frequency: FRS Ch 1 (template default)
RF_GAIN = 40                     # Default tuner gain (dB) — application default

# ── Squelch ─────────────────────────────────────────────
SQUELCH_THRESHOLD_DB = -45.0     # RF power squelch threshold (dB)
SQUELCH_ALPHA = 0.001            # Squelch smoothing factor
SQUELCH_RAMP = 240               # Ramp samples for squelch transitions

# ── RSSI ────────────────────────────────────────────────
RSSI_AVERAGE_LEN = 1000          # Moving average length for RSSI

# ── DCS (Digital Coded Squelch) ─────────────────────────
DCS_CODE = 0                     # Default DCS code: none (overridden by YAML/CLI)
DCS_BITRATE = 134.4              # bits per second
DCS_GOLAY_POLY = 0xAE3           # Golay generator polynomial
DCS_CONSECUTIVE_MATCHES = 2      # Required consecutive matches before declaring detected

# ── Channel ─────────────────────────────────────────────
CHANNEL_NAME = "FRS Ch 1"        # Default channel name (overridden by YAML/CLI)
DEFAULT_CHANNEL_ID = "frs_ch_1"  # Default channel ID for data directory

# ── Audio Voice Processing ─────────────────────────────
# Presets define the voice path filtering and AGC parameters.
# Applied in order: DC blocker → HPF → LPF → AGC (optional) → headroom → float_to_short
AUDIO_PRESETS = {
    "conservative": {
        "hpf_cutoff": 280,         # Hz — DCS at 146 Hz well below
        "hpf_transition": 80,      # Hz
        "lpf_cutoff": 3200,        # Hz — keeps upper voice harmonics
        "lpf_transition": 500,     # Hz
        "agc_enabled": True,
        "agc_attack": 1e-2,        # Fast enough to catch loud bursts
        "agc_decay": 1e-3,         # Slow decay avoids pumping
        "agc_reference": 0.25,     # Target output level
        "agc_gain": 1.0,           # Initial gain
        "agc_max_gain": 6.0,       # Low max gain — less pumping risk
        "headroom": 0.85,          # Multiply before int16 to prevent clip
    },
    "aggressive": {
        "hpf_cutoff": 320,         # Hz — tighter low cut
        "hpf_transition": 80,
        "lpf_cutoff": 2800,        # Hz — narrower passband, less hiss
        "lpf_transition": 500,
        "agc_enabled": True,
        "agc_attack": 1e-2,
        "agc_decay": 2e-3,         # Faster decay — more leveling
        "agc_reference": 0.25,
        "agc_gain": 1.0,
        "agc_max_gain": 10.0,      # Higher max — louder weak signals
        "headroom": 0.85,
    },
    "flat": {
        "hpf_cutoff": 250,         # Hz — minimal filtering
        "hpf_transition": 50,
        "lpf_cutoff": 3400,        # Hz — wide passband
        "lpf_transition": 500,
        "agc_enabled": False,      # No AGC — raw levels
        "agc_attack": 0,
        "agc_decay": 0,
        "agc_reference": 0,
        "agc_gain": 0,
        "agc_max_gain": 0,
        "headroom": 0.85,
    },
}
DEFAULT_AUDIO_PRESET = "conservative"
FM_TAU = 0                         # De-emphasis time constant (0=none for LMR)

# ── Audio / Recording ──────────────────────────────────
CHUNK_DURATION = 0.25          # seconds per audio chunk
CHUNK_SAMPLES = int(AUDIO_RATE * CHUNK_DURATION)  # 2000 samples
RING_SIZE = 8                  # Pre-trigger buffer chunks (~2s)

# ── State Machine Timing ───────────────────────────────
POLL_RATE_HZ = 10              # State machine poll rate
CARRIER_DETECT_POLLS = 2       # Consecutive squelch-open polls for carrier detect
DCS_INITIAL_WINDOW_MS = 500    # Initial DCS confirmation window
TX_ENDING_TIMEOUT_S = 2.0      # Squelch-closed duration before finalizing TX
TX_ENDING_POLLS = int(TX_ENDING_TIMEOUT_S * POLL_RATE_HZ)  # 20 polls

# ── Disk ───────────────────────────────────────────────
MAX_AUDIO_MB = 500

# ── Web Server ──────────────────────────────────────────
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
TELEMETRY_RATE_HZ = 5          # WebSocket telemetry push rate

# ── Channel ID Validation ──────────────────────────────
CHANNEL_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def sanitize_name(name):
    """Convert a channel name to a safe filesystem prefix.

    Lowercases, replaces non-alphanumeric with _, collapses runs,
    strips edges, truncates to 32 chars. Falls back to 'monitor'.
    """
    s = name.lower()
    s = re.sub(r'[^a-z0-9]+', '_', s)
    s = s.strip('_')
    s = s[:32]
    return s if s else "monitor"


def validate_channel_id(channel_id, source=""):
    """Validate a channel ID against the allowed pattern.

    Returns the ID unchanged on success, calls sys.exit on failure.
    """
    if not CHANNEL_ID_RE.match(channel_id):
        hint = ""
        if source == "config_stem":
            hint = " Tip: rename the file, or use --channel <id> instead of --config."
        print(f"Error: invalid channel ID '{channel_id}' — "
              f"must match [a-zA-Z0-9_-]+.{hint}", file=sys.stderr)
        sys.exit(1)
    return channel_id


@dataclasses.dataclass(frozen=True)
class ResolvedPaths:
    """Resolved filesystem paths for config and data storage."""
    config_dir: str        # ~/.config/sdr-rx
    channels_dir: str      # ~/.config/sdr-rx/channels
    data_root: str         # ~/.local/share/sdr-rx
    channel_data_dir: str  # data_root/<channel_id>
    audio_dir: str         # channel_data_dir/audio


def resolve_paths(data_dir_override=None, channel_id=None):
    """Resolve all paths using XDG conventions.

    Precedence for data_root: data_dir_override > SDR_RX_DATA_DIR env > XDG_DATA_HOME/sdr-rx
    Config is always XDG_CONFIG_HOME/sdr-rx.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    config_dir = os.path.join(xdg_config, APP_NAME)
    channels_dir = os.path.join(config_dir, "channels")

    if data_dir_override:
        data_root = data_dir_override
    elif os.environ.get("SDR_RX_DATA_DIR"):
        data_root = os.environ["SDR_RX_DATA_DIR"]
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        data_root = os.path.join(xdg_data, APP_NAME)

    cid = channel_id or DEFAULT_CHANNEL_ID
    channel_data_dir = os.path.join(data_root, cid)

    return ResolvedPaths(
        config_dir=config_dir,
        channels_dir=channels_dir,
        data_root=data_root,
        channel_data_dir=channel_data_dir,
        audio_dir=os.path.join(channel_data_dir, "audio"),
    )


def resolve_channel_configs(channel_ids, channels_dir, max_channels=None):
    """Resolve multiple channel IDs to (id, path) pairs.

    Validates: no duplicates, all IDs valid, all files exist,
    count within max_channels.
    Calls sys.exit on any error.
    Returns list of (channel_id, config_path) tuples.
    """
    if max_channels is None:
        max_channels = DEFAULT_RECEIVER.max_channels

    if len(channel_ids) < 1:
        print("Error: at least one -c/--channel is required.", file=sys.stderr)
        print("  Tip: python main.py -c <channel_id>", file=sys.stderr)
        print("  Run 'python main.py --list-channels' to see available channels.", file=sys.stderr)
        sys.exit(1)

    if len(channel_ids) > max_channels:
        print(f"Error: too many channels ({len(channel_ids)}). "
              f"Max {max_channels} per receiver.", file=sys.stderr)
        sys.exit(1)

    # Check duplicates
    seen = set()
    for cid in channel_ids:
        if cid in seen:
            print(f"Error: duplicate channel ID '{cid}'.", file=sys.stderr)
            sys.exit(1)
        seen.add(cid)

    result = []
    for cid in channel_ids:
        validate_channel_id(cid)
        path = os.path.join(channels_dir, f"{cid}.yaml")
        if not os.path.isfile(path):
            print(f"Error: channel '{cid}' not found at {path}", file=sys.stderr)
            sys.exit(1)
        result.append((cid, path))

    return result
