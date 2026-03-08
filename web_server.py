"""FastAPI web server with multi-channel WebSocket telemetry and live audio streaming."""

import asyncio
import json
import os
import queue
import struct
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import TELEMETRY_RATE_HZ

STATIC_DIR = Path(__file__).parent / "static"


class _ClientSets:
    """Container for WebSocket client sets for one channel."""
    def __init__(self):
        self.telemetry = set()
        self.audio = set()
        self.telem_lock = asyncio.Lock()
        self.audio_lock = asyncio.Lock()
        self.audio_client_count = 0  # thread-safe via GIL for simple int reads


def create_app(channel_stacks, flowgraph):
    """Create and configure the FastAPI application for multi-channel monitoring."""

    app = FastAPI(title="SDR Monitor")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Per-channel client sets
    clients = {ch_id: _ClientSets() for ch_id in channel_stacks}
    loop_ref = [None]  # mutable container for event loop reference

    def _get_stack(ch):
        """Look up channel stack, return None if not found."""
        return channel_stacks.get(ch)

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
                            flowgraph.set_squelch_threshold(ch, float(data["squelch_threshold"]))
                        if "gain" in data:
                            flowgraph.set_gain(float(data["gain"]))
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
