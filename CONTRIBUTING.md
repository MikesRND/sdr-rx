# Contributing

See [README.md](README.md) for setup, prerequisites, and usage.
See [DESIGN.md](DESIGN.md) for DSP signal path, state machine, threading model, file structure, and API endpoints.
See [DESIGN-UI.md](DESIGN-UI.md) for settings modal UI architecture, data flow, and control locking rules.

## Architecture

Multi-channel receiver: one RTL-SDR dongle feeds N parallel demodulation chains (max 2 per receiver). Each channel has independent squelch, RSSI, recording, and state machine. Four thread types orchestrated from `main.py`:

1. **GNU Radio thread** (`gr_engine.py`): RTL-SDR source → N × (freq_xlating_filter → squelch → NBFM → {voice → AudioTapBlock, DCSDecoder}). DCS taps raw NBFM output before voice filtering. One `ReceiverFlowgraph` with per-channel block sets. RSSI probe taps pre-squelch per channel.

2. **App Core threads** (`app_core.py`): One per channel. 10 Hz poll loop driving a state machine (IDLE → CARRIER_DETECTED → TX_ACTIVE → TX_ENDING → IDLE). Each AppCore receives its `channel_id` and calls per-channel flowgraph methods (`get_rssi(channel_id)`, etc.). Each AppCore also runs a **finalize worker thread** that handles WAV writing, sox filtering, and disk cleanup off the poll thread (see Recording below).

3. **Finalize worker threads** (`app_core.py`): One per channel. Dequeues `(job_id, filename, raw_audio)` from bounded queue, writes WAV, runs sox filter, cleans up disk. Keeps poll thread responsive (see Recording Finalization below).

4. **FastAPI/uvicorn thread** (`web_server.py`): Per-channel WebSocket endpoints for telemetry (`/ws/{ch}`) and live audio (`/audio/{ch}/live`). Per-channel REST endpoints for transmissions, config, and audio files. Config CRUD endpoints for channel database and settings management. One telemetry broadcaster task per channel.

### Key Concepts

- **Receiver**: Frozen dataclass (`config.Receiver`) representing physical SDR hardware properties (sample_rate, max_channels, gains, device_index). `DEFAULT_RECEIVER = Receiver()` in `config.py`.
- **ChannelStack**: Groups a channel's components (app_core, audio_tap, dcs_decoder, telemetry_queue, paths) — defined in `main.py`.
- **ChannelConfig**: Flowgraph-level channel config (channel_id, freq_hz, squelch_threshold, audio_tap, dcs_decoder) — defined in `gr_engine.py`.

Cross-thread communication:
- **Telemetry**: One `queue.Queue` per channel from AppCore → FastAPI (sentinel `None` = shutdown)
- **Live audio**: Per-channel `AudioTapBlock` callback → per-channel `broadcast_audio` closure → `loop.call_soon_threadsafe()` into asyncio event loop
- **Shutdown**: `threading.Event` + signal handlers, graceful then force on second SIGINT

## Initialization Order (main.py)

1. Load `config.yaml` via `load_config()` — returns seeded defaults (30 FRS/GMRS channels) if file missing
2. Build CLI overrides dict (gain, squelch, record, etc.) using Click's `ctx.get_parameter_source()`
3. Determine channels: CLI `-c` flags override `startup_channels` from config
4. Validate and resolve each channel from config's channel catalog
5. Create `AudioTapBlock` + `DCSDecoderBlock` per channel
6. Build list of `ChannelConfig` for the flowgraph
7. Create single `ReceiverFlowgraph(channels=[...], receiver=receiver, gain=gain)`
8. Create `AppCore` per channel (each gets same flowgraph ref + its `channel_id`)
9. Create FastAPI app via `create_app(channel_stacks, flowgraph, config_dir=..., receiver=..., cli_overrides=...)`
10. Wire each `audio_tap._live_callback = web_app.broadcast_audio[channel_id]`
11. Start flowgraph → all app_cores → uvicorn
12. Main thread waits on `shutdown_event`

## Configuration

### Channel Database (`config.yaml`)

All channels are defined in a single `~/.config/sdr-rx/config.yaml` file. On first run with no config file, seeded defaults (30 FRS/GMRS channels) are loaded in memory. The file is only written when the user saves from the settings UI.

Structure:
```yaml
channels:
  frs1: { name: "FRS/GMRS 1", freq_hz: 462562500, dcs_code: 0, dcs_mode: advisory, squelch: -45.0 }
  ...

startup_channels: [frs1]

settings:
  gain: 40
  default_squelch: -45.0
  audio_preset: conservative
  ...
```

### Channel Selection

`startup_channels` in config determines which channels to monitor on startup. CLI `-c` flags override if provided.

- Channel IDs must match `^[a-zA-Z0-9_-]+$`
- Max channels per receiver: 2 (configurable via `Receiver.max_channels`)
- Bandwidth validation: selected channels must fit within `sample_rate - CHANNEL_RATE` (216 kHz)

### CLI Override Tracking

CLI flags (--gain, --squelch, etc.) override config values for the current run only. Tracked via `cli_overrides` dict passed to `create_app()`. The `/api/runtime` endpoint reports each setting's `source` ("default"/"config"/"cli") and `locked` state. CLI-locked fields are read-only in the UI.

### Running Channel Restrictions

Running channels cannot have `freq_hz`, `dcs_code`, or `dcs_mode` changed (409 Conflict). `name` and `squelch` can be edited — squelch applies live, name takes effect on restart.

### DCS Role

DCS is **metadata enrichment only** — transmissions are always detected and recorded based on carrier squelch. `dcs_mode: advisory` records DCS match status silently; `strict` adds a `dcs_mismatch` flag for non-matching TXs. Neither mode drops audio.

## Key Constants (config.py)

- **Receiver**: `DEFAULT_RECEIVER.sample_rate=240k`, `.max_channels=2`, `.if_gain=20`, `.bb_gain=20`
- DSP: `CHANNEL_RATE=24k`, `AUDIO_RATE=8k`, `DECIMATION=10`
- Defaults: `FREQ_HZ=462_562_500` (template default), `RF_GAIN=40` (app default)
- Squelch: `SQUELCH_THRESHOLD_DB=-45.0`, `SQUELCH_ALPHA=0.001`, `SQUELCH_RAMP=240`
- DCS: `DCS_BITRATE=134.4 bps`, `DCS_GOLAY_POLY=0xAE3`, `DCS_CONSECUTIVE_MATCHES=2`
- Timing: `POLL_RATE_HZ=10`, `CARRIER_DETECT_POLLS=2`, `TX_ENDING_TIMEOUT_S=2.0`
- Audio: `CHUNK_SAMPLES=2000` (int16), `RING_SIZE=8` (~2s pre-trigger)
- Disk: `MAX_AUDIO_MB=500` (auto-cleanup of oldest WAVs)

## Recording Finalization

When a transmission ends, `_finalize_tx()` splits work between the poll thread and a background finalize worker:

- **Synchronous (poll thread):** `audio_tap.stop_recording()`, counter updates, TX log append, CSV write. This takes microseconds so the poll loop stays responsive.
- **Asynchronous (finalize worker):** `write_wav()`, `apply_sox_filter()`, `cleanup_audio()`. Queued via bounded `queue.Queue(maxsize=4)`.

Key design decisions:
- **Job ID keying:** Each TX gets a unique job ID (`timestamp_microseconds_sequence`). Cancellation and filenames are keyed by job ID, not filename, to avoid collisions under burst traffic.
- **Backpressure:** When the finalize queue is full, the overflow job writes a raw WAV inline (no sox) and increments `finalize_dropped`. When backlog is near capacity, sox is skipped for queued jobs too.
- **Delete/cancel race:** `delete_tx()` calls `cancel_finalize(job_id)` which adds the job ID to a canceled set. The finalize worker checks this set before writing (skips entirely) and after writing (removes the file). The inline write path also checks cancellation after completing.
- **Shutdown:** `stop()` sends a sentinel to the finalize queue with a non-blocking retry loop (5s timeout), then joins the worker thread (15s timeout).
- **Telemetry:** `finalize_pending` is included in every telemetry frame for observability.

## Testing

Smoke tests: `python test_dual_channel.py` (no hardware needed, uses mocks).
Finalize tests: `python test_finalize.py` (no hardware needed, tests delete race, backpressure, shutdown drain).
Standalone flowgraph test: `python gr_engine.py` (requires RTL-SDR).
