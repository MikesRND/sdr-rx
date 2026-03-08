#!/usr/bin/env python3
"""Smoke tests for dual-channel SDR monitor.

Runs without hardware. Tests config resolution, web API routes,
and per-channel isolation.

Usage: python test_dual_channel.py
"""

import dataclasses
import os
import queue
import sys
import tempfile

# ── Test resolve_channel_configs ──────────────────────

def test_resolve_channel_configs():
    from config import resolve_channel_configs, validate_channel_id

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test channel files
        for name in ("ch_a", "ch_b", "ch_c"):
            with open(os.path.join(tmpdir, f"{name}.yaml"), "w") as f:
                f.write(f"name: {name}\nfreq_hz: 462562500\ndcs_code: 0\n")

        # Valid: two channels
        result = resolve_channel_configs(("ch_a", "ch_b"), tmpdir, max_channels=2)
        assert len(result) == 2
        assert result[0][0] == "ch_a"
        assert result[1][0] == "ch_b"
        assert result[0][1].endswith("ch_a.yaml")

        # Valid: single channel
        result = resolve_channel_configs(("ch_a",), tmpdir, max_channels=2)
        assert len(result) == 1

        # Error: no channels (should exit)
        try:
            resolve_channel_configs((), tmpdir, max_channels=2)
            assert False, "Should have exited"
        except SystemExit:
            pass

        # Error: too many channels
        try:
            resolve_channel_configs(("ch_a", "ch_b", "ch_c"), tmpdir, max_channels=2)
            assert False, "Should have exited"
        except SystemExit:
            pass

        # Error: duplicate
        try:
            resolve_channel_configs(("ch_a", "ch_a"), tmpdir, max_channels=2)
            assert False, "Should have exited"
        except SystemExit:
            pass

        # Error: missing file
        try:
            resolve_channel_configs(("ch_a", "nonexistent"), tmpdir, max_channels=2)
            assert False, "Should have exited"
        except SystemExit:
            pass

    print("  PASS: resolve_channel_configs")


# ── Test web API routes ──────────────────────────────

def test_web_routes():
    try:
        from starlette.testclient import TestClient
    except (ImportError, RuntimeError):
        print("  FAIL: web routes — httpx is required (pip install httpx)")
        return False

    from config import ResolvedPaths

    # Build mock channel stacks
    @dataclasses.dataclass
    class MockAppCore:
        def get_tx_log(self):
            return [{"time": "12:00:00", "duration": 3.5, "peak_rssi": -30.0,
                      "dcs_confirmed": True, "dcs_polarity": "N", "filename": None}]
        def delete_tx(self, index):
            return index == 0

    @dataclasses.dataclass
    class MockFlowgraph:
        def get_squelch_threshold(self, ch):
            return -45.0
        def get_gain(self):
            return 40
        def set_squelch_threshold(self, ch, db):
            pass
        def set_gain(self, gain):
            pass

    @dataclasses.dataclass
    class MockStack:
        channel_id: str
        channel_info: dict
        app_core: object
        audio_tap: object = None
        dcs_decoder: object = None
        telemetry_queue: object = None
        paths: object = None

    with tempfile.TemporaryDirectory() as tmpdir:
        paths_a = ResolvedPaths(
            config_dir=tmpdir, channels_dir=tmpdir,
            data_root=tmpdir, channel_data_dir=os.path.join(tmpdir, "ch_a"),
            audio_dir=os.path.join(tmpdir, "ch_a", "audio"),
        )
        paths_b = ResolvedPaths(
            config_dir=tmpdir, channels_dir=tmpdir,
            data_root=tmpdir, channel_data_dir=os.path.join(tmpdir, "ch_b"),
            audio_dir=os.path.join(tmpdir, "ch_b", "audio"),
        )

        stacks = {
            "ch_a": MockStack(
                channel_id="ch_a",
                channel_info={"name": "Channel A", "freq_hz": 464475000, "dcs_code": 565, "dcs_mode": "advisory"},
                app_core=MockAppCore(),
                telemetry_queue=queue.Queue(maxsize=50),
                paths=paths_a,
            ),
            "ch_b": MockStack(
                channel_id="ch_b",
                channel_info={"name": "Channel B", "freq_hz": 464575000, "dcs_code": 0, "dcs_mode": "advisory"},
                app_core=MockAppCore(),
                telemetry_queue=queue.Queue(maxsize=50),
                paths=paths_b,
            ),
        }

        fg = MockFlowgraph()

        from web_server import create_app
        app = create_app(stacks, fg)
        client = TestClient(app)

        # GET /api/channels — list both
        r = client.get("/api/channels")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 2
        ids = {ch["id"] for ch in data}
        assert "ch_a" in ids
        assert "ch_b" in ids

        # GET /api/channels/{id} — single channel
        r = client.get("/api/channels/ch_a")
        assert r.status_code == 200
        assert r.json()["name"] == "Channel A"
        assert r.json()["freq_hz"] == 464475000

        r = client.get("/api/channels/ch_b")
        assert r.status_code == 200
        assert r.json()["name"] == "Channel B"

        # GET /api/channels/{bad_id} — 404
        r = client.get("/api/channels/nonexistent")
        assert r.status_code == 404

        # GET /api/channels/{id}/config — per-channel config
        r = client.get("/api/channels/ch_a/config")
        assert r.status_code == 200
        assert r.json()["squelch_threshold"] == -45.0
        assert r.json()["gain"] == 40

        # GET /api/channels/{id}/transmissions
        r = client.get("/api/channels/ch_a/transmissions")
        assert r.status_code == 200
        log = r.json()
        assert len(log) == 1
        assert log[0]["time"] == "12:00:00"

        # DELETE /api/channels/{id}/transmissions/{idx}
        r = client.delete("/api/channels/ch_a/transmissions/0")
        assert r.status_code == 200

        r = client.delete("/api/channels/ch_a/transmissions/99")
        assert r.status_code == 404

        # 404 for bad channel on all routes
        r = client.get("/api/channels/bad/config")
        assert r.status_code == 404
        r = client.get("/api/channels/bad/transmissions")
        assert r.status_code == 404

        # GET / — dashboard
        r = client.get("/")
        assert r.status_code == 200
        assert "channelSelector" in r.text

    print("  PASS: web routes")
    return True


# ── Test per-channel telemetry isolation ──────────────

def test_telemetry_isolation():
    q1 = queue.Queue(maxsize=50)
    q2 = queue.Queue(maxsize=50)

    q1.put({"rssi": -30.0, "state": "IDLE"})

    assert not q1.empty()
    assert q2.empty()

    item = q1.get_nowait()
    assert item["rssi"] == -30.0
    assert q2.empty()

    print("  PASS: telemetry isolation")


# ── Test Receiver dataclass ──────────────────────────

def test_receiver():
    from config import Receiver, DEFAULT_RECEIVER, CHANNEL_RATE, DECIMATION

    r = DEFAULT_RECEIVER
    assert r.device_index == 0
    assert r.sample_rate == 240_000
    assert r.max_channels == 2
    assert r.if_gain == 20
    assert r.bb_gain == 20

    # Verify DECIMATION is computed correctly
    assert DECIMATION == r.sample_rate // CHANNEL_RATE

    # Verify frozen
    try:
        r.sample_rate = 1_000_000
        assert False, "Should be frozen"
    except (dataclasses.FrozenInstanceError, AttributeError):
        pass

    print("  PASS: Receiver dataclass")


# ── Main ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Running dual-channel smoke tests...")
    test_receiver()
    test_resolve_channel_configs()
    test_telemetry_isolation()
    web_ok = test_web_routes()
    if web_ok is False:
        print("FAILED: some tests did not pass.")
        sys.exit(1)
    print("All tests passed.")
