"""FastAPI web server with WebSocket telemetry and live audio streaming."""

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
    """Container to avoid Python closure scoping issues with mutable sets."""
    def __init__(self):
        self.telemetry = set()
        self.audio = set()
        self.telem_lock = asyncio.Lock()
        self.audio_lock = asyncio.Lock()
        self.loop = None  # event loop ref, set at startup
        self.audio_client_count = 0  # thread-safe via GIL for simple int reads


def create_app(telemetry_queue, app_core, flowgraph, channel_info=None, paths=None):
    """Create and configure the FastAPI application."""

    if channel_info is None:
        channel_info = {"name": "FRS Ch 1", "freq_hz": 462_562_500, "dcs_code": 0, "dcs_mode": "advisory"}

    audio_dir = paths.audio_dir if paths else "audio"

    app = FastAPI(title=channel_info["name"])
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    clients = _ClientSets()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        index_path = STATIC_DIR / "index.html"
        return index_path.read_text()

    @app.get("/api/channel")
    async def get_channel():
        return JSONResponse(content=channel_info)

    @app.websocket("/ws")
    async def telemetry_ws(websocket: WebSocket):
        await websocket.accept()
        async with clients.telem_lock:
            clients.telemetry.add(websocket)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                    try:
                        data = json.loads(msg)
                        if "squelch_threshold" in data:
                            flowgraph.set_squelch_threshold(float(data["squelch_threshold"]))
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
            async with clients.telem_lock:
                clients.telemetry.discard(websocket)

    @app.websocket("/audio/live")
    async def audio_ws(websocket: WebSocket):
        await websocket.accept()
        async with clients.audio_lock:
            clients.audio.add(websocket)
            clients.audio_client_count = len(clients.audio)
        try:
            while True:
                await asyncio.sleep(60)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with clients.audio_lock:
                clients.audio.discard(websocket)
                clients.audio_client_count = len(clients.audio)

    @app.get("/api/transmissions")
    async def get_transmissions():
        log = app_core.get_tx_log()
        return JSONResponse(content=log)

    @app.delete("/api/transmissions/{index}")
    async def delete_transmission(index: int):
        result = app_core.delete_tx(index)
        if result:
            return JSONResponse(content={"status": "ok"})
        return JSONResponse(status_code=404, content={"error": "not found"})

    @app.get("/api/config")
    async def get_config():
        return JSONResponse(content={
            "squelch_threshold": flowgraph.get_squelch_threshold(),
            "gain": flowgraph.get_gain(),
        })

    @app.put("/api/config")
    async def put_config(config: dict):
        if "squelch_threshold" in config:
            flowgraph.set_squelch_threshold(float(config["squelch_threshold"]))
        if "gain" in config:
            flowgraph.set_gain(float(config["gain"]))
        return JSONResponse(content={"status": "ok"})

    @app.get("/audio/{filename}")
    async def get_audio_file(filename: str):
        safe_name = os.path.basename(filename)
        filepath = os.path.join(audio_dir, safe_name)
        if os.path.isfile(filepath):
            return FileResponse(filepath, media_type="audio/wav")
        return JSONResponse(status_code=404, content={"error": "not found"})

    async def _telemetry_broadcaster():
        """Background task: read telemetry queue and broadcast to WebSocket clients."""
        loop = asyncio.get_running_loop()

        while True:
            try:
                telem = await loop.run_in_executor(
                    None, lambda: telemetry_queue.get(timeout=1.0)
                )
            except queue.Empty:
                continue

            if telem is None:
                break

            # Drain queue, keep only latest
            latest = telem
            while True:
                try:
                    latest = telemetry_queue.get_nowait()
                    if latest is None:
                        return
                except queue.Empty:
                    break

            msg = json.dumps({"type": "telemetry", **latest})

            async with clients.telem_lock:
                dead = []
                for ws in clients.telemetry:
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    clients.telemetry.discard(ws)

    @app.on_event("startup")
    async def startup():
        clients.loop = asyncio.get_running_loop()
        asyncio.create_task(_telemetry_broadcaster())

    def broadcast_audio(seq, chunk_bytes):
        """Called from GNU Radio thread — schedules async broadcast."""
        if clients.audio_client_count == 0:
            return
        loop = clients.loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(
                asyncio.ensure_future,
                _send_audio(seq, chunk_bytes)
            )

    async def _send_audio(seq, chunk_bytes):
        """Send timestamped PCM frame to all audio WebSocket clients."""
        header = struct.pack(">I", seq)
        frame = header + chunk_bytes

        async with clients.audio_lock:
            dead = []
            for ws in clients.audio:
                try:
                    await ws.send_bytes(frame)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                clients.audio.discard(ws)
            if dead:
                clients.audio_client_count = len(clients.audio)

    app.broadcast_audio = broadcast_audio

    return app
