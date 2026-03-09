#!/usr/bin/env python3
"""SDR Monitor — CLI entry point.

Multi-channel NFM SDR monitor. Channels loaded from config.yaml
(consolidated channel database). Monitors up to N channels
simultaneously on a single RTL-SDR receiver.
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

from config import (
    FREQ_HZ, RF_GAIN, SQUELCH_THRESHOLD_DB,
    MAX_AUDIO_MB, WEB_HOST, WEB_PORT, TX_ENDING_TIMEOUT_S,
    DEFAULT_AUDIO_PRESET, FM_TAU,
    CHANNEL_NAME, DCS_CODE, CHANNEL_RATE,
    DEFAULT_RECEIVER, Receiver, ResolvedPaths,
    resolve_paths, validate_channel_id, load_config,
    validate_channel, validate_settings,
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


@click.command()
@click.option("--channel", "-c", "channel_ids", multiple=True, type=str,
              help="Channel ID to monitor (from config.yaml). Repeatable. Overrides startup_channels.")
@click.option("--gain", "-g", default=None, type=float,
              help="RTL-SDR tuner gain (dB). Overrides config.")
@click.option("--squelch", "-s", default=None, type=float,
              help="RF power squelch threshold (dB). Overrides all channels for this run.")
@click.option("--record/--no-record", "-r/-R", default=None,
              help="Record transmissions to WAV files.")
@click.option("--max-audio-mb", default=None, type=int,
              help="Max audio folder size in MB.")
@click.option("--port", "-p", default=WEB_PORT, show_default=True,
              help="Web dashboard port.")
@click.option("--host", default=WEB_HOST, show_default=True,
              help="Web dashboard bind address.")
@click.option("--tx-tail", default=None, type=float,
              help="Seconds of squelch-closed before ending TX.")
@click.option("--audio-preset", type=click.Choice(["conservative", "aggressive", "flat"]),
              default=None, help="Voice audio processing preset.")
@click.option("--tau", default=None, type=float,
              help="FM de-emphasis time constant (seconds). 0=none.")
@click.option("--data-dir", default=None, type=str,
              help="Override data directory (default: ~/.local/share/sdr-rx).")
@click.pass_context
def main(ctx, channel_ids, gain, squelch,
         record, max_audio_mb, port, host, tx_tail, audio_preset, tau,
         data_dir):
    """SDR Monitor — GNU Radio + Web Dashboard.

    \b
    Monitors configurable NFM channels using RTL-SDR with
    RF power squelch, DCS decoding, and a web dashboard.
    Channel settings are loaded from config.yaml.

    \b
    Examples:
      python main.py                                    # use startup_channels from config
      python main.py -c frs1                            # override: monitor frs1
      python main.py -c frs1 -c frs20                   # monitor two channels
      python main.py -c frs1 -g 30 -s -25               # custom gain and squelch
    """

    # ── Load config ──
    pre_paths = resolve_paths(data_dir_override=data_dir)
    config_dir = pre_paths.config_dir
    cfg = load_config(config_dir)
    settings = cfg["settings"]

    # ── Build CLI overrides dict ──
    # Track which fields were explicitly passed via CLI
    cli_overrides = {}

    def _is_cli_set(param_name):
        src = ctx.get_parameter_source(param_name)
        return src is not None and src != click.core.ParameterSource.DEFAULT

    if _is_cli_set("gain"):
        cli_overrides["gain"] = gain
    if _is_cli_set("squelch"):
        cli_overrides["squelch"] = squelch
    if _is_cli_set("record"):
        cli_overrides["record"] = record
    if _is_cli_set("max_audio_mb"):
        cli_overrides["max_audio_mb"] = max_audio_mb
    if _is_cli_set("tx_tail"):
        cli_overrides["tx_tail"] = tx_tail
    if _is_cli_set("audio_preset"):
        cli_overrides["audio_preset"] = audio_preset
    if _is_cli_set("tau"):
        cli_overrides["tau"] = tau

    # ── Validate persisted settings ──
    settings_errors = validate_settings(settings)
    if settings_errors:
        click.echo("Error: invalid settings in config.yaml:", err=True)
        for field, msg in settings_errors.items():
            click.echo(f"  {field}: {msg}", err=True)
        sys.exit(1)

    # ── Resolve effective settings ──
    eff_gain = cli_overrides.get("gain", settings.get("gain", RF_GAIN))
    eff_squelch = cli_overrides.get("squelch", None)  # None = use per-channel
    eff_record = cli_overrides.get("record", settings.get("record", True))
    eff_max_audio = cli_overrides.get("max_audio_mb", settings.get("max_audio_mb", MAX_AUDIO_MB))
    eff_tx_tail = cli_overrides.get("tx_tail", settings.get("tx_tail", TX_ENDING_TIMEOUT_S))
    eff_preset = cli_overrides.get("audio_preset", settings.get("audio_preset", DEFAULT_AUDIO_PRESET))
    eff_tau = cli_overrides.get("tau", settings.get("tau", FM_TAU))
    eff_log_days = settings.get("log_days", 7)

    # Apply tx-tail to config module
    import config
    config.TX_ENDING_TIMEOUT_S = eff_tx_tail
    config.TX_ENDING_POLLS = int(eff_tx_tail * config.POLL_RATE_HZ)

    # ── Determine channels to monitor ──
    receiver = DEFAULT_RECEIVER
    all_channels = cfg.get("channels", {})

    if channel_ids:
        # CLI overrides startup_channels
        selected_ids = list(channel_ids)
    else:
        selected_ids = cfg.get("startup_channels", [])

    if not selected_ids:
        click.echo("Error: no channels to monitor.", err=True)
        click.echo("  Tip: python main.py -c <channel_id>", err=True)
        click.echo("  Or set startup_channels in config.yaml", err=True)
        sys.exit(1)

    if len(selected_ids) > receiver.max_channels:
        click.echo(f"Error: too many channels ({len(selected_ids)}). "
                   f"Max {receiver.max_channels} per receiver.", err=True)
        sys.exit(1)

    # Check duplicates
    seen = set()
    for cid in selected_ids:
        if cid in seen:
            click.echo(f"Error: duplicate channel ID '{cid}'.", err=True)
            sys.exit(1)
        seen.add(cid)

    # Resolve channel configs
    channel_cfgs = []
    for cid in selected_ids:
        validate_channel_id(cid)
        if cid not in all_channels:
            click.echo(f"Error: channel '{cid}' not found in config.", err=True)
            sys.exit(1)

        ch_cfg = dict(all_channels[cid])
        # Apply defaults for missing fields
        ch_cfg.setdefault("name", CHANNEL_NAME)
        ch_cfg.setdefault("freq_hz", FREQ_HZ)
        ch_cfg.setdefault("dcs_code", DCS_CODE)
        ch_cfg.setdefault("dcs_mode", "advisory")
        ch_cfg.setdefault("squelch", settings.get("default_squelch", SQUELCH_THRESHOLD_DB))

        # Validate channel config
        ch_errors = validate_channel(ch_cfg)
        if ch_errors:
            click.echo(f"Error: invalid config for channel '{cid}':", err=True)
            for field, msg in ch_errors.items():
                click.echo(f"  {field}: {msg}", err=True)
            sys.exit(1)

        # CLI squelch override applies to all channels
        if eff_squelch is not None:
            ch_cfg["squelch"] = eff_squelch

        paths = resolve_paths(data_dir_override=data_dir, channel_id=cid)
        os.makedirs(paths.channel_data_dir, exist_ok=True)
        if eff_record:
            os.makedirs(paths.audio_dir, exist_ok=True)

        channel_cfgs.append((cid, ch_cfg, paths))

    # ── Bandwidth validation ──
    if len(channel_cfgs) > 1:
        freqs = [cfg["freq_hz"] for _, cfg, _ in channel_cfgs]
        spread = max(freqs) - min(freqs)
        max_spread = receiver.sample_rate - CHANNEL_RATE
        if spread > max_spread:
            click.echo(f"Error: channel frequency spread {spread/1e3:.1f} kHz exceeds "
                       f"usable bandwidth {max_spread/1e3:.1f} kHz.", err=True)
            sys.exit(1)

    tau_display = f"{eff_tau}" if eff_tau else f"{FM_TAU} (default)"
    click.echo(f"Starting {len(channel_cfgs)} channel(s)...")
    for cid, cfg_ch, paths in channel_cfgs:
        click.echo(f"  [{cid}] {cfg_ch['name']} — {cfg_ch['freq_hz']/1e6:.3f} MHz, DCS {cfg_ch['dcs_code']:03d} ({cfg_ch['dcs_mode']})")
    click.echo(f"  Gain: {eff_gain} | Squelch: {eff_squelch or 'per-channel'} | Record: {'ON' if eff_record else 'OFF'} | TX tail: {eff_tx_tail}s")
    click.echo(f"  Web: http://{host}:{port} | Audio: {eff_preset} preset | tau: {tau_display}")

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
            squelch_threshold=channel_cfg["squelch"],
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
        gain=eff_gain,
        audio_preset=eff_preset,
        tau=eff_tau,
    )

    # ── Create AppCore per channel ──
    for channel_id, channel_cfg, paths in channel_cfgs:
        stack = channel_stacks[channel_id]
        app_core = AppCore(
            flowgraph=flowgraph,
            audio_tap=stack.audio_tap,
            dcs_decoder=stack.dcs_decoder,
            telemetry_queue=stack.telemetry_queue,
            record=eff_record,
            max_audio_mb=eff_max_audio,
            channel_name=channel_cfg["name"],
            dcs_code=channel_cfg["dcs_code"],
            dcs_mode=channel_cfg["dcs_mode"],
            paths=paths,
            channel_id=channel_id,
            log_days=eff_log_days,
        )
        stack.app_core = app_core

    # Shutdown coordination
    shutdown_event = threading.Event()

    # ── Create web app ──
    web_app = create_app(channel_stacks, flowgraph,
                         config_dir=config_dir, receiver=receiver,
                         cli_overrides=cli_overrides,
                         shutdown_event=shutdown_event)

    # Wire live audio callbacks
    for channel_id, stack in channel_stacks.items():
        stack.audio_tap._live_callback = web_app.broadcast_audio[channel_id]
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

    if getattr(web_app, '_restart_requested', False):
        click.echo("Restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
