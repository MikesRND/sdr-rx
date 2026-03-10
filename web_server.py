"""FastAPI web server with multi-channel WebSocket telemetry, live audio, and config management."""

import asyncio
import json
import os
import queue
import struct
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from config import (
    TELEMETRY_RATE_HZ, CHANNEL_RATE, CHANNEL_ID_RE,
    SETTINGS_SCHEMA, DEFAULT_SETTINGS,
    load_config, save_config, validate_channel, validate_settings,
)

STATIC_DIR = Path(__file__).parent / "static"


class _ClientSets:
    """Container for WebSocket client sets for one channel."""
    def __init__(self):
        self.telemetry = set()
        self.audio = set()
        self.telem_lock = asyncio.Lock()
        self.audio_lock = asyncio.Lock()
        self.audio_client_count = 0  # thread-safe via GIL for simple int reads


def create_app(channel_stacks, flowgraph, *, config_dir=None, receiver=None, cli_overrides=None,
               shutdown_event=None):
    """Create and configure the FastAPI application for multi-channel monitoring."""

    app = FastAPI(title="SDR Monitor")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _cli_overrides = cli_overrides or {}
    _config_dir = config_dir
    _receiver = receiver
    app._restart_requested = False

    # Per-channel client sets
    clients = {ch_id: _ClientSets() for ch_id in channel_stacks}
    loop_ref = [None]  # mutable container for event loop reference

    def _get_stack(ch):
        """Look up channel stack, return None if not found."""
        return channel_stacks.get(ch)

    def _running_channel_ids():
        return list(channel_stacks.keys())

    # ── Dashboard ─────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        index_path = STATIC_DIR / "index.html"
        return index_path.read_text()

    # ── Channel listing ───────────────────────────────────

    @app.get("/api/channels")
    async def get_channels():
        result = []
        for ch_id, stack in channel_stacks.items():
            info = dict(stack.channel_info)
            info["id"] = ch_id
            result.append(info)
        return JSONResponse(content=result)

    @app.get("/api/channels/{ch}")
    async def get_channel(ch: str):
        stack = _get_stack(ch)
        if not stack:
            return JSONResponse(status_code=404, content={"error": "channel not found"})
        info = dict(stack.channel_info)
        info["id"] = ch
        return JSONResponse(content=info)

    # ── Per-channel config (read-only) ────────────────────

    @app.get("/api/channels/{ch}/config")
    async def get_config(ch: str):
        stack = _get_stack(ch)
        if not stack:
            return JSONResponse(status_code=404, content={"error": "channel not found"})
        return JSONResponse(content={
            "squelch_threshold": flowgraph.get_squelch_threshold(ch),
            "gain": flowgraph.get_gain(),
        })

    # ── Per-channel transmissions ─────────────────────────

    @app.get("/api/channels/{ch}/transmissions")
    async def get_transmissions(ch: str):
        stack = _get_stack(ch)
        if not stack:
            return JSONResponse(status_code=404, content={"error": "channel not found"})
        log = stack.app_core.get_tx_log()
        return JSONResponse(content=log)

    @app.delete("/api/channels/{ch}/transmissions/{index}")
    async def delete_transmission(ch: str, index: int):
        stack = _get_stack(ch)
        if not stack:
            return JSONResponse(status_code=404, content={"error": "channel not found"})
        result = stack.app_core.delete_tx(index)
        if result:
            return JSONResponse(content={"status": "ok"})
        return JSONResponse(status_code=404, content={"error": "not found"})

    # ── Runtime state (read-only) ─────────────────────────

    @app.get("/api/runtime")
    async def get_runtime():
        running = _running_channel_ids()

        # Build effective settings with source tracking
        cfg = load_config(_config_dir) if _config_dir else {"settings": {}}
        config_settings = cfg.get("settings", {})
        settings_from_file = cfg.get("_settings_from_file", set())

        effective = {}
        for key, schema in SETTINGS_SCHEMA.items():
            default_val = schema["default"]
            cli_val = _cli_overrides.get(key)

            if cli_val is not None:
                effective[key] = {"value": cli_val, "source": "cli", "locked": True}
            elif key == "gain":
                # Gain is live-adjustable — read from flowgraph
                live_gain = flowgraph.get_gain()
                saved_gain = config_settings.get("gain", default_val)
                if key in settings_from_file and abs(live_gain - saved_gain) < 0.01:
                    effective[key] = {"value": live_gain, "source": "config", "locked": False}
                elif abs(live_gain - default_val) < 0.01:
                    effective[key] = {"value": live_gain, "source": "default", "locked": False}
                else:
                    effective[key] = {"value": live_gain, "source": "runtime", "locked": False}
            elif key in settings_from_file:
                effective[key] = {"value": config_settings[key], "source": "config", "locked": False}
            else:
                effective[key] = {"value": default_val, "source": "default", "locked": False}

        # Channel runtime state (per-channel overrides)
        channel_runtime = {}
        squelch_cli = _cli_overrides.get("squelch")
        for ch_id in running:
            ch_rt = {}
            if squelch_cli is not None:
                ch_rt["squelch"] = {"value": squelch_cli, "source": "cli", "locked": True}
            else:
                live_sq = flowgraph.get_squelch_threshold(ch_id)
                stack = channel_stacks[ch_id]
                ch_cfg_sq = stack.channel_info.get("squelch")
                if ch_cfg_sq is not None and abs(live_sq - ch_cfg_sq) < 0.01:
                    ch_rt["squelch"] = {"value": live_sq, "source": "config", "locked": False}
                else:
                    ch_rt["squelch"] = {"value": live_sq, "source": "runtime", "locked": False}
            channel_runtime[ch_id] = ch_rt

        receiver_info = {}
        if _receiver:
            receiver_info = {
                "device_index": _receiver.device_index,
                "sample_rate": _receiver.sample_rate,
                "max_channels": _receiver.max_channels,
                "if_gain": _receiver.if_gain,
                "bb_gain": _receiver.bb_gain,
            }

        return JSONResponse(content={
            "receiver": receiver_info,
            "running_channels": running,
            "effective_settings": effective,
            "channel_runtime": channel_runtime,
        })

    # ── Persisted config CRUD ─────────────────────────────

    @app.get("/api/config")
    async def get_full_config():
        if not _config_dir:
            return JSONResponse(status_code=500, content={"error": "config_dir not set"})
        cfg = load_config(_config_dir)
        running = _running_channel_ids()
        startup = cfg.get("startup_channels", [])

        # Add convenience fields per channel
        channels_out = {}
        for ch_id, ch_data in cfg.get("channels", {}).items():
            ch_out = dict(ch_data)
            ch_out["startup_selected"] = ch_id in startup
            ch_out["running"] = ch_id in running
            channels_out[ch_id] = ch_out

        return JSONResponse(content={
            "channels": channels_out,
            "startup_channels": startup,
            "settings": cfg.get("settings", {}),
        })

    @app.post("/api/config/channels")
    async def create_channel(request: Request):
        if not _config_dir:
            return JSONResponse(status_code=500, content={"error": "config_dir not set"})

        body = await request.json()
        ch_id = body.get("id", "").strip()

        # Validate ID
        if not ch_id or not CHANNEL_ID_RE.match(ch_id):
            return JSONResponse(status_code=400, content={
                "errors": {"id": "must match [a-zA-Z0-9_-]+"}
            })

        cfg = load_config(_config_dir)
        if ch_id in cfg["channels"]:
            return JSONResponse(status_code=409, content={
                "errors": {"id": f"channel '{ch_id}' already exists"}
            })

        ch_data = {
            "name": body.get("name", ""),
            "freq_hz": body.get("freq_hz", 0),
            "dcs_code": body.get("dcs_code", 0),
            "dcs_mode": body.get("dcs_mode", "advisory"),
        }
        if "squelch" in body:
            ch_data["squelch"] = body["squelch"]

        errors = validate_channel(ch_data)
        if errors:
            return JSONResponse(status_code=400, content={"errors": errors})

        cfg["channels"][ch_id] = ch_data
        save_config(_config_dir, cfg)
        return JSONResponse(content={"status": "ok", "id": ch_id})

    @app.put("/api/config/channels/{ch_id}")
    async def update_channel(ch_id: str, request: Request):
        if not _config_dir:
            return JSONResponse(status_code=500, content={"error": "config_dir not set"})

        cfg = load_config(_config_dir)
        if ch_id not in cfg["channels"]:
            return JSONResponse(status_code=404, content={"error": "channel not found"})

        body = await request.json()
        running = _running_channel_ids()
        is_running = ch_id in running
        locked_fields = ["freq_hz", "dcs_code", "dcs_mode"]

        # Enforce running-channel restrictions
        if is_running:
            for field in locked_fields:
                if field in body:
                    old_val = cfg["channels"][ch_id].get(field)
                    if body[field] != old_val:
                        return JSONResponse(status_code=409, content={
                            "error": f"cannot change {field} on running channel",
                            "locked_fields": locked_fields,
                        })

        # Build updated channel
        updated = dict(cfg["channels"][ch_id])
        for key in ("name", "freq_hz", "dcs_code", "dcs_mode", "squelch"):
            if key in body:
                updated[key] = body[key]

        errors = validate_channel(updated)
        if errors:
            return JSONResponse(status_code=400, content={"errors": errors})

        # If freq_hz changed and this channel is in the startup set,
        # revalidate the startup set's bandwidth spread
        startup = cfg.get("startup_channels", [])
        if "freq_hz" in body and ch_id in startup and len(startup) > 1:
            # Temporarily apply the update to check bandwidth
            old_channels = cfg["channels"]
            test_channels = dict(old_channels)
            test_channels[ch_id] = updated
            startup_freqs = []
            for sid in startup:
                if sid in test_channels and "freq_hz" in test_channels[sid]:
                    startup_freqs.append(test_channels[sid]["freq_hz"])
            if len(startup_freqs) > 1:
                spread = max(startup_freqs) - min(startup_freqs)
                sample_rate = _receiver.sample_rate if _receiver else 240_000
                max_spread = sample_rate - CHANNEL_RATE
                if spread > max_spread:
                    return JSONResponse(status_code=400, content={
                        "error": f"this change would make the startup set invalid: "
                                 f"channels would span {spread/1e3:.1f} kHz, "
                                 f"exceeding usable bandwidth {max_spread/1e3:.1f} kHz"
                    })

        cfg["channels"][ch_id] = updated
        save_config(_config_dir, cfg)

        # Apply live squelch if running
        if is_running and "squelch" in body:
            squelch_cli = _cli_overrides.get("squelch")
            if squelch_cli is None:
                flowgraph.set_squelch_threshold(ch_id, float(body["squelch"]))

        return JSONResponse(content={"status": "ok"})

    @app.delete("/api/config/channels/{ch_id}")
    async def delete_channel(ch_id: str):
        if not _config_dir:
            return JSONResponse(status_code=500, content={"error": "config_dir not set"})

        cfg = load_config(_config_dir)
        if ch_id not in cfg["channels"]:
            return JSONResponse(status_code=404, content={"error": "channel not found"})

        running = _running_channel_ids()
        if ch_id in running:
            return JSONResponse(status_code=409, content={
                "error": "cannot delete running channel",
            })

        del cfg["channels"][ch_id]
        # Remove from startup_channels if present
        if ch_id in cfg.get("startup_channels", []):
            cfg["startup_channels"] = [c for c in cfg["startup_channels"] if c != ch_id]
        save_config(_config_dir, cfg)
        return JSONResponse(content={"status": "ok"})

    @app.put("/api/config/settings")
    async def update_settings(request: Request):
        if not _config_dir:
            return JSONResponse(status_code=500, content={"error": "config_dir not set"})

        body = await request.json()
        errors = validate_settings(body)
        if errors:
            return JSONResponse(status_code=400, content={"errors": errors})

        cfg = load_config(_config_dir)
        for key, value in body.items():
            if key in SETTINGS_SCHEMA:
                cfg["settings"][key] = value

        save_config(_config_dir, cfg)

        # Apply live gain if not CLI-locked
        if "gain" in body and "gain" not in _cli_overrides:
            flowgraph.set_gain(float(body["gain"]))

        return JSONResponse(content={"status": "ok"})

    @app.put("/api/config/startup_channels")
    async def update_startup_channels(request: Request):
        if not _config_dir:
            return JSONResponse(status_code=500, content={"error": "config_dir not set"})

        body = await request.json()
        channels_list = body.get("channels", [])

        if not isinstance(channels_list, list):
            return JSONResponse(status_code=400, content={
                "error": "channels must be a list"
            })

        cfg = load_config(_config_dir)
        all_channels = cfg.get("channels", {})
        max_ch = _receiver.max_channels if _receiver else 2

        # Validate: no dupes
        if len(channels_list) != len(set(channels_list)):
            return JSONResponse(status_code=400, content={
                "error": "duplicate channel IDs"
            })

        # Validate: count
        if len(channels_list) > max_ch:
            return JSONResponse(status_code=400, content={
                "error": f"too many channels (max {max_ch})"
            })

        # Validate: all exist
        for cid in channels_list:
            if cid not in all_channels:
                return JSONResponse(status_code=400, content={
                    "error": f"channel '{cid}' not found in config"
                })

        # Validate: bandwidth spread
        if len(channels_list) > 1:
            freqs = [all_channels[cid]["freq_hz"] for cid in channels_list]
            spread = max(freqs) - min(freqs)
            sample_rate = _receiver.sample_rate if _receiver else 240_000
            max_spread = sample_rate - CHANNEL_RATE
            if spread > max_spread:
                return JSONResponse(status_code=400, content={
                    "error": f"selected channels span {spread/1e3:.1f} kHz, "
                             f"exceeding usable bandwidth {max_spread/1e3:.1f} kHz"
                })

        cfg["startup_channels"] = channels_list
        save_config(_config_dir, cfg)
        return JSONResponse(content={"status": "ok"})

    # ── Restart ────────────────────────────────────────────

    @app.post("/api/restart")
    async def restart():
        app._restart_requested = True
        if shutdown_event:
            shutdown_event.set()
        return JSONResponse(content={"status": "restarting"})

    # ── Software update ───────────────────────────────────

    @app.post("/api/update")
    async def update_software():
        """Pull latest code, rebuild venv, restart service via update.sh."""
        update_script = Path(__file__).parent / "deploy" / "update.sh"
        if not update_script.is_file():
            return JSONResponse(status_code=404, content={"error": "update.sh not found"})

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: subprocess.run(
                ["sudo", str(update_script)],
                capture_output=True, text=True, timeout=300,
            ))
            if result.returncode == 0:
                return JSONResponse(content={
                    "status": "ok",
                    "output": result.stdout[-2000:] if result.stdout else "",
                })
            else:
                return JSONResponse(status_code=500, content={
                    "status": "error",
                    "output": (result.stdout + result.stderr)[-2000:],
                })
        except subprocess.TimeoutExpired:
            return JSONResponse(status_code=504, content={
                "status": "error",
                "output": "Update timed out after 5 minutes",
            })

    # ── Per-channel telemetry WebSocket ───────────────────

    @app.websocket("/ws/{ch}")
    async def telemetry_ws(websocket: WebSocket, ch: str):
        if ch not in clients:
            await websocket.close(code=4004)
            return
        ch_clients = clients[ch]
        await websocket.accept()
        async with ch_clients.telem_lock:
            ch_clients.telemetry.add(websocket)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                    try:
                        data = json.loads(msg)
                        if "squelch_threshold" in data:
                            if "squelch" not in _cli_overrides:
                                flowgraph.set_squelch_threshold(ch, float(data["squelch_threshold"]))
                            else:
                                await websocket.send_text(json.dumps({"type": "locked", "field": "squelch"}))
                        if "gain" in data:
                            if "gain" not in _cli_overrides:
                                flowgraph.set_gain(float(data["gain"]))
                            else:
                                await websocket.send_text(json.dumps({"type": "locked", "field": "gain"}))
                    except (json.JSONDecodeError, ValueError):
                        pass
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "ping"}))
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with ch_clients.telem_lock:
                ch_clients.telemetry.discard(websocket)

    # ── Per-channel live audio WebSocket ──────────────────

    @app.websocket("/audio/{ch}/live")
    async def audio_ws(websocket: WebSocket, ch: str):
        if ch not in clients:
            await websocket.close(code=4004)
            return
        ch_clients = clients[ch]
        await websocket.accept()
        async with ch_clients.audio_lock:
            ch_clients.audio.add(websocket)
            ch_clients.audio_client_count = len(ch_clients.audio)
        try:
            while True:
                await asyncio.sleep(60)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with ch_clients.audio_lock:
                ch_clients.audio.discard(websocket)
                ch_clients.audio_client_count = len(ch_clients.audio)

    # ── Per-channel audio file download ───────────────────

    @app.get("/audio/{ch}/{filename}")
    async def get_audio_file(ch: str, filename: str):
        stack = _get_stack(ch)
        if not stack:
            return JSONResponse(status_code=404, content={"error": "channel not found"})
        safe_name = os.path.basename(filename)
        filepath = os.path.join(stack.paths.audio_dir, safe_name)
        if os.path.isfile(filepath):
            return FileResponse(filepath, media_type="audio/wav")
        return JSONResponse(status_code=404, content={"error": "not found"})

    # ── Telemetry broadcaster (one task per channel) ──────

    async def _telemetry_broadcaster(ch_id):
        """Background task: read telemetry queue and broadcast to WebSocket clients."""
        stack = channel_stacks[ch_id]
        ch_clients = clients[ch_id]
        telem_q = stack.telemetry_queue
        loop = asyncio.get_running_loop()

        while True:
            try:
                telem = await loop.run_in_executor(
                    None, lambda: telem_q.get(timeout=1.0)
                )
            except queue.Empty:
                continue

            if telem is None:
                break

            # Drain queue, keep only latest
            latest = telem
            while True:
                try:
                    latest = telem_q.get_nowait()
                    if latest is None:
                        return
                except queue.Empty:
                    break

            msg = json.dumps({"type": "telemetry", **latest})

            async with ch_clients.telem_lock:
                dead = []
                for ws in ch_clients.telemetry:
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    ch_clients.telemetry.discard(ws)

    # ── Audio broadcast (one closure per channel) ─────────

    async def _send_audio(ch_id, seq, chunk_bytes):
        """Send timestamped PCM frame to all audio WebSocket clients for a channel."""
        ch_clients = clients[ch_id]
        header = struct.pack(">I", seq)
        frame = header + chunk_bytes

        async with ch_clients.audio_lock:
            dead = []
            for ws in ch_clients.audio:
                try:
                    await ws.send_bytes(frame)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                ch_clients.audio.discard(ws)
            if dead:
                ch_clients.audio_client_count = len(ch_clients.audio)

    def _make_broadcast_audio(ch_id):
        """Create a broadcast_audio callback for a specific channel."""
        ch_clients = clients[ch_id]

        def broadcast_audio(seq, chunk_bytes):
            """Called from GNU Radio thread — schedules async broadcast."""
            if ch_clients.audio_client_count == 0:
                return
            loop = loop_ref[0]
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    _send_audio(ch_id, seq, chunk_bytes)
                )

        return broadcast_audio

    # Build per-channel broadcast_audio dict
    app.broadcast_audio = {ch_id: _make_broadcast_audio(ch_id) for ch_id in channel_stacks}

    @app.on_event("startup")
    async def startup():
        loop_ref[0] = asyncio.get_running_loop()
        for ch_id in channel_stacks:
            asyncio.create_task(_telemetry_broadcaster(ch_id))

    return app
