#!/usr/bin/env python3
"""SDR Monitor — CLI entry point.

Multi-channel NFM SDR monitor with configurable channels loaded
from YAML config files. Monitors up to N channels simultaneously
on a single RTL-SDR receiver.
"""

import dataclasses
import os
import queue
import signal
import sys
import threading
import time

import click
import uvicorn
import yaml

from config import (
    FREQ_HZ, RF_GAIN, SQUELCH_THRESHOLD_DB,
    MAX_AUDIO_MB, WEB_HOST, WEB_PORT, TX_ENDING_TIMEOUT_S,
    DEFAULT_AUDIO_PRESET, FM_TAU,
    CHANNEL_NAME, DCS_CODE,
    DEFAULT_RECEIVER, Receiver, ResolvedPaths,
    resolve_paths, resolve_channel_configs, validate_channel_id,
)


@dataclasses.dataclass
class ChannelStack:
    """All components for one monitored channel."""
    channel_id: str
    channel_info: dict          # name, freq_hz, dcs_code, dcs_mode
    app_core: object            # AppCore
    audio_tap: object           # AudioTapBlock
    dcs_decoder: object         # DCSDecoderBlock
    telemetry_queue: object     # queue.Queue
    paths: ResolvedPaths


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
    click.echo(f"Edit it to configure your channel, then run: python main.py -c {cid}")


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
@click.option("--channel", "-c", "channel_ids", multiple=True, type=str,
              help="Channel ID (loads from ~/.config/sdr-rx/channels/<id>.yaml). Repeatable.")
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
@click.option("--data-dir", default=None, type=str,
              help="Override data directory (default: ~/.local/share/sdr-rx).")
@click.option("--list-channels", is_flag=True, default=False,
              help="List available channel configs and exit.")
@click.option("--init-channel", "init_channel_id", default=None, type=str, is_eager=True,
              is_flag=False, flag_value="default",
              help="Create a channel config from template and exit. Defaults to 'default'.")
def main(channel_ids, gain, squelch,
         record, max_audio_mb, port, host, tx_tail, audio_preset, tau,
         data_dir, list_channels, init_channel_id):
    """SDR Monitor — GNU Radio + Web Dashboard.

    \b
    Monitors configurable NFM channels using RTL-SDR with
    RF power squelch, DCS decoding, and a web dashboard.
    Channel settings are loaded from YAML config files.

    \b
    Examples:
      python main.py -c my_channel                    # monitor one channel
      python main.py -c ch1 -c ch2                    # monitor two channels
      python main.py -c ch1 -c ch2 -g 30 -s -25      # custom gain and squelch
      python main.py --init-channel myradio           # create new channel config
      python main.py --list-channels                  # show available channels
    """

    # ── Short-circuit commands (no hardware needed) ──
    pre_paths = resolve_paths(data_dir_override=data_dir)

    if init_channel_id is not None:
        _handle_init_channel(init_channel_id or None, pre_paths)
        return

    if list_channels:
        _handle_list_channels(pre_paths)
        return

    # ── Run mode: require at least one channel ──
    if not channel_ids:
        click.echo("Error: at least one -c/--channel is required.", err=True)
        click.echo("  Tip: python main.py -c <channel_id>", err=True)
        click.echo("  Run 'python main.py --list-channels' to see available channels.", err=True)
        sys.exit(1)

    # ── Resolve and validate all channels ──
    receiver = DEFAULT_RECEIVER
    resolved = resolve_channel_configs(
        channel_ids, pre_paths.channels_dir, receiver.max_channels,
    )

    # Apply tx-tail to config
    import config
    config.TX_ENDING_TIMEOUT_S = tx_tail
    config.TX_ENDING_POLLS = int(tx_tail * config.POLL_RATE_HZ)

    # ── Load and validate each channel config ──
    channel_cfgs = []  # list of (channel_id, channel_cfg_dict, paths)
    for channel_id, config_path in resolved:
        paths = resolve_paths(data_dir_override=data_dir, channel_id=channel_id)

        channel_cfg = {
            "name": CHANNEL_NAME,
            "freq_hz": FREQ_HZ,
            "dcs_code": DCS_CODE,
            "dcs_mode": "advisory",
        }

        yaml_data = _load_channel_yaml(config_path)
        for key in ("name", "freq_hz", "dcs_code", "dcs_mode"):
            if key in yaml_data:
                channel_cfg[key] = yaml_data[key]

        _validate_channel_config(channel_cfg)

        os.makedirs(paths.channel_data_dir, exist_ok=True)
        if record:
            os.makedirs(paths.audio_dir, exist_ok=True)

        channel_cfgs.append((channel_id, channel_cfg, paths))

    tau_display = f"{tau}" if tau is not None else f"{FM_TAU} (default)"
    click.echo(f"Starting {len(channel_cfgs)} channel(s)...")
    for cid, cfg, paths in channel_cfgs:
        click.echo(f"  [{cid}] {cfg['name']} — {cfg['freq_hz']/1e6:.3f} MHz, DCS {cfg['dcs_code']:03d} ({cfg['dcs_mode']})")
    click.echo(f"  Gain: {gain} | Squelch: {squelch} dB | Record: {'ON' if record else 'OFF'} | TX tail: {tx_tail}s")
    click.echo(f"  Web: http://{host}:{port} | Audio: {audio_preset} preset | tau: {tau_display}")

    click.echo("Loading GNU Radio modules...")
    from gr_engine import ReceiverFlowgraph, ChannelConfig
    from dcs_decoder import DCSDecoderBlock
    from audio_tap import AudioTapBlock
    from app_core import AppCore
    from web_server import create_app

    # ── Build channel stacks ──
    channel_configs = []   # for ReceiverFlowgraph
    channel_stacks = {}    # channel_id → ChannelStack

    for channel_id, channel_cfg, paths in channel_cfgs:
        telem_q = queue.Queue(maxsize=50)
        dcs_decoder = DCSDecoderBlock(dcs_code=channel_cfg["dcs_code"])
        audio_tap = AudioTapBlock()

        channel_configs.append(ChannelConfig(
            channel_id=channel_id,
            freq_hz=channel_cfg["freq_hz"],
            squelch_threshold=squelch,
            audio_tap=audio_tap,
            dcs_decoder=dcs_decoder,
        ))

        channel_stacks[channel_id] = ChannelStack(
            channel_id=channel_id,
            channel_info=channel_cfg,
            app_core=None,  # set after flowgraph creation
            audio_tap=audio_tap,
            dcs_decoder=dcs_decoder,
            telemetry_queue=telem_q,
            paths=paths,
        )

    # ── Create flowgraph ──
    click.echo("Building flowgraph...")
    flowgraph = ReceiverFlowgraph(
        channels=channel_configs,
        receiver=receiver,
        gain=gain,
        audio_preset=audio_preset,
        tau=tau,
    )

    # ── Create AppCore per channel ──
    for channel_id, channel_cfg, paths in channel_cfgs:
        stack = channel_stacks[channel_id]
        app_core = AppCore(
            flowgraph=flowgraph,
            audio_tap=stack.audio_tap,
            dcs_decoder=stack.dcs_decoder,
            telemetry_queue=stack.telemetry_queue,
            record=record,
            max_audio_mb=max_audio_mb,
            channel_name=channel_cfg["name"],
            dcs_code=channel_cfg["dcs_code"],
            dcs_mode=channel_cfg["dcs_mode"],
            paths=paths,
            channel_id=channel_id,
        )
        stack.app_core = app_core

    # ── Create web app ──
    web_app = create_app(channel_stacks, flowgraph)

    # Wire live audio callbacks
    for channel_id, stack in channel_stacks.items():
        stack.audio_tap._live_callback = web_app.broadcast_audio[channel_id]

    # Shutdown coordination
    shutdown_event = threading.Event()
    _shutting_down = False

    def signal_handler(sig, frame):
        nonlocal _shutting_down
        if _shutting_down:
            click.echo("\nForce exit.")
            os._exit(1)
        _shutting_down = True
        click.echo("\nShutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start GNU Radio flowgraph
    click.echo("Starting GNU Radio flowgraph...")
    flowgraph.start()

    # Start app core polling (one thread per channel)
    click.echo("Starting state machines...")
    for stack in channel_stacks.values():
        stack.app_core.start()

    # Start web server
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

    # Shutdown sequence
    click.echo("Stopping app cores...")
    for stack in channel_stacks.values():
        stack.app_core.stop()

    click.echo("Sending telemetry sentinels...")
    for stack in channel_stacks.values():
        stack.telemetry_queue.put(None)

    click.echo("Stopping web server...")
    server.should_exit = True
    web_thread.join(timeout=3)

    click.echo("Stopping GNU Radio flowgraph...")
    flowgraph.stop()
    flowgraph.wait()

    click.echo("Shutdown complete.")


if __name__ == "__main__":
    main()
