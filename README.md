# sdr-rx

Generic NFM SDR monitor/recorder — monitors a configurable narrowband FM radio channel using an RTL-SDR dongle. Provides RF power squelch, DCS/DPL tone decoding, automatic transmission recording, and a real-time web dashboard.

## Architecture

See [DESIGN.md](DESIGN.md) for the full design document with signal flow diagrams, threading model, and DSP details. See [CONTRIBUTING.md](CONTRIBUTING.md) for developer reference.

## Prerequisites

**System packages** (Ubuntu/Debian):

```bash
sudo apt install gnuradio gr-osmosdr librtlsdr-dev sox python3-venv
```

**Hardware**: RTL-SDR dongle (RTL-SDR Blog V3 or compatible).

## Setup

```bash
# Create venv with access to system GNU Radio bindings
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

## Quick Start

```bash
source .venv/bin/activate

# Run with built-in defaults (FRS Channel 1, 462.5625 MHz, no DCS)
python main.py

# Create a named channel config
python main.py --init-channel default
# Edit ~/.config/sdr-rx/channels/default.yaml with your settings, then:
python main.py
```

The web dashboard is at **http://localhost:8080**.

## Channel Configuration

Channel configs live in `~/.config/sdr-rx/channels/` (XDG convention). Create one with `--init-channel`:

```bash
python main.py --init-channel weather       # creates weather.yaml from template
python main.py --channel weather            # run with that channel
```

Each channel YAML looks like:

```yaml
name: "My Channel"       # Display name for the dashboard
freq_hz: 462562500       # Center frequency in Hz
dcs_code: 0              # DCS code (0 = none)
dcs_mode: advisory       # advisory | strict
```

Settings follow three-layer precedence: **built-in defaults** < **YAML config** < **CLI flags**.

## Common Options

```bash
python main.py -g 30 -s -25              # custom gain (dB) and squelch threshold (dB)
python main.py --no-record               # monitor only, no WAV recording
python main.py --audio-preset aggressive  # tighter voice filtering
python main.py --freq 462000000          # override frequency via CLI
python main.py --list-channels           # show available channel configs
python main.py --data-dir /mnt/data      # override data storage location
```

Run `python main.py --help` for all options.

## Data Storage

Per-channel data is stored under `~/.local/share/sdr-rx/<channel_id>/`:

```
~/.local/share/sdr-rx/
  default/
    audio/                    # WAV recordings (auto-cleaned at 500 MB)
    20260308.csv              # Daily transmission log
  weather/
    audio/
    ...
```

Override with `--data-dir` or `SDR_RX_DATA_DIR` environment variable.
