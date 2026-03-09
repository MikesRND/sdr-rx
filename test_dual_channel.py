#!/usr/bin/env python3
"""Smoke tests for dual-channel SDR monitor.

Runs without hardware. Tests config validation, web API routes,
and per-channel isolation.

Usage: python test_dual_channel.py
"""

import dataclasses
import os
import queue
import sys
import tempfile

# ── Test validate_channel ────────────────────────────

def test_validate_channel():
    from config import validate_channel

    # Valid channel
    errors = validate_channel({
        "name": "Test", "freq_hz": 462562500, "dcs_code": 0,
        "dcs_mode": "advisory", "squelch": -45.0,
    })
    assert errors == {}, f"Expected no errors, got {errors}"

    # Invalid freq
    errors = validate_channel({
        "name": "Test", "freq_hz": 500, "dcs_code": 0, "dcs_mode": "advisory",
    })
    assert "freq_hz" in errors

    # Invalid DCS code (non-octal digits)
    errors = validate_channel({
        "name": "Test", "freq_hz": 462562500, "dcs_code": 189, "dcs_mode": "advisory",
    })
    assert "dcs_code" in errors

    # Invalid name (empty)
    errors = validate_channel({
        "name": "", "freq_hz": 462562500, "dcs_code": 0, "dcs_mode": "advisory",
    })
    assert "name" in errors

    # Invalid squelch
    errors = validate_channel({
        "name": "Test", "freq_hz": 462562500, "dcs_code": 0,
        "dcs_mode": "advisory", "squelch": -80,
    })
    assert "squelch" in errors

    print("  PASS: validate_channel")


# ── Test load/save config ────────────────────────────

def test_config_load_save():
    from config import load_config, save_config

    with tempfile.TemporaryDirectory() as tmpdir:
        # Missing file returns seeded defaults
        cfg = load_config(tmpdir)
        assert "channels" in cfg
        assert "frs1" in cfg["channels"]
        assert len(cfg["channels"]) == 30
        assert cfg["startup_channels"] == ["frs1"]

        # Save and reload
        save_config(tmpdir, cfg)
        cfg2 = load_config(tmpdir)
        assert cfg2["channels"]["frs1"]["freq_hz"] == 462562500
        assert cfg2["startup_channels"] == ["frs1"]

    print("  PASS: config load/save")


# ── Test web API routes ──────────────────────────────

def test_web_routes():
    try:
        from starlette.testclient import TestClient
    except (ImportError, RuntimeError):
        print("  FAIL: web routes — httpx is required (pip install httpx)")
        return False

    from config import ResolvedPaths, Receiver

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
            config_dir=tmpdir,
            data_root=tmpdir, channel_data_dir=os.path.join(tmpdir, "ch_a"),
            audio_dir=os.path.join(tmpdir, "ch_a", "audio"),
        )
        paths_b = ResolvedPaths(
            config_dir=tmpdir,
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
        receiver = Receiver()

        from web_server import create_app
        app = create_app(stacks, fg, config_dir=tmpdir, receiver=receiver, cli_overrides={})
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

        # GET /api/runtime
        r = client.get("/api/runtime")
        assert r.status_code == 200
        rt = r.json()
        assert "receiver" in rt
        assert "running_channels" in rt
        assert set(rt["running_channels"]) == {"ch_a", "ch_b"}
        assert "effective_settings" in rt

        # GET /api/config
        r = client.get("/api/config")
        assert r.status_code == 200
        cfg = r.json()
        assert "channels" in cfg
        assert "settings" in cfg

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


# ── Test WS CLI lock enforcement ──────────────────────

def test_ws_cli_lock():
    """WebSocket ignores gain/squelch updates when CLI-locked."""
    try:
        from starlette.testclient import TestClient
    except (ImportError, RuntimeError):
        print("  FAIL: ws cli lock — httpx required")
        return False

    from config import ResolvedPaths, Receiver

    @dataclasses.dataclass
    class MockAppCore:
        def get_tx_log(self):
            return []
        def delete_tx(self, index):
            return False

    @dataclasses.dataclass
    class MockFlowgraph:
        _squelch: float = -45.0
        _gain: float = 40.0
        def get_squelch_threshold(self, ch):
            return self._squelch
        def get_gain(self):
            return self._gain
        def set_squelch_threshold(self, ch, db):
            self._squelch = db
        def set_gain(self, gain):
            self._gain = gain

    @dataclasses.dataclass
    class MockStack:
        channel_id: str
        channel_info: dict
        app_core: object
        audio_tap: object = None
        dcs_decoder: object = None
        telemetry_queue: object = None
        paths: object = None

    import json
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = ResolvedPaths(
            config_dir=tmpdir, data_root=tmpdir,
            channel_data_dir=os.path.join(tmpdir, "ch_a"),
            audio_dir=os.path.join(tmpdir, "ch_a", "audio"),
        )
        fg = MockFlowgraph()
        stacks = {
            "ch_a": MockStack(
                channel_id="ch_a",
                channel_info={"name": "A", "freq_hz": 462562500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
                app_core=MockAppCore(),
                telemetry_queue=queue.Queue(maxsize=50),
                paths=paths,
            ),
        }

        from web_server import create_app
        # CLI overrides lock both gain and squelch
        app = create_app(stacks, fg, config_dir=tmpdir, receiver=Receiver(),
                         cli_overrides={"gain": 40.0, "squelch": -45.0})
        client = TestClient(app)

        with client.websocket_connect("/ws/ch_a") as ws:
            # Try to change squelch — should be rejected
            ws.send_text(json.dumps({"squelch_threshold": -30.0}))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "locked" and resp["field"] == "squelch"
            assert fg._squelch == -45.0, "Squelch should not have changed"

            # Try to change gain — should be rejected
            ws.send_text(json.dumps({"gain": 20.0}))
            resp = json.loads(ws.receive_text())
            assert resp["type"] == "locked" and resp["field"] == "gain"
            assert fg._gain == 40.0, "Gain should not have changed"

    print("  PASS: ws cli lock enforcement")
    return True


# ── Test invalid settings abort startup ───────────────

def test_invalid_settings_startup():
    """Invalid saved settings should cause validate_settings to return errors."""
    from config import validate_settings

    # Valid settings — no errors
    good = {"gain": 40, "default_squelch": -45.0, "audio_preset": "conservative",
            "tau": 0, "record": True, "max_audio_mb": 500, "tx_tail": 2.0, "log_days": 7}
    assert validate_settings(good) == {}

    # Invalid gain (out of range)
    bad = dict(good, gain=200)
    errors = validate_settings(bad)
    assert "gain" in errors

    # Invalid audio_preset
    bad2 = dict(good, audio_preset="invalid_preset")
    errors2 = validate_settings(bad2)
    assert "audio_preset" in errors2

    print("  PASS: invalid settings abort startup")


# ── Test squelch provenance after live change ─────────

def test_squelch_provenance():
    """/api/runtime reports source='runtime' after a live squelch change."""
    try:
        from starlette.testclient import TestClient
    except (ImportError, RuntimeError):
        print("  FAIL: squelch provenance — httpx required")
        return False

    from config import ResolvedPaths, Receiver

    @dataclasses.dataclass
    class MockAppCore:
        def get_tx_log(self):
            return []
        def delete_tx(self, index):
            return False

    class MockFlowgraph:
        def __init__(self):
            self._squelch = -45.0
            self._gain = 40.0
        def get_squelch_threshold(self, ch):
            return self._squelch
        def get_gain(self):
            return self._gain
        def set_squelch_threshold(self, ch, db):
            self._squelch = db
        def set_gain(self, gain):
            self._gain = gain

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
        paths = ResolvedPaths(
            config_dir=tmpdir, data_root=tmpdir,
            channel_data_dir=os.path.join(tmpdir, "ch_a"),
            audio_dir=os.path.join(tmpdir, "ch_a", "audio"),
        )
        fg = MockFlowgraph()
        stacks = {
            "ch_a": MockStack(
                channel_id="ch_a",
                channel_info={"name": "A", "freq_hz": 462562500, "dcs_code": 0, "dcs_mode": "advisory", "squelch": -45.0},
                app_core=MockAppCore(),
                telemetry_queue=queue.Queue(maxsize=50),
                paths=paths,
            ),
        }

        from web_server import create_app
        app = create_app(stacks, fg, config_dir=tmpdir, receiver=Receiver(), cli_overrides={})
        client = TestClient(app)

        # Before any live change — source should be "config"
        r = client.get("/api/runtime")
        rt = r.json()
        assert rt["channel_runtime"]["ch_a"]["squelch"]["source"] == "config"

        # Simulate a live squelch change via WS
        fg._squelch = -35.0  # different from config's -45.0

        # Now runtime should report "runtime"
        r = client.get("/api/runtime")
        rt = r.json()
        assert rt["channel_runtime"]["ch_a"]["squelch"]["source"] == "runtime", \
            f"Expected 'runtime', got '{rt['channel_runtime']['ch_a']['squelch']['source']}'"
        assert rt["channel_runtime"]["ch_a"]["squelch"]["value"] == -35.0

        # Gain provenance: before live change, gain matches default (40)
        r = client.get("/api/runtime")
        rt = r.json()
        assert rt["effective_settings"]["gain"]["value"] == 40.0

        # Simulate a live gain change
        fg._gain = 25.0

        r = client.get("/api/runtime")
        rt = r.json()
        assert rt["effective_settings"]["gain"]["value"] == 25.0, \
            f"Expected live gain 25.0, got {rt['effective_settings']['gain']['value']}"
        assert rt["effective_settings"]["gain"]["source"] == "runtime", \
            f"Expected 'runtime', got '{rt['effective_settings']['gain']['source']}'"

    print("  PASS: squelch/gain provenance after live change")
    return True


# ── Main ─────────────────────────────────────────────

if __name__ == "__main__":
    print("Running dual-channel smoke tests...")
    test_receiver()
    test_validate_channel()
    test_config_load_save()
    test_telemetry_isolation()
    web_ok = test_web_routes()
    if web_ok is False:
        print("FAILED: some tests did not pass.")
        sys.exit(1)
    test_invalid_settings_startup()
    ws_ok = test_ws_cli_lock()
    if ws_ok is False:
        print("FAILED: some tests did not pass.")
        sys.exit(1)
    prov_ok = test_squelch_provenance()
    if prov_ok is False:
        print("FAILED: some tests did not pass.")
        sys.exit(1)
    print("All tests passed.")
