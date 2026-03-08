#!/usr/bin/env python3
"""Regression tests for async finalize: delete race, backpressure, shutdown.

Usage: python test_finalize.py
"""

import os
import queue
import sys
import tempfile
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import app_core as app_core_mod
from config import ResolvedPaths
from recording import write_wav as real_write_wav


def _make_paths(tmpdir, channel_id="test"):
    """Create ResolvedPaths pointing into a temp directory."""
    return ResolvedPaths(
        config_dir=tmpdir,
        channels_dir=tmpdir,
        data_root=tmpdir,
        channel_data_dir=os.path.join(tmpdir, channel_id),
        audio_dir=os.path.join(tmpdir, channel_id, "audio"),
    )


def _make_app_core(tmpdir, channel_id="test", record=True):
    """Create an AppCore with mock flowgraph/audio_tap/dcs for testing."""
    fg = MagicMock()
    fg.get_rssi.return_value = -30.0
    fg.get_squelch_open.return_value = False
    fg.get_squelch_threshold.return_value = -45.0
    fg.get_gain.return_value = 40

    audio_tap = MagicMock()
    audio_tap.is_recording.return_value = False
    audio_tap.get_recording_duration.return_value = 0.0

    dcs = MagicMock()
    dcs.get_detected.return_value = False
    dcs.get_polarity.return_value = "unknown"

    paths = _make_paths(tmpdir, channel_id)
    telem_q = queue.Queue(maxsize=50)

    core = app_core_mod.AppCore(
        flowgraph=fg,
        audio_tap=audio_tap,
        dcs_decoder=dcs,
        telemetry_queue=telem_q,
        record=record,
        max_audio_mb=500,
        channel_name="Test Channel",
        dcs_code=0,
        dcs_mode="advisory",
        paths=paths,
        channel_id=channel_id,
    )
    return core, fg, audio_tap, dcs, telem_q


def _setup_recording(core, audio_tap, raw_audio):
    """Set up core state and audio_tap mock for a finalize call."""
    audio_tap.is_recording.return_value = True
    audio_tap.stop_recording.return_value = raw_audio
    core._tx_start_time = datetime.now()
    core._state = core.TX_ACTIVE
    core._tx_peak_rssi = -25.0


# 1 second of 16-bit mono at 8kHz
_RAW_1S = b'\x00\x01' * 8000


def test_delete_before_worker_runs():
    """Delete before worker runs should produce no file after worker drain."""
    with tempfile.TemporaryDirectory() as tmpdir:
        core, fg, audio_tap, _, _ = _make_app_core(tmpdir)

        # Gate blocks write_wav until we release it
        gate = threading.Event()
        worker_entered = threading.Event()

        def gated_write_wav(filename, raw_bytes, **kwargs):
            worker_entered.set()
            gate.wait(timeout=10)
            real_write_wav(filename, raw_bytes, **kwargs)

        with patch.object(app_core_mod, 'write_wav', side_effect=gated_write_wav):
            core.start()
            time.sleep(0.05)

            _setup_recording(core, audio_tap, _RAW_1S)
            core._finalize_tx()

            # Wait until worker has dequeued and entered write_wav
            assert worker_entered.wait(timeout=5), "Worker never picked up job"

            # Entry in log with filename
            assert len(core._tx_log) == 1
            filename = core._tx_log[0]["filename"]
            assert filename is not None

            # Delete before write completes
            result = core.delete_tx(0)
            assert result is True
            assert len(core._tx_log) == 0

            # Release gate — worker should see cancellation (post-write cleanup)
            gate.set()
            core.stop()

        # File should NOT exist
        assert not os.path.isfile(filename), f"Orphan file created: {filename}"
        assert core._finalize_canceled_count >= 1

    print("  PASS: delete before worker runs")


def test_delete_after_file_written():
    """Delete after file already written should remove the file normally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        core, fg, audio_tap, _, _ = _make_app_core(tmpdir)

        core.start()
        time.sleep(0.05)

        _setup_recording(core, audio_tap, _RAW_1S)
        core._finalize_tx()

        # Wait for worker to finish
        deadline = time.monotonic() + 5
        while core._finalize_pending > 0 and time.monotonic() < deadline:
            time.sleep(0.05)

        assert len(core._tx_log) == 1
        filename = core._tx_log[0]["filename"]
        assert filename is not None
        assert os.path.isfile(filename), "WAV should exist after finalize"

        # Now delete — should remove the file
        result = core.delete_tx(0)
        assert result is True
        assert not os.path.isfile(filename), "WAV should be deleted"

        core.stop()
    print("  PASS: delete after file written")


def test_burst_transmissions_bounded_memory():
    """Burst N transmissions should keep memory bounded via backpressure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        core, fg, audio_tap, _, _ = _make_app_core(tmpdir)

        raw_audio = b'\x00\x01' * 16000  # 2s of audio

        # Gate blocks only the finalize worker thread so jobs pile up.
        # Inline fallback writes (on the poll/main thread) pass through ungated.
        gate = threading.Event()
        worker_entered = threading.Event()
        worker_thread_name = "finalize-test"

        def gated_write_wav(filename, raw_bytes, **kwargs):
            if threading.current_thread().name == worker_thread_name:
                worker_entered.set()
                gate.wait(timeout=30)
            real_write_wav(filename, raw_bytes, **kwargs)

        with patch.object(app_core_mod, 'write_wav', side_effect=gated_write_wav):
            core.start()
            time.sleep(0.05)

            # Submit first job, wait for it to be picked up by worker
            _setup_recording(core, audio_tap, raw_audio)
            core._finalize_tx()
            assert worker_entered.wait(timeout=5), "Worker never picked up first job"

            # Now the queue is empty, worker is blocked on job 1.
            # Burst enough to fill queue + overflow.
            max_q = app_core_mod._FINALIZE_QUEUE_MAX
            burst_extra = max_q + 3  # guaranteed to exceed capacity

            for i in range(burst_extra):
                _setup_recording(core, audio_tap, raw_audio)
                core._finalize_tx()

            total_submitted = 1 + burst_extra

            # Queue should be at capacity (not unbounded)
            assert core._finalize_queue.qsize() <= max_q

            # Overflow jobs should have been dropped (written inline)
            assert core._finalize_dropped >= burst_extra - max_q, \
                f"Expected >= {burst_extra - max_q} drops, got {core._finalize_dropped}"

            # All TXs should be in the log regardless of drop/queue
            assert len(core._tx_log) == total_submitted
            filenames = [e["filename"] for e in core._tx_log if e.get("filename")]
            assert len(filenames) == total_submitted
            assert len(set(filenames)) == len(filenames), "Filenames must be unique per TX"

        # Release and drain
        gate.set()
        core.stop()
        assert core._finalize_pending == 0

    print("  PASS: burst transmissions bounded memory")


def test_shutdown_drains_pending():
    """Shutdown with pending jobs should drain consistently."""
    with tempfile.TemporaryDirectory() as tmpdir:
        core, fg, audio_tap, _, _ = _make_app_core(tmpdir)

        core.start()
        time.sleep(0.05)

        for i in range(3):
            _setup_recording(core, audio_tap, _RAW_1S)
            core._finalize_tx()
            time.sleep(0.01)

        # Stop should drain all jobs
        core.stop()

        # All files should exist
        for entry in core._tx_log:
            if entry.get("filename"):
                assert os.path.isfile(entry["filename"]), \
                    f"File missing after shutdown drain: {entry['filename']}"

        assert core._finalize_pending == 0, \
            f"Pending jobs remain after shutdown: {core._finalize_pending}"

    print("  PASS: shutdown drains pending")


def test_shutdown_does_not_resurrect_deleted():
    """Shutdown should not resurrect deleted entries as orphan files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        core, fg, audio_tap, _, _ = _make_app_core(tmpdir)

        gate = threading.Event()
        worker_entered = threading.Event()

        def gated_write_wav(filename, raw_bytes, **kwargs):
            worker_entered.set()
            gate.wait(timeout=10)
            real_write_wav(filename, raw_bytes, **kwargs)

        with patch.object(app_core_mod, 'write_wav', side_effect=gated_write_wav):
            core.start()
            time.sleep(0.05)

            _setup_recording(core, audio_tap, _RAW_1S)
            core._finalize_tx()

            # Wait for worker to pick up the job
            assert worker_entered.wait(timeout=5), "Worker never picked up job"

            filename = core._tx_log[0]["filename"]

            # Delete entry — cancels the pending write
            core.delete_tx(0)

            # Release gate and shutdown
            gate.set()
            core.stop()

        assert not os.path.isfile(filename), f"Deleted entry resurrected: {filename}"

    print("  PASS: shutdown does not resurrect deleted")


def test_finalize_counters_in_telemetry():
    """finalize_pending should appear in telemetry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        core, fg, audio_tap, _, telem_q = _make_app_core(tmpdir)

        core.start()
        time.sleep(0.15)

        telem = None
        while not telem_q.empty():
            telem = telem_q.get_nowait()

        assert telem is not None, "No telemetry received"
        assert "finalize_pending" in telem, "finalize_pending missing from telemetry"
        assert telem["finalize_pending"] == 0

        core.stop()
    print("  PASS: finalize counters in telemetry")


if __name__ == "__main__":
    print("Running finalize regression tests...")
    test_delete_before_worker_runs()
    test_delete_after_file_written()
    test_burst_transmissions_bounded_memory()
    test_shutdown_drains_pending()
    test_shutdown_does_not_resurrect_deleted()
    test_finalize_counters_in_telemetry()
    print("All finalize tests passed.")
