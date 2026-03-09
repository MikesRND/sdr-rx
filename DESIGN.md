# SDR Monitor — Design Document

## Overview

Multi-channel NFM SDR monitor using GNU Radio + FastAPI web dashboard. One RTL-SDR dongle feeds up to 2 simultaneous demodulation channels, each with independent squelch, recording, and state machine. Channels are defined in a consolidated channel database at `~/.config/sdr-rx/config.yaml`; the `startup_channels` setting determines which channels to monitor by default (overridable via CLI `-c` flags).

Key features:
- Multi-channel monitoring (up to 2 channels per receiver, configurable)
- RF power squelch on raw IQ samples (not audio energy), independent per channel
- DCS/DPL Golay(23,12) tone decoding with dual-polarity detection (per channel)
- RSSI signal level (dBFS) per channel
- Web dashboard with channel selector, live telemetry, audio streaming, and recording management
- DCS match rate tracking and mismatch flagging per channel

## Architecture

```mermaid
flowchart LR
    SDR[RTL-SDR<br/>Blog V3]
    GR[ReceiverFlowgraph<br/>N demod chains]
    APP1[AppCore #1<br/>State Machine]
    APP2[AppCore #2<br/>State Machine]
    WEB[FastAPI<br/>WebSocket]
    UI[Browser<br/>Dashboard]

    SDR -->|IQ samples<br/>240 kHz| GR
    GR -->|squelch/RSSI/DCS<br/>audio per channel| APP1
    GR -->|squelch/RSSI/DCS<br/>audio per channel| APP2
    APP1 -->|telemetry queue| WEB
    APP2 -->|telemetry queue| WEB
    WEB -->|WebSocket per ch<br/>10 Hz telemetry<br/>PCM audio| UI
```

## GNU Radio Flowgraph

One `ReceiverFlowgraph(gr.top_block)` with a single RTL-SDR source and N parallel demod chains. Center frequency is computed as the midpoint of all channel frequencies. Each channel uses a `freq_xlating_fir_filter_ccc` with its frequency offset from center.

```mermaid
flowchart LR
    SDR["RTL-SDR<br/>240 kHz IQ<br/>center = midpoint"]

    XF1["freq_xlating_fir #1<br/>offset=-50kHz"]
    XF2["freq_xlating_fir #2<br/>offset=+50kHz"]

    SQ1["squelch #1"]
    SQ2["squelch #2"]

    NBFM1["nbfm #1"]
    NBFM2["nbfm #2"]

    VOICE1["voice #1"]
    VOICE2["voice #2"]

    TAP1["audio_tap_1"]
    TAP2["audio_tap_2"]

    DCS1["dcs_decoder_1"]
    DCS2["dcs_decoder_2"]

    SDR --> XF1 --> SQ1 --> NBFM1
    SDR --> XF2 --> SQ2 --> NBFM2

    NBFM1 --> VOICE1 --> TAP1
    NBFM1 --> DCS1

    NBFM2 --> VOICE2 --> TAP2
    NBFM2 --> DCS2

    style SDR fill:#2a4a7f,stroke:#4a8af4,color:#fff
    style XF1 fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style XF2 fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style SQ1 fill:#5a2a2a,stroke:#ff4444,color:#fff
    style SQ2 fill:#5a2a2a,stroke:#ff4444,color:#fff
    style NBFM1 fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style NBFM2 fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style VOICE1 fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style VOICE2 fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style TAP1 fill:#2a5a2a,stroke:#44ff44,color:#fff
    style TAP2 fill:#2a5a2a,stroke:#44ff44,color:#fff
    style DCS1 fill:#4a2a5a,stroke:#aa66ff,color:#fff
    style DCS2 fill:#4a2a5a,stroke:#aa66ff,color:#fff
```

Per-channel block chain (each channel gets its own independent set):

```mermaid
flowchart TD
    SRC["<b>osmosdr.source</b><br/>RTL-SDR @ 240 kHz<br/>center = midpoint"]

    CHAN["<b>freq_xlating_fir_filter_ccc</b><br/>LPF cutoff 7.5 kHz<br/>offset = ch.freq - center<br/>decimate ÷10 → 24 kHz"]

    SQ["<b>pwr_squelch_cc</b><br/>per-channel threshold<br/>alpha=0.001, ramp=240<br/>gate=False"]

    NBFM["<b>nbfm_rx</b><br/>quad=24k, audio=8k<br/>max_dev=5 kHz, tau=configurable"]

    DC["<b>dc_blocker_ff</b><br/>len=32"]

    HPF["<b>fir_filter_fff</b><br/>high-pass (preset-dependent)<br/>280-320 Hz"]

    LPF["<b>fir_filter_fff</b><br/>low-pass (preset-dependent)<br/>2800-3200 Hz"]

    AGC["<b>agc2_ff</b><br/>(optional per preset)<br/>slow attack/decay<br/>max_gain 6-10"]

    HEADROOM["<b>multiply_const_ff</b><br/>×0.85 headroom"]

    F2S["<b>float_to_short</b><br/>×32767"]

    TAP["<b>AudioTapBlock</b><br/>ring buffer (2s, always filling)<br/>recording + streaming"]

    DCS["<b>DCSDecoderBlock</b><br/>IIR LPF 250 Hz<br/>134.4 bps clock recovery<br/>Golay(23,12) correlation<br/>dual-polarity N/I"]

    MAG["<b>complex_to_mag_squared</b>"]
    AVG["<b>moving_average_ff</b><br/>len=1000"]
    LOG["<b>nlog10_ff</b><br/>×10 → dBFS"]
    PROBE["<b>probe_signal_f</b><br/>RSSI readout"]

    SRC --> CHAN

    CHAN --> SQ
    SQ --> NBFM
    NBFM --> DC --> HPF --> LPF --> AGC --> HEADROOM --> F2S --> TAP
    NBFM --> DCS

    CHAN --> MAG --> AVG --> LOG --> PROBE

    style SRC fill:#2a4a7f,stroke:#4a8af4,color:#fff
    style CHAN fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style SQ fill:#5a2a2a,stroke:#ff4444,color:#fff
    style NBFM fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style DC fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style HPF fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style LPF fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style AGC fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style HEADROOM fill:#3a1e3a,stroke:#ff66ff,color:#fff
    style F2S fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style TAP fill:#2a5a2a,stroke:#44ff44,color:#fff
    style DCS fill:#4a2a5a,stroke:#aa66ff,color:#fff
    style MAG fill:#3a3a1e,stroke:#ffaa44,color:#fff
    style AVG fill:#3a3a1e,stroke:#ffaa44,color:#fff
    style LOG fill:#3a3a1e,stroke:#ffaa44,color:#fff
    style PROBE fill:#3a3a1e,stroke:#ffaa44,color:#fff
```

### Bandwidth Validation

All channel frequencies must fit within the receiver bandwidth:
`max(freqs) - min(freqs) < receiver.sample_rate - CHANNEL_RATE`
(240k - 24k = 216 kHz usable span)

### Sample Rates

| Stage | Rate | Decimation |
|-------|------|------------|
| RTL-SDR capture | 240 kHz | — |
| Channel filter output | 24 kHz | ÷10 |
| NBFM audio output | 8 kHz | ÷3 |

### DSP Path Design Decisions

**Two branches from NBFM:** The demodulated audio splits into two independent paths immediately after `nbfm_rx`:
- **Branch A (DCS):** Raw demod → `DCSDecoderBlock`. Needs the 146.2 Hz sub-audible DCS tone intact for decoding.
- **Branch B (Voice):** Full processing chain → `AudioTapBlock`. Filters, levels, and cleans the audio for listening and recording.

This split is critical — filtering the DCS tone out before the decoder would break DCS detection. Both paths receive the same demodulated samples; only the voice path is processed.

**Voice processing chain order:**
1. **DC blocker** (`dc_blocker_ff(32)`) — Removes DC offset from NBFM demod. The demodulator can produce small baseline wander from imperfect carrier tracking. A short (32-sample) DC blocker removes this without affecting voice frequencies. Must come first to prevent DC bias from shifting the HPF/LPF operating point.
2. **High-pass filter** (FIR, preset-dependent cutoff 280-320 Hz) — Removes the DCS sub-audible tone at ~134 Hz and any sub-audio rumble. FIR chosen over IIR for linear phase (no group delay distortion on voice). Cutoff is above the DCS tone but below the voice first-formant region (~300-400 Hz).
3. **Low-pass filter** (FIR, preset-dependent cutoff 2800-3200 Hz) — Removes high-frequency hiss, squelch edge artifacts, and out-of-band noise. NFM voice intelligibility is preserved up to ~3 kHz; content above that is noise in this application.
4. **AGC** (`agc2_ff`, optional per preset) — Stabilizes loudness across weak and strong transmissions. Conservative preset uses slow decay (1e-3) and low max_gain (6.0) to minimize "pumping" artifacts on noisy channels. Aggressive preset allows faster decay (2e-3) and higher max_gain (10.0) for more leveling. The `flat` preset disables AGC entirely for raw monitoring.
5. **Headroom limiter** (`multiply_const_ff(0.85)`) — Scales output to 85% before `float_to_short` conversion. Prevents AGC overshoot from causing int16 clipping. The 15% headroom absorbs transient peaks that the AGC hasn't caught yet.
6. **Float to int16** (`float_to_short(1, 32767)`) — Final conversion for the AudioTapBlock.

**Why tau is configurable (default 0):** The NBFM demodulator's `tau` parameter controls FM de-emphasis — a high-frequency rolloff that compensates for pre-emphasis in broadcast FM. LMR systems typically do not use pre-emphasis, so `tau=0` is the default. However, some older LMR systems do apply mild pre-emphasis, and the difference is best judged by ear on actual recordings. The `--tau` CLI option allows A/B testing without code changes. Try `--tau 0` vs `--tau 75e-6` and compare recordings. Implementation note: GNU Radio's `nbfm_rx` does not accept `tau=0` or `tau=None` (divides by tau internally). When tau=0 is requested, the flowgraph substitutes `tau=1e-9`, which places the de-emphasis corner frequency at 1 GHz — effectively a flat filter (<0.01 dB variation across the audio band).

**Why voice filtering is in the flowgraph, not post-processing:** Previously, DCS tone removal was only applied by sox during WAV post-processing — live WebSocket audio was unfiltered. Moving the entire voice processing chain into the GNU Radio flowgraph ensures both live monitoring and recorded files receive identical filtering, AGC, and level management. Sox post-processing still runs on saved WAV files as a safety-net highpass and to convert mono to stereo for playback on both ears.

**Why the RSSI probe taps pre-squelch:** RSSI should reflect actual signal level regardless of whether the squelch gate is open. The probe chain (`complex_to_mag_squared → moving_average → nlog10`) connects to the channel filter output, not the squelch output. This allows the dashboard to show signal level rising toward the threshold before the squelch opens.

### Audio Presets

| Preset | HPF | LPF | AGC | Max Gain | Use Case |
|--------|-----|-----|-----|----------|----------|
| **conservative** (default) | 280 Hz | 3200 Hz | On, slow decay | 6.0 | General monitoring — minimal artifacts |
| **aggressive** | 320 Hz | 2800 Hz | On, faster decay | 10.0 | Noisy channels — tighter filtering, more leveling |
| **flat** | 250 Hz | 3400 Hz | Off | — | Raw monitoring — no AGC, wide passband |

Select via `--audio-preset conservative|aggressive|flat`.

### Squelch

`analog.pwr_squelch_cc` operates on IQ power, not demodulated audio. `gate=False` means it outputs zeros when squelched so all downstream blocks keep running. State is queried via `squelch.unmuted()` at 10 Hz from the app core.

### RSSI

Tapped pre-squelch from the channel filter output:

```
complex_to_mag_squared → moving_average(1000) → nlog10(10) → probe_signal_f
```

Reports dBFS (dB relative to full-scale).

### DCS Decoding

Custom `gr.sync_block` (`DCSDecoderBlock`) on the raw demodulated audio (float, 8 kHz, before HPF). The target DCS code is passed as a constructor parameter — codewords are computed at init time, not at module import.

1. Single-pole IIR LPF isolates sub-300 Hz DCS signal
2. Zero-crossing bit clock recovery at 134.4 bps (~59.5 samples/bit)
3. 23-bit shift register correlates against computed Golay codeword for the configured code
4. Checks both normal and inverted polarities on every shift
5. Requires 2 consecutive matches before declaring detected (non-matches reset counters)
6. Reports polarity per TX: `N`, `I`, or `unknown`

DCS codeword construction (example for code 565):
```
9-bit code (octal 565, reversed) + 3-bit fixed (100) → 12-bit data
12-bit data → Golay polynomial 0xAE3 → 11-bit parity
23-bit codeword = data | (parity << 12)
```

## State Machine

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> CARRIER_DETECTED : squelch open<br/>2+ consecutive polls

    CARRIER_DETECTED --> TX_ACTIVE : 500ms elapsed<br/>(DCS confirmed or not)
    CARRIER_DETECTED --> TX_ENDING : squelch closes

    TX_ACTIVE --> TX_ENDING : squelch closes

    TX_ENDING --> TX_ACTIVE : squelch reopens
    TX_ENDING --> IDLE : squelch closed<br/>configurable (default 2s)

    IDLE --> [*]
```

### State Details

| State | Entry Action | During | Exit |
|-------|-------------|--------|------|
| **IDLE** | — | Poll squelch + RSSI at 10 Hz | — |
| **CARRIER_DETECTED** | Reset DCS decoder, start recording (copy ring buffer as pre-trigger) | Wait up to 500ms for DCS | — |
| **TX_ACTIVE** | — | Record audio, track peak RSSI, check DCS (can retroactively confirm) | — |
| **TX_ENDING** | — | Count continuous squelch-closed polls | Finalize: stop recording + log CSV (sync), enqueue WAV write + sox filter (async worker) |

### Timing

- Poll rate: 10 Hz (100ms intervals)
- Carrier detection: 2 consecutive polls (200ms)
- DCS initial window: 500ms
- TX ending timeout: configurable via `--tx-tail` (default 2.0s, 20 polls)
- Pre-trigger buffer: 2 seconds (8 × 250ms chunks), always filling

### Gap Handling

The ring buffer fills continuously regardless of recording state. When a recording starts, the ring buffer contents are *copied* (not drained) as pre-trigger audio. This means:

- **Gaps < TX tail timeout:** Merged into one continuous recording (squelch reopens during TX_ENDING → back to TX_ACTIVE)
- **Gaps >= TX tail timeout:** Separate recordings, but the next TX always has a full 2-second pre-trigger from the ring buffer — no blind window during finalization

## Threading Model

```mermaid
flowchart TD
    subgraph T1["Thread 1: GNU Radio"]
        GR_SCHED["GR Scheduler<br/><code>top_block.start()</code>"]
        GR_BLOCKS["N demod chains<br/>AudioTapBlock callbacks<br/>DCSDecoder callbacks"]
        GR_SCHED --> GR_BLOCKS
    end

    subgraph T2["Threads 2..N+1: App Cores"]
        POLL["10 Hz poll loop (per channel)"]
        SM["State machine"]
        REC["Recording orchestration<br/>(stop_recording + log)"]
        POLL --> SM --> REC
    end

    subgraph T2B["Threads N+2..2N+1: Finalize Workers"]
        FW["WAV write + sox + cleanup<br/>(one worker per channel)"]
    end

    subgraph T3["Thread 2N+2: FastAPI"]
        UV["uvicorn asyncio loop"]
        TELEM_BC["Telemetry broadcaster<br/>(one task per channel)"]
        WS_HANDLERS["Per-channel WS handlers"]
        UV --> TELEM_BC
        UV --> WS_HANDLERS
    end

    subgraph MAIN["Main Thread"]
        STARTUP["Startup + wiring"]
        SIG["Signal handling"]
        SHUTDOWN["Shutdown sequence"]
        STARTUP --> SIG --> SHUTDOWN
    end

    Q["queue.Queue per channel<br/>(telemetry)"]
    LOOP_REF["loop ref<br/>(per-channel audio broadcast)"]

    T2 -->|bounded queue<br/>(job_id, filename, audio)| T2B
    T2 -->|put| Q
    Q -->|run_in_executor get| T3
    T1 -->|call_soon_threadsafe| LOOP_REF
    LOOP_REF --> T3

    style T1 fill:#1e3a5f,stroke:#4a8af4,color:#fff
    style T2 fill:#2a5a2a,stroke:#44ff44,color:#fff
    style T2B fill:#2a4a2a,stroke:#44cc44,color:#fff
    style T3 fill:#5a2a2a,stroke:#ff4444,color:#fff
    style MAIN fill:#3a3a1e,stroke:#ffaa44,color:#fff
```

### Cross-Thread Communication

- **Telemetry (T2..N → T3):** One `queue.Queue(maxsize=50)` per channel. Each AppCore pushes dicts to its own queue. FastAPI runs one background broadcaster task per channel that does blocking `queue.get()` via `run_in_executor`, then broadcasts to that channel's WebSocket clients. Sentinel `None` signals shutdown.
- **Live audio (T1 → T3):** Per-channel `AudioTapBlock` callback invokes a per-channel `broadcast_audio()` closure which uses `loop.call_soon_threadsafe()` to schedule async sends on the FastAPI event loop. Loop reference captured at FastAPI startup. **Client gating:** each `broadcast_audio()` checks its channel's `audio_client_count` and returns immediately when zero.
- **Recording finalize (T2 → T2B):** Per-channel bounded `queue.Queue(maxsize=4)`. AppCore enqueues `(job_id, filename, raw_audio)` tuples after `stop_recording()`. Finalize worker dequeues and writes WAV + sox. Backpressure: when full, poll thread writes raw WAV inline (no sox). Delete cancellation via job ID set checked pre-write and post-write.
- **Shutdown:** Main thread sets `shutdown_event`, all app cores stop (saving in-progress recordings, draining finalize workers), sentinel `None` pushed to each telemetry queue, uvicorn `should_exit` set, flowgraph stopped. Second SIGINT forces `os._exit(1)`.

## Web Frontend

### Endpoints

All data routes are per-channel, keyed by `{ch}` (channel ID).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard HTML |
| GET | `/api/channels` | List all channels `[{id, name, freq_hz, dcs_code, dcs_mode}, ...]` |
| GET | `/api/channels/{ch}` | Single channel info |
| GET | `/api/channels/{ch}/config` | Per-channel squelch + shared gain (read-only, for slider sync) |
| GET | `/api/channels/{ch}/transmissions` | Per-channel TX log |
| DELETE | `/api/channels/{ch}/transmissions/{idx}` | Per-channel delete |
| GET | `/api/runtime` | Runtime state (active channels, CLI overrides for gain/squelch/etc.) |
| GET | `/api/config` | Persisted config (read full `config.yaml` contents) |
| POST | `/api/config/channels` | Create a new channel in config |
| PUT | `/api/config/channels/{id}` | Update an existing channel in config |
| DELETE | `/api/config/channels/{id}` | Delete a channel from config |
| PUT | `/api/config/settings` | Update persisted settings (gain, squelch, etc.) |
| PUT | `/api/config/startup_channels` | Update the `startup_channels` list in config |
| WS | `/ws/{ch}` | Per-channel telemetry (accepts `squelch_threshold` and `gain` config writes) |
| WS | `/audio/{ch}/live` | Per-channel live PCM audio (4-byte seq + 8 kHz s16le mono chunks) |
| GET | `/audio/{ch}/{filename}` | Per-channel WAV download |

Config writes (squelch threshold, gain) are sent as JSON over the telemetry WebSocket. `squelch_threshold` is per-channel; `gain` is shared (one tuner). `GET /api/channels/{ch}/config` is read-only, used for initial slider sync on page load or channel switch.

### Dashboard

Vanilla HTML/CSS/JS, no frameworks. Dark theme with CSS grid layout. Responsive design for mobile.

Components:
- **Settings modal** (gear icon in header, opens persisted config editor)
- **Channel selector** (tab bar between header and main grid, one tab per channel)
- Status bar with LED indicators (squelch, DCS, recording, connection)
- Signal level meter (color-coded bar with threshold marker)
- Numeric RSSI display (dBFS)
- Squelch threshold and gain sliders (live update via WebSocket)
- Live audio button (Web Audio API with jitter buffer)
- Transmission log table (scrollable, newest first, with play/download/delete per row)
- Log persistence: today's CSV is reloaded on startup

Channel switching reconnects telemetry and audio WebSockets to the new channel's endpoints, refreshes TX log and slider positions, and updates channel info (name, freq, DCS).

### Live Audio

Client-side architecture:
1. WebSocket receives binary frames: 4-byte big-endian sequence number + s16le PCM
2. PCM decoded to Float32Array, pushed to jitter buffer
3. `ScriptProcessorNode` drains buffer for playout
4. Prebuffer gate: waits for 600ms of buffered audio before starting playout
5. On buffer drain, re-enters prebuffer state (inserts silence, no hard stop)
6. Auto-reconnects WebSocket on disconnect without recreating AudioContext (prevents resource leaks)

## File Structure

```
sdr-rx/
├── main.py              # CLI entry point, multi-channel orchestration, ChannelStack
├── gr_engine.py         # ReceiverFlowgraph(gr.top_block), ChannelConfig
├── dcs_decoder.py       # DCSDecoderBlock — configurable DCS Golay decoder
├── audio_tap.py         # AudioTapBlock — ring buffer + recording
├── app_core.py          # State machine, CSV logging, finalize worker (one instance per channel)
├── web_server.py        # FastAPI, per-channel WebSocket/REST endpoints
├── config.py            # DEFAULT_CHANNELS, SETTINGS_SCHEMA, load_config(), save_config(), validate_channel(), validate_settings()
├── recording.py         # WAV writing, sox filter (HPF + mono→stereo), disk cleanup
├── test_dual_channel.py # Smoke tests (no hardware needed)
├── test_finalize.py     # Finalize regression tests (delete race, backpressure, shutdown)
├── requirements.txt     # Python dependencies (includes pyyaml)
├── DESIGN.md            # This file
├── DESIGN-UI.md         # UI design details
└── static/
    ├── index.html       # Dashboard page with channel selector
    ├── app.js           # Multi-channel WebSocket client, channel switching
    └── style.css        # Dark theme, responsive, channel tab styles
```

## CLI Options

```
--channel, -c        Channel ID (repeatable). Overrides startup_channels from
                     config.yaml. Channels must exist in config.yaml.
--data-dir           Override data directory
--gain, -g           RTL-SDR tuner gain in dB (default: from config).
                     CLI value is tracked as an override, reported via /api/runtime.
--squelch, -s        RF power squelch threshold in dB (default: from config).
                     CLI value is tracked as an override, reported via /api/runtime.
--record / --no-record, -r/-R   Record transmissions to WAV (default: on)
--max-audio-mb       Max audio folder size in MB (default: 500)
--port, -p           Web dashboard port (default: 8080)
--host               Web dashboard bind address (default: 0.0.0.0)
--tx-tail            Seconds of squelch-closed before ending TX (default: 2.0)
--audio-preset       Voice processing preset: conservative|aggressive|flat (default: conservative)
--tau                FM de-emphasis time constant in seconds (default: 0, i.e. none)
```

CLI flags like `--gain` and `--squelch` act as runtime overrides: they take precedence over persisted `config.yaml` values for the current session but do not modify the config file. The `/api/runtime` endpoint reports which values are overridden.

Examples:
```
python main.py                                  # monitor startup_channels from config.yaml
python main.py -c ch1 -c ch2                    # override startup_channels
python main.py -c ch1 -c ch2 -g 30 -s -25      # custom gain and squelch overrides
```

## Dependencies

**System packages** (pre-installed):
- GNU Radio 3.10.9, gr-osmosdr, librtlsdr
- sox (audio filtering)
- numpy

**Python (venv with `--system-site-packages`):**
- fastapi, uvicorn, websockets, click
