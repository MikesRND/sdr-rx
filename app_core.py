"""Application core — state machine, recording orchestration, CSV logging.

States: IDLE → CARRIER_DETECTED → TX_ACTIVE → TX_ENDING → IDLE
"""

import csv
import os
import queue
import threading
import time
from datetime import datetime
from collections import deque

import config as cfg
from config import (
    POLL_RATE_HZ, CARRIER_DETECT_POLLS, DCS_INITIAL_WINDOW_MS,
    sanitize_name,
)
from recording import write_wav, apply_sox_filter, cleanup_audio


class AppCore:
    """State machine that polls the GNU Radio flowgraph and orchestrates recording."""

    # States
    IDLE = "IDLE"
    CARRIER_DETECTED = "CARRIER_DETECTED"
    TX_ACTIVE = "TX_ACTIVE"
    TX_ENDING = "TX_ENDING"

    def __init__(self, flowgraph, audio_tap, dcs_decoder, telemetry_queue,
                 record=True, max_audio_mb=500, channel_name="FRS Ch 1",
                 dcs_code=0, dcs_mode="advisory", paths=None,
                 rssi_offset=0.0, rssi_calibration_tier=None):
        self._fg = flowgraph
        self._audio_tap = audio_tap
        self._dcs = dcs_decoder
        self._telem_q = telemetry_queue
        self._record = record
        self._max_audio_mb = max_audio_mb
        self._channel_name = channel_name
        self._dcs_code = dcs_code
        self._dcs_mode = dcs_mode
        self._file_prefix = sanitize_name(channel_name)
        self._channel_data_dir = paths.channel_data_dir if paths else "."
        self._audio_dir = paths.audio_dir if paths else "audio"
        self._rssi_offset = rssi_offset
        self._rssi_calibration_tier = rssi_calibration_tier

        self._state = self.IDLE
        self._running = False
        self._thread = None

        # State tracking
        self._squelch_open_count = 0
        self._squelch_closed_count = 0
        self._tx_start_time = None
        self._tx_peak_rssi = -100.0
        self._tx_dcs_confirmed = False
        self._tx_dcs_polarity = "unknown"
        self._carrier_detect_time = None

        # Statistics
        self._tx_count = 0
        self._tx_with_dcs_match = 0
        self._tx_without_dcs_match = 0
        self._tx_log = deque(maxlen=200)

        os.makedirs(self._channel_data_dir, exist_ok=True)
        if record:
            os.makedirs(self._audio_dir, exist_ok=True)

        self._load_csv_log()

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        # Save in-progress recording
        try:
            if self._state in (self.TX_ACTIVE, self.TX_ENDING, self.CARRIER_DETECTED):
                self._finalize_tx()
        except Exception as e:
            print(f"[AppCore] finalize error during shutdown: {e}")

    def _poll_loop(self):
        interval = 1.0 / POLL_RATE_HZ
        while self._running:
            t0 = time.monotonic()
            try:
                self._poll()
            except Exception as e:
                print(f"[AppCore] poll error: {e}")
            elapsed = time.monotonic() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _apply_offset(self, rssi_dbfs):
        """Apply calibration offset to raw RSSI."""
        return rssi_dbfs + self._rssi_offset

    def _poll(self):
        rssi_raw = self._fg.get_rssi()
        rssi = self._apply_offset(rssi_raw)
        squelch_open = self._fg.get_squelch_open()
        dcs_detected = self._dcs.get_detected() if self._dcs else False
        dcs_polarity = self._dcs.get_polarity() if self._dcs else "unknown"

        # Update DCS status during active TX
        if self._state in (self.TX_ACTIVE, self.TX_ENDING, self.CARRIER_DETECTED):
            if dcs_detected and not self._tx_dcs_confirmed:
                self._tx_dcs_confirmed = True
                self._tx_dcs_polarity = dcs_polarity

            if rssi > self._tx_peak_rssi:
                self._tx_peak_rssi = rssi

        if self._state == self.IDLE:
            if squelch_open:
                self._squelch_open_count += 1
            else:
                self._squelch_open_count = 0

            if self._squelch_open_count >= CARRIER_DETECT_POLLS:
                self._transition_to_carrier_detected(rssi)

        elif self._state == self.CARRIER_DETECTED:
            # Wait for initial DCS window, then transition to TX_ACTIVE
            elapsed_ms = (time.monotonic() - self._carrier_detect_time) * 1000
            if elapsed_ms >= DCS_INITIAL_WINDOW_MS:
                self._state = self.TX_ACTIVE
            if not squelch_open:
                # Squelch closed during carrier detect — go to TX_ENDING
                self._state = self.TX_ENDING
                self._squelch_closed_count = 1

        elif self._state == self.TX_ACTIVE:
            if not squelch_open:
                self._state = self.TX_ENDING
                self._squelch_closed_count = 1
            else:
                self._squelch_closed_count = 0

        elif self._state == self.TX_ENDING:
            if squelch_open:
                self._state = self.TX_ACTIVE
                self._squelch_closed_count = 0
            else:
                self._squelch_closed_count += 1
                if self._squelch_closed_count >= cfg.TX_ENDING_POLLS:
                    self._finalize_tx()
                    self._state = self.IDLE

        # Push telemetry
        self._push_telemetry(rssi, squelch_open, dcs_detected, dcs_polarity)

    def _transition_to_carrier_detected(self, rssi):
        self._state = self.CARRIER_DETECTED
        self._carrier_detect_time = time.monotonic()
        self._tx_start_time = datetime.now()
        self._tx_peak_rssi = rssi
        self._tx_dcs_confirmed = False
        self._tx_dcs_polarity = "unknown"
        self._squelch_closed_count = 0

        # Reset DCS decoder for new TX
        if self._dcs:
            self._dcs.reset()

        # Start recording
        if self._audio_tap:
            self._audio_tap.start_recording()

    def _finalize_tx(self):
        """Finalize current transmission: save recording, log CSV."""
        filename = None

        if self._audio_tap and self._audio_tap.is_recording():
            raw_audio = self._audio_tap.stop_recording()
            duration = len(raw_audio) / (2 * 8000)  # 16-bit mono

            if self._record and duration >= 0.5 and self._tx_start_time:
                ts = self._tx_start_time.strftime("%Y%m%d_%H%M%S")
                filename = os.path.join(self._audio_dir, f"{self._file_prefix}_{ts}.wav")
                write_wav(filename, raw_audio)
                apply_sox_filter(filename)
                cleanup_audio(self._max_audio_mb, self._audio_dir)
        else:
            duration = 0.0

        self._tx_count += 1

        # Track DCS match counters
        if self._tx_dcs_confirmed:
            self._tx_with_dcs_match += 1
        else:
            self._tx_without_dcs_match += 1

        entry = {
            "time": self._tx_start_time.strftime("%H:%M:%S") if self._tx_start_time else "??:??:??",
            "date": self._tx_start_time.strftime("%Y-%m-%d") if self._tx_start_time else "",
            "duration": duration,
            "peak_rssi": self._tx_peak_rssi,
            "dcs_confirmed": self._tx_dcs_confirmed,
            "dcs_polarity": self._tx_dcs_polarity,
            "filename": filename,
        }

        # In strict mode, flag non-matching transmissions
        if self._dcs_mode == "strict" and not self._tx_dcs_confirmed:
            entry["dcs_mismatch"] = True

        self._tx_log.append(entry)

        # CSV logging
        self._log_csv(entry)

        # Reset
        self._squelch_open_count = 0
        self._squelch_closed_count = 0
        self._tx_start_time = None

    def _log_csv(self, entry):
        csv_path = os.path.join(self._channel_data_dir, f"{datetime.now().strftime('%Y%m%d')}.csv")
        write_header = not os.path.exists(csv_path)
        try:
            with open(csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    rssi_col = "peak_rssi_" + self._rssi_unit_label().replace(" ", "_").replace("~", "approx_")
                    cols = [
                        "date", "time", "duration_sec", rssi_col,
                        "dcs_confirmed", "dcs_polarity", "filename",
                    ]
                    if self._dcs_mode == "strict":
                        cols.append("dcs_mismatch")
                    writer.writerow(cols)
                row = [
                    entry["date"],
                    entry["time"],
                    f"{entry['duration']:.1f}",
                    f"{entry['peak_rssi']:.1f}",
                    entry["dcs_confirmed"],
                    entry["dcs_polarity"],
                    os.path.basename(entry["filename"]) if entry["filename"] else "",
                ]
                if self._dcs_mode == "strict":
                    row.append(entry.get("dcs_mismatch", False))
                writer.writerow(row)
        except Exception as e:
            print(f"[AppCore] CSV log error: {e}")

    def _load_csv_log(self):
        """Load today's CSV log into the in-memory TX log on startup."""
        csv_path = os.path.join(self._channel_data_dir, f"{datetime.now().strftime('%Y%m%d')}.csv")
        if not os.path.exists(csv_path):
            return
        try:
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    filename = row.get("filename", "")
                    if filename:
                        filename = os.path.join(self._audio_dir, filename)
                    # Find RSSI column regardless of unit suffix
                    rssi_val = -100.0
                    for key in row:
                        if key.startswith("peak_rssi"):
                            try:
                                rssi_val = float(row[key])
                            except (ValueError, TypeError):
                                pass
                            break
                    entry = {
                        "date": row.get("date", ""),
                        "time": row.get("time", ""),
                        "duration": float(row.get("duration_sec", 0)),
                        "peak_rssi": rssi_val,
                        "dcs_confirmed": row.get("dcs_confirmed", "") == "True",
                        "dcs_polarity": row.get("dcs_polarity", "unknown"),
                        "filename": filename if filename else None,
                    }
                    if row.get("dcs_mismatch"):
                        entry["dcs_mismatch"] = row["dcs_mismatch"] == "True"
                    self._tx_log.append(entry)
                self._tx_count = len(self._tx_log)
                # Reconstruct DCS match counters from loaded log
                self._tx_with_dcs_match = sum(1 for e in self._tx_log if e.get("dcs_confirmed"))
                self._tx_without_dcs_match = self._tx_count - self._tx_with_dcs_match
                print(f"[AppCore] Loaded {self._tx_count} entries from {os.path.basename(csv_path)}")
        except Exception as e:
            print(f"[AppCore] Could not load CSV log: {e}")

    def _push_telemetry(self, rssi, squelch_open, dcs_detected, dcs_polarity):
        """Push telemetry dict to queue for WebSocket broadcast."""
        rec_duration = 0.0
        if self._audio_tap:
            rec_duration = self._audio_tap.get_recording_duration()

        dcs_match_rate = 0.0
        if self._tx_count > 0:
            dcs_match_rate = round(self._tx_with_dcs_match / self._tx_count * 100, 1)

        telem = {
            "rssi": round(rssi, 1),
            "rssi_unit": self._rssi_unit_label(),
            "squelch_open": squelch_open,
            "squelch_threshold": self._fg.get_squelch_threshold(),
            "dcs_detected": dcs_detected,
            "dcs_polarity": dcs_polarity,
            "state": self._state,
            "tx_count": self._tx_count,
            "tx_with_dcs_match": self._tx_with_dcs_match,
            "tx_without_dcs_match": self._tx_without_dcs_match,
            "dcs_match_rate": dcs_match_rate,
            "recording": self._audio_tap.is_recording() if self._audio_tap else False,
            "recording_duration": round(rec_duration, 1),
            "gain": self._fg.get_gain(),
            "timestamp": time.time(),
        }

        try:
            self._telem_q.put_nowait(telem)
        except queue.Full:
            # Drop oldest
            try:
                self._telem_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._telem_q.put_nowait(telem)
            except queue.Full:
                pass

    def _rssi_unit_label(self):
        if self._rssi_calibration_tier == "absolute":
            return "dBm"
        elif self._rssi_calibration_tier == "field":
            return "~dBm (field cal)"
        return "dBFS"

    def get_tx_log(self):
        """Return list of transmission log entries."""
        return list(self._tx_log)

    def delete_tx(self, index):
        """Delete a transmission entry by index. Removes WAV file and rewrites CSV."""
        if index < 0 or index >= len(self._tx_log):
            return False

        entry = self._tx_log[index]

        # Update DCS match counters
        if entry.get("dcs_confirmed"):
            self._tx_with_dcs_match = max(0, self._tx_with_dcs_match - 1)
        else:
            self._tx_without_dcs_match = max(0, self._tx_without_dcs_match - 1)

        # Delete WAV file if it exists
        if entry.get("filename"):
            try:
                if os.path.isfile(entry["filename"]):
                    os.remove(entry["filename"])
            except Exception as e:
                print(f"[AppCore] Could not delete {entry['filename']}: {e}")

        # Remove from in-memory log
        del self._tx_log[index]
        self._tx_count = max(0, self._tx_count - 1)

        # Rewrite today's CSV from remaining entries
        self._rewrite_csv()
        return True

    def _rewrite_csv(self):
        """Rewrite today's CSV from current in-memory log."""
        csv_path = os.path.join(self._channel_data_dir, f"{datetime.now().strftime('%Y%m%d')}.csv")
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # Separate today's entries from other days
            today_entries = [e for e in self._tx_log if e.get("date") == today]

            rssi_col = "peak_rssi_" + self._rssi_unit_label().replace(" ", "_").replace("~", "approx_")
            cols = [
                "date", "time", "duration_sec", rssi_col,
                "dcs_confirmed", "dcs_polarity", "filename",
            ]
            if self._dcs_mode == "strict":
                cols.append("dcs_mismatch")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                for entry in today_entries:
                    row = [
                        entry["date"],
                        entry["time"],
                        f"{entry['duration']:.1f}",
                        f"{entry['peak_rssi']:.1f}",
                        entry["dcs_confirmed"],
                        entry["dcs_polarity"],
                        os.path.basename(entry["filename"]) if entry.get("filename") else "",
                    ]
                    if self._dcs_mode == "strict":
                        row.append(entry.get("dcs_mismatch", False))
                    writer.writerow(row)
        except Exception as e:
            print(f"[AppCore] CSV rewrite error: {e}")

    def get_state(self):
        return self._state

    def get_tx_count(self):
        return self._tx_count
