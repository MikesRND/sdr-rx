# Contributing

See [README.md](README.md) for setup, prerequisites, and usage.
See [DESIGN.md](DESIGN.md) for DSP signal path, state machine, threading model, file structure, and API endpoints.

## Architecture

Three threads orchestrated from `main.py`:

1. **GNU Radio thread** (`gr_engine.py`): RTL-SDR → channel filter (240k→24k) → squelch → NBFM demod (24k→8k) → voice processing → AudioTapBlock. RSSI probe taps pre-squelch. DCS decoder taps raw NBFM output (before voice filtering, to preserve 146 Hz tone).

2. **App Core thread** (`app_core.py`): 10 Hz poll loop driving a state machine (IDLE → CARRIER_DETECTED → TX_ACTIVE → TX_ENDING → IDLE). Orchestrates recording start/stop, CSV logging, telemetry queue.

3. **FastAPI/uvicorn thread** (`web_server.py`): WebSocket endpoints for telemetry (`/ws`) and live audio (`/audio/live`). REST endpoints for transmission log, channel info, and runtime config changes.

Cross-thread communication:
- **Telemetry**: `queue.Queue` from AppCore → FastAPI (sentinel `None` = shutdown)
- **Live audio**: `AudioTapBlock` callback → `loop.call_soon_threadsafe()` into asyncio event loop
- **Shutdown**: `threading.Event` + signal handlers, graceful then force on second SIGINT

## Initialization Order (main.py)

1. Resolve paths (XDG) and channel config → merge with CLI overrides
2. Load RSSI calibration JSON (if exists and freq/gain match)
3. Create `DCSDecoderBlock` and `AudioTapBlock` instances
4. Create `MonitorFlowgraph` with decoder + tap blocks
5. Create `AppCore` with flowgraph + blocks + telemetry queue
6. Create FastAPI app via `create_app()` with queue + app_core + flowgraph
7. Wire `audio_tap._live_callback = web_app.broadcast_audio`
8. Start flowgraph → app_core → uvicorn (each in own thread)
9. Main thread waits on `shutdown_event`

## Configuration Internals

### Channel Selection Precedence

`--config /path` > `--channel <id>` > `default.yaml` > built-in defaults (FRS Ch 1)

- `--config` and `--channel` cannot be used together (hard error)
- `--channel <id>` loads `~/.config/sdr-rx/channels/<id>.yaml` (hard error if missing)
- `--config /path` loads the file directly; channel ID is derived from the filename stem
- Channel IDs must match `^[a-zA-Z0-9_-]+$`

### DCS Role

DCS is **metadata enrichment only** — transmissions are always detected and recorded based on carrier squelch. `dcs_mode: advisory` records DCS match status silently; `strict` adds a `dcs_mismatch` flag for non-matching TXs. Neither mode drops audio.

## Key Constants (config.py)

- Sample rates: `SDR_SAMPLE_RATE=240k`, `CHANNEL_RATE=24k`, `AUDIO_RATE=8k`
- Defaults: `FREQ_HZ=462_562_500` (FRS Ch 1), `DCS_CODE=0`, `CHANNEL_NAME="FRS Ch 1"`
- Squelch: `SQUELCH_THRESHOLD_DB=-45.0`, `SQUELCH_ALPHA=0.001`, `SQUELCH_RAMP=240`
- DCS: `DCS_BITRATE=134.4 bps`, `DCS_GOLAY_POLY=0xAE3`, `DCS_CONSECUTIVE_MATCHES=2`
- Timing: `POLL_RATE_HZ=10`, `CARRIER_DETECT_POLLS=2`, `TX_ENDING_TIMEOUT_S=2.0`
- Audio: `CHUNK_SAMPLES=2000` (int16), `RING_SIZE=8` (~2s pre-trigger)
- Disk: `MAX_AUDIO_MB=500` (auto-cleanup of oldest WAVs)

## Testing

There are no tests or linting configured. The flowgraph module has a standalone test mode: `python gr_engine.py`.
