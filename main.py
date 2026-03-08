#!/usr/bin/env python3
"""SDR Monitor — CLI entry point.

Generic NFM SDR monitor with configurable channel name, frequency,
and DCS code loaded from YAML config file with CLI overrides.
"""

import json
import os
import queue
import signal
import sys
import threading
import time

import click
import numpy as np
import uvicorn
import yaml

from config import (
    FREQ_HZ, RF_GAIN, SQUELCH_THRESHOLD_DB,
    MAX_AUDIO_MB, WEB_HOST, WEB_PORT, TX_ENDING_TIMEOUT_S,
    DEFAULT_AUDIO_PRESET, FM_TAU,
    CHANNEL_NAME, DCS_CODE, DEFAULT_CHANNEL_ID,
    DEFAULT_RSSI_OFFSET, DEFAULT_RSSI_CALIBRATION_TIER,
    CHANNEL_ID_RE,
    resolve_paths, resolve_channel_config, validate_channel_id,
)


def _load_channel_yaml(config_path):
    """Load channel configuration from YAML file.

    Returns a dict with keys: name, freq_hz, dcs_code, dcs_mode.
    Returns empty dict if file is missing (with warning).
    Exits on invalid YAML or invalid values.
    """
    if not os.path.isfile(config_path):
        click.echo(f"Warning: channel config '{config_path}' not found, using defaults.")
        return {}

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        click.echo(f"Error: invalid YAML in '{config_path}': {e}", err=True)
        sys.exit(1)

    if not isinstance(data, dict):
        click.echo(f"Error: '{config_path}' must contain a YAML mapping.", err=True)
        sys.exit(1)

    return data


def _validate_channel_config(cfg):
    """Validate merged channel config. Exits on invalid values."""
    # freq_hz
    freq = cfg.get("freq_hz")
    if not isinstance(freq, int) or freq < 1_000_000 or freq > 6_000_000_000:
        click.echo(f"Error: freq_hz must be an integer between 1 MHz and 6 GHz, got: {freq}", err=True)
        sys.exit(1)

    # dcs_code — decimal representation of octal digits
    code = cfg.get("dcs_code")
    if not isinstance(code, int) or code < 0:
        click.echo(f"Error: dcs_code must be a non-negative integer, got: {code}", err=True)
        sys.exit(1)
    padded = f"{code:03d}"
    if len(padded) > 3:
        click.echo(f"Error: dcs_code must be 1-3 digits, got: {code}", err=True)
        sys.exit(1)
    for ch in padded:
        if ch not in "01234567":
            click.echo(f"Error: dcs_code digits must be 0-7 (octal), got: {code}", err=True)
            sys.exit(1)

    # name
    name = cfg.get("name", "")
    if not isinstance(name, str) or len(name.strip()) == 0:
        click.echo(f"Error: name must be a non-empty string.", err=True)
        sys.exit(1)
    if len(name) > 64:
        click.echo(f"Error: name must be at most 64 characters, got {len(name)}.", err=True)
        sys.exit(1)

    # dcs_mode
    mode = cfg.get("dcs_mode", "advisory")
    if mode not in ("advisory", "strict"):
        click.echo(f"Error: dcs_mode must be 'advisory' or 'strict', got: {mode}", err=True)
        sys.exit(1)


def _load_calibration(paths, freq_hz, gain):
    """Load RSSI calibration from file if it exists and matches current RF settings.

    Returns (rssi_offset, rssi_calibration_tier).
    """
    if not os.path.isfile(paths.calibration_file):
        return (DEFAULT_RSSI_OFFSET, DEFAULT_RSSI_CALIBRATION_TIER)
    try:
        with open(paths.calibration_file) as f:
            cal = json.load(f)
    except Exception as e:
        click.echo(f"Warning: could not load calibration: {e}")
        return (DEFAULT_RSSI_OFFSET, DEFAULT_RSSI_CALIBRATION_TIER)

    # Validate RF settings match
    cal_freq = cal.get("freq_hz")
    cal_gain = cal.get("gain")
    if cal_freq != freq_hz or cal_gain != gain:
        click.echo(f"Warning: ignoring calibration (freq={cal_freq}, gain={cal_gain}) — "
                    f"does not match current settings (freq={freq_hz}, gain={gain})")
        return (DEFAULT_RSSI_OFFSET, DEFAULT_RSSI_CALIBRATION_TIER)

    offset = cal.get("offset", DEFAULT_RSSI_OFFSET)
    tier = cal.get("tier", DEFAULT_RSSI_CALIBRATION_TIER)
    click.echo(f"Loaded RSSI calibration: offset={offset:.1f}, tier={tier}")
    return (offset, tier)


def _run_calibrate(freq, gain, tier, paths):
    """Run RSSI calibration mode."""
    from gr_engine import MonitorFlowgraph

    click.echo(f"RSSI Calibration Mode (tier: {tier})")
    click.echo(f"Frequency: {freq/1e6:.3f} MHz, Gain: {gain}")
    click.echo("Measuring RSSI for 10 seconds (100 samples at 10 Hz)...")

    fg = MonitorFlowgraph(freq=freq, gain=gain)
    fg.start()
    time.sleep(1)  # Let flowgraph settle

    readings = []
    for _ in range(100):
        readings.append(fg.get_rssi())
        time.sleep(0.1)

    fg.stop()
    fg.wait()

    readings = np.array(readings)
    mean_dbfs = float(np.mean(readings))
    std_dbfs = float(np.std(readings))

    click.echo(f"\nMeasured RSSI: {mean_dbfs:.1f} dBFS (std: {std_dbfs:.1f})")

    ref_dbm = click.prompt("Enter reference signal level (dBm)", type=float)
    offset = ref_dbm - mean_dbfs

    click.echo(f"\nComputed offset: {offset:.1f} dB")
    click.echo(f"  Measured dBFS:  {mean_dbfs:.1f}")
    click.echo(f"  Reference dBm:  {ref_dbm:.1f}")
    click.echo(f"  Offset:         {offset:.1f}")

    if click.confirm("Save calibration?"):
        os.makedirs(paths.channel_data_dir, exist_ok=True)
        cal = {
            "offset": offset,
            "tier": tier,
            "measured_dbfs": mean_dbfs,
            "reference_dbm": ref_dbm,
            "freq_hz": freq,
            "gain": gain,
            "timestamp": time.time(),
        }
        with open(paths.calibration_file, "w") as f:
            json.dump(cal, f, indent=2)
        click.echo(f"Calibration saved to {paths.calibration_file}")
    else:
        click.echo("Calibration not saved.")


def _handle_init_channel(channel_id, paths):
    """Create a channel config from template."""
    cid = channel_id or "default"
    validate_channel_id(cid)

    os.makedirs(paths.channels_dir, exist_ok=True)
    dest = os.path.join(paths.channels_dir, f"{cid}.yaml")
    if os.path.exists(dest):
        click.echo(f"Error: {dest} already exists.", err=True)
        sys.exit(1)

    template = os.path.join(os.path.dirname(__file__), "examples", "channel_template.yaml")
    if not os.path.isfile(template):
        click.echo(f"Error: template not found at {template}", err=True)
        sys.exit(1)

    import shutil
    shutil.copy2(template, dest)
    click.echo(f"Created {dest}")
    click.echo(f"Edit it to configure your channel, then run: python main.py --channel {cid}")


def _handle_list_channels(paths):
    """List available channel configs."""
    if not os.path.isdir(paths.channels_dir):
        click.echo(f"No channels directory found at {paths.channels_dir}")
        click.echo("Run: python main.py --init-channel <id>")
        return

    import glob as globmod
    files = sorted(globmod.glob(os.path.join(paths.channels_dir, "*.yaml")))
    if not files:
        click.echo(f"No channel configs found in {paths.channels_dir}")
        click.echo("Run: python main.py --init-channel <id>")
        return

    click.echo(f"Available channels in {paths.channels_dir}:")
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        click.echo(f"  {stem}")


@click.command()
@click.option("--config", "config_file", default=None,
              help="Path to channel YAML config file.")
@click.option("--channel", "channel_id_cli", default=None, type=str,
              help="Channel ID (loads from ~/.config/sdr-rx/channels/<id>.yaml).")
@click.option("--freq", default=None, type=int,
              help="Override channel frequency (Hz).")
@click.option("--dcs-code", default=None, type=int,
              help="Override DCS code (octal digits as decimal, e.g. 565).")
@click.option("--name", "channel_name_cli", default=None, type=str,
              help="Override channel display name.")
@click.option("--gain", "-g", default=RF_GAIN, show_default=True,
              help="RTL-SDR tuner gain (dB).")
@click.option("--squelch", "-s", default=SQUELCH_THRESHOLD_DB, show_default=True,
              type=float, help="RF power squelch threshold (dB).")
@click.option("--record/--no-record", "-r/-R", default=True, show_default=True,
              help="Record transmissions to WAV files.")
@click.option("--max-audio-mb", default=MAX_AUDIO_MB, show_default=True,
              help="Max audio folder size in MB.")
@click.option("--port", "-p", default=WEB_PORT, show_default=True,
              help="Web dashboard port.")
@click.option("--host", default=WEB_HOST, show_default=True,
              help="Web dashboard bind address.")
@click.option("--tx-tail", default=TX_ENDING_TIMEOUT_S, show_default=True,
              type=float, help="Seconds of squelch-closed before ending TX (merges short gaps).")
@click.option("--audio-preset", type=click.Choice(["conservative", "aggressive", "flat"]),
              default=DEFAULT_AUDIO_PRESET, show_default=True,
              help="Voice audio processing preset.")
@click.option("--tau", default=None, type=float,
              help="FM de-emphasis time constant (seconds). 0=none (LMR default), 75e-6=broadcast FM. Omit to use default.")
@click.option("--calibrate-rssi", type=click.Choice(["field", "absolute"]),
              default=None, help="Run RSSI calibration mode.")
@click.option("--cal-freq", default=None, type=int,
              help="Frequency for calibration (Hz). Defaults to channel frequency.")
@click.option("--data-dir", default=None, type=str,
              help="Override data directory (default: ~/.local/share/sdr-rx).")
@click.option("--list-channels", is_flag=True, default=False,
              help="List available channel configs and exit.")
@click.option("--init-channel", "init_channel_id", default=None, type=str, is_eager=True,
              is_flag=False, flag_value="default",
              help="Create a channel config from template and exit. Defaults to 'default'.")
def main(config_file, channel_id_cli, freq, dcs_code, channel_name_cli, gain, squelch,
         record, max_audio_mb, port, host, tx_tail, audio_preset, tau,
         calibrate_rssi, cal_freq, data_dir, list_channels, init_channel_id):
    """SDR Monitor — GNU Radio + Web Dashboard.

    \b
    Monitors a configurable NFM channel using RTL-SDR with
    RF power squelch, DCS decoding, and a web dashboard.
    Channel settings are loaded from a YAML config file
    and can be overridden via CLI options.

    \b
    Examples:
      python main.py                          # start with built-in defaults (FRS Ch 1)
      python main.py --channel my_channel     # use named channel config
      python main.py -g 30 -s -25             # custom gain and squelch
      python main.py --no-record              # monitor only, no recording
      python main.py --init-channel myradio   # create new channel config
      python main.py --list-channels          # show available channels
      python main.py --calibrate-rssi field   # calibrate RSSI
    """

    # ── Short-circuit commands (no hardware needed) ──
    # Resolve paths early for init/list (use temp channel ID)
    pre_paths = resolve_paths(data_dir_override=data_dir)

    if init_channel_id is not None:
        # --init-channel with empty string means use default
        _handle_init_channel(init_channel_id or None, pre_paths)
        return

    if list_channels:
        _handle_list_channels(pre_paths)
        return

    # ── Resolve channel config and ID ──
    config_file_resolved, channel_id = resolve_channel_config(
        config_path=config_file,
        channel_id=channel_id_cli,
        channels_dir=pre_paths.channels_dir,
    )

    # ── Resolve final paths with channel ID ──
    paths = resolve_paths(data_dir_override=data_dir, channel_id=channel_id)

    # ── Build merged channel config: defaults < YAML < CLI ──
    channel_cfg = {
        "name": CHANNEL_NAME,
        "freq_hz": FREQ_HZ,
        "dcs_code": DCS_CODE,
        "dcs_mode": "advisory",
    }

    # Layer 2: YAML overrides
    if config_file_resolved:
        yaml_data = _load_channel_yaml(config_file_resolved)
        for key in ("name", "freq_hz", "dcs_code", "dcs_mode"):
            if key in yaml_data:
                channel_cfg[key] = yaml_data[key]
    else:
        click.echo(f"No channel config found. Using built-in defaults (FRS Ch 1).")
        click.echo(f"  Tip: run 'python main.py --init-channel' to create a config.")

    # Layer 3: CLI overrides
    if freq is not None:
        channel_cfg["freq_hz"] = freq
    if dcs_code is not None:
        channel_cfg["dcs_code"] = dcs_code
    if channel_name_cli is not None:
        channel_cfg["name"] = channel_name_cli

    # Validate
    _validate_channel_config(channel_cfg)

    ch_freq = channel_cfg["freq_hz"]
    ch_dcs = channel_cfg["dcs_code"]
    ch_name = channel_cfg["name"]
    ch_dcs_mode = channel_cfg["dcs_mode"]

    # ── Create data directories ──
    os.makedirs(paths.channel_data_dir, exist_ok=True)
    if record:
        os.makedirs(paths.audio_dir, exist_ok=True)

    # Calibration mode
    if calibrate_rssi:
        _run_calibrate(cal_freq or ch_freq, gain, calibrate_rssi, paths)
        return

    # Load existing calibration
    rssi_offset, rssi_calibration_tier = _load_calibration(paths, ch_freq, gain)

    # Apply tx-tail to config
    import config
    config.TX_ENDING_TIMEOUT_S = tx_tail
    config.TX_ENDING_POLLS = int(tx_tail * config.POLL_RATE_HZ)

    tau_display = f"{tau}" if tau is not None else f"{FM_TAU} (default)"
    click.echo(f"{ch_name} [{channel_id}] starting...")
    click.echo(f"  Freq: {ch_freq/1e6:.3f} MHz | Gain: {gain} | Squelch: {squelch} dB")
    click.echo(f"  DCS: {ch_dcs:03d} ({ch_dcs_mode}) | Record: {'ON' if record else 'OFF'} | TX tail: {tx_tail}s")
    click.echo(f"  Web: http://{host}:{port} | Audio: {audio_preset} preset | tau: {tau_display}")
    click.echo(f"  Data: {paths.channel_data_dir}")

    # Import here to avoid GNU Radio init during calibration-only runs
    click.echo("Loading GNU Radio modules...")
    from gr_engine import MonitorFlowgraph
    from dcs_decoder import DCSDecoderBlock
    from audio_tap import AudioTapBlock
    from app_core import AppCore
    from web_server import create_app

    # Telemetry queue: app_core → web_server
    telemetry_queue = queue.Queue(maxsize=50)

    # Create components
    click.echo("Building flowgraph...")
    dcs_decoder = DCSDecoderBlock(dcs_code=ch_dcs)
    audio_tap = AudioTapBlock()

    flowgraph = MonitorFlowgraph(
        freq=ch_freq,
        gain=gain,
        squelch_threshold=squelch,
        audio_tap=audio_tap,
        dcs_decoder=dcs_decoder,
        audio_preset=audio_preset,
        tau=tau,
    )

    app_core = AppCore(
        flowgraph=flowgraph,
        audio_tap=audio_tap,
        dcs_decoder=dcs_decoder,
        telemetry_queue=telemetry_queue,
        record=record,
        max_audio_mb=max_audio_mb,
        channel_name=ch_name,
        dcs_code=ch_dcs,
        dcs_mode=ch_dcs_mode,
        paths=paths,
        rssi_offset=rssi_offset,
        rssi_calibration_tier=rssi_calibration_tier,
    )

    web_app = create_app(telemetry_queue, app_core, flowgraph,
                         channel_info=channel_cfg, paths=paths)

    # Wire live audio callback
    audio_tap._live_callback = web_app.broadcast_audio

    # Shutdown coordination
    shutdown_event = threading.Event()
    _shutting_down = False

    def signal_handler(sig, frame):
        nonlocal _shutting_down
        if _shutting_down:
            # Second signal — force exit
            click.echo("\nForce exit.")
            os._exit(1)
        _shutting_down = True
        click.echo("\nShutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start GNU Radio flowgraph (Thread 1)
    click.echo("Starting GNU Radio flowgraph...")
    flowgraph.start()

    # Start app core polling (Thread 2)
    click.echo("Starting state machine...")
    app_core.start()

    # Start web server (Thread 3)
    click.echo(f"Starting web dashboard on http://{host}:{port}")
    uvicorn_config = uvicorn.Config(
        web_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    web_thread = threading.Thread(target=server.run, daemon=True)
    web_thread.start()

    click.echo("Monitor running. Press Ctrl+C to stop. (Ctrl+C again to force)")

    # Main thread: wait for shutdown signal
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    # Shutdown sequence — each step has a timeout to prevent hanging
    click.echo("Stopping app core...")
    app_core.stop()

    click.echo("Sending telemetry sentinel...")
    telemetry_queue.put(None)

    click.echo("Stopping web server...")
    server.should_exit = True
    web_thread.join(timeout=3)

    click.echo("Stopping GNU Radio flowgraph...")
    flowgraph.stop()
    flowgraph.wait()

    click.echo("Shutdown complete.")


if __name__ == "__main__":
    main()
