"""Constants and configuration defaults for SDR monitor."""

import dataclasses
import os
import re
import sys
import tempfile

import yaml


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
        data_root=data_root,
        channel_data_dir=channel_data_dir,
        audio_dir=os.path.join(channel_data_dir, "audio"),
    )


# ── Default Channels (30 FRS/GMRS) ────────────────────
DEFAULT_CHANNELS = {
    # FRS/GMRS 1-7 (462 MHz, shared simplex)
    "frs1":  {"name": "FRS/GMRS 1",  "freq_hz": 462562500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs2":  {"name": "FRS/GMRS 2",  "freq_hz": 462587500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs3":  {"name": "FRS/GMRS 3",  "freq_hz": 462612500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs4":  {"name": "FRS/GMRS 4",  "freq_hz": 462637500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs5":  {"name": "FRS/GMRS 5",  "freq_hz": 462662500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs6":  {"name": "FRS/GMRS 6",  "freq_hz": 462687500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs7":  {"name": "FRS/GMRS 7",  "freq_hz": 462712500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    # FRS 8-14 (467 MHz, FRS-only)
    "frs8":  {"name": "FRS 8",  "freq_hz": 467562500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs9":  {"name": "FRS 9",  "freq_hz": 467587500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs10": {"name": "FRS 10", "freq_hz": 467612500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs11": {"name": "FRS 11", "freq_hz": 467637500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs12": {"name": "FRS 12", "freq_hz": 467662500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs13": {"name": "FRS 13", "freq_hz": 467687500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs14": {"name": "FRS 14", "freq_hz": 467712500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    # FRS/GMRS 15-22 (462 MHz, shared simplex)
    "frs15": {"name": "FRS/GMRS 15", "freq_hz": 462550000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs16": {"name": "FRS/GMRS 16", "freq_hz": 462575000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs17": {"name": "FRS/GMRS 17", "freq_hz": 462600000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs18": {"name": "FRS/GMRS 18", "freq_hz": 462625000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs19": {"name": "FRS/GMRS 19", "freq_hz": 462650000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs20": {"name": "FRS/GMRS 20", "freq_hz": 462675000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs21": {"name": "FRS/GMRS 21", "freq_hz": 462700000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "frs22": {"name": "FRS/GMRS 22", "freq_hz": 462725000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    # GMRS 15R-22R repeater inputs (467 MHz)
    "gmrs15r": {"name": "GMRS 15R Input", "freq_hz": 467550000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs16r": {"name": "GMRS 16R Input", "freq_hz": 467575000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs17r": {"name": "GMRS 17R Input", "freq_hz": 467600000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs18r": {"name": "GMRS 18R Input", "freq_hz": 467625000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs19r": {"name": "GMRS 19R Input", "freq_hz": 467650000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs20r": {"name": "GMRS 20R Input", "freq_hz": 467675000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs21r": {"name": "GMRS 21R Input", "freq_hz": 467700000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
    "gmrs22r": {"name": "GMRS 22R Input", "freq_hz": 467725000, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
}

# ── Settings Schema ────────────────────────────────────
# key → (type, default, min, max, choices)
SETTINGS_SCHEMA = {
    "gain":            {"type": "number", "default": RF_GAIN,              "min": 0,    "max": 50},
    "default_squelch": {"type": "number", "default": SQUELCH_THRESHOLD_DB, "min": -70,  "max": -5},
    "audio_preset":    {"type": "choice", "default": DEFAULT_AUDIO_PRESET, "choices": list(AUDIO_PRESETS.keys())},
    "tau":             {"type": "number", "default": FM_TAU,               "min": 0,    "max": 1},
    "record":          {"type": "bool",   "default": True},
    "max_audio_mb":    {"type": "number", "default": MAX_AUDIO_MB,         "min": 10,   "max": 100000},
    "tx_tail":         {"type": "number", "default": TX_ENDING_TIMEOUT_S,  "min": 0.5,  "max": 30},
    "log_days":        {"type": "number", "default": 7,                    "min": 1,    "max": 365},
}

DEFAULT_SETTINGS = {k: v["default"] for k, v in SETTINGS_SCHEMA.items()}


def _default_config():
    """Return seeded in-memory config with 30 FRS/GMRS channels."""
    import copy
    return {
        "channels": copy.deepcopy(DEFAULT_CHANNELS),
        "startup_channels": ["frs1"],
        "settings": dict(DEFAULT_SETTINGS),
        "_settings_from_file": set(),  # nothing from file when using defaults
    }


def load_config(config_dir):
    """Load config.yaml from config_dir. Returns seeded defaults if file missing."""
    config_path = os.path.join(config_dir, "config.yaml")
    if not os.path.isfile(config_path):
        return _default_config()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"Error: invalid YAML in '{config_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"Error: '{config_path}' must contain a YAML mapping.", file=sys.stderr)
        sys.exit(1)

    # Merge with defaults, tracking which settings came from file
    result = _default_config()
    settings_from_file = set()

    if "channels" in data and isinstance(data["channels"], dict):
        result["channels"] = data["channels"]

    if "startup_channels" in data and isinstance(data["startup_channels"], list):
        result["startup_channels"] = data["startup_channels"]

    if "settings" in data and isinstance(data["settings"], dict):
        for k, v in data["settings"].items():
            if k in SETTINGS_SCHEMA:
                result["settings"][k] = v
                settings_from_file.add(k)

    # Track provenance so runtime endpoint can distinguish config vs default
    result["_settings_from_file"] = settings_from_file

    return result


def save_config(config_dir, data):
    """Atomic write of config.yaml (write .tmp then rename)."""
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.yaml")
    # Strip internal provenance keys before writing
    to_write = {k: v for k, v in data.items() if not k.startswith("_")}
    fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp", prefix="config_")
    try:
        with os.fdopen(fd, 'w') as f:
            yaml.dump(to_write, f, default_flow_style=False, sort_keys=False)
        os.rename(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_channel(cfg):
    """Validate a channel config dict. Returns {field: error_message} map (empty = valid)."""
    errors = {}

    # name
    name = cfg.get("name", "")
    if not isinstance(name, str) or len(name.strip()) == 0:
        errors["name"] = "must be a non-empty string"
    elif len(name) > 64:
        errors["name"] = "must be at most 64 characters"

    # freq_hz
    freq = cfg.get("freq_hz")
    if not isinstance(freq, int) or freq < 1_000_000 or freq > 6_000_000_000:
        errors["freq_hz"] = "must be an integer between 1 MHz and 6 GHz"

    # dcs_code
    code = cfg.get("dcs_code")
    if not isinstance(code, int) or code < 0:
        errors["dcs_code"] = "must be a non-negative integer"
    else:
        padded = f"{code:03d}"
        if len(padded) > 3:
            errors["dcs_code"] = "must be 1-3 digits"
        elif any(ch not in "01234567" for ch in padded):
            errors["dcs_code"] = "digits must be 0-7 (octal)"

    # dcs_mode
    mode = cfg.get("dcs_mode", "advisory")
    if mode not in ("advisory", "strict"):
        errors["dcs_mode"] = "must be 'advisory' or 'strict'"

    # squelch
    squelch = cfg.get("squelch")
    if squelch is not None:
        if not isinstance(squelch, (int, float)):
            errors["squelch"] = "must be a number"
        elif squelch < -70 or squelch > -5:
            errors["squelch"] = "must be between -70 and -5"

    return errors


def validate_settings(settings):
    """Validate a settings dict. Returns {field: error_message} map (empty = valid)."""
    errors = {}
    for key, value in settings.items():
        if key not in SETTINGS_SCHEMA:
            errors[key] = "unknown setting"
            continue

        schema = SETTINGS_SCHEMA[key]
        stype = schema["type"]

        if stype == "number":
            if not isinstance(value, (int, float)):
                errors[key] = "must be a number"
            else:
                if "min" in schema and value < schema["min"]:
                    errors[key] = f"must be at least {schema['min']}"
                elif "max" in schema and value > schema["max"]:
                    errors[key] = f"must be at most {schema['max']}"
        elif stype == "bool":
            if not isinstance(value, bool):
                errors[key] = "must be true or false"
        elif stype == "choice":
            if value not in schema["choices"]:
                errors[key] = f"must be one of: {', '.join(schema['choices'])}"

    return errors
