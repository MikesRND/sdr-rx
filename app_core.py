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

# Max pending finalize jobs before applying backpressure
_FINALIZE_QUEUE_MAX = 4


class AppCore:
    """State machine that polls the GNU Radio flowgraph and orchestrates recording."""

    # States
    IDLE = "IDLE"
    CARRIER_DETECTED = "CARRIER_DETECTED"
    TX_ACTIVE = "TX_ACTIVE"
    TX_ENDING = "TX_ENDING"

    def __init__(self, flowgraph, audio_tap, dcs_decoder, telemetry_queue,
                 record=True, max_audio_mb=500, channel_name="FRS Ch 1",
                 dcs_code=0, dcs_mode="advisory", paths=None, *, channel_id):
        self._fg = flowgraph
        self._channel_id = channel_id
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
        self._tx_seq = 0

        # Background finalization — WAV/sox/cleanup off the poll thread.
        # Single worker thread with bounded queue for backpressure.
        self._finalize_lock = threading.Lock()
        self._finalize_queue = queue.Queue(maxsize=_FINALIZE_QUEUE_MAX)
        self._finalize_canceled = set()  # job IDs canceled by delete_tx
        self._finalize_thread = None
        self._finalize_shutdown = False

        # Finalize counters (observable via telemetry)
        self._finalize_pending = 0
        self._finalize_written = 0
        self._finalize_failed = 0
        self._finalize_canceled_count = 0
        self._finalize_dropped = 0

        os.makedirs(self._channel_data_dir, exist_ok=True)
        if record:
            os.makedirs(self._audio_dir, exist_ok=True)

        self._load_csv_log()

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self._finalize_thread = threading.Thread(
            target=self._finalize_worker, daemon=True,
            name=f"finalize-{self._channel_id}",
        )
        self._finalize_thread.start()

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
        # Drain the finalize worker without blocking forever if the queue is full.
        self._finalize_shutdown = True
        deadline = time.monotonic() + 5.0
        while True:
            try:
                self._finalize_queue.put(None, timeout=0.1)  # sentinel
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    print("[AppCore] finalize queue full during shutdown; worker will exit when queue drains")
                    break
        if self._finalize_thread:
            self._finalize_thread.join(timeout=15)

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

    def _poll(self):
        rssi = self._fg.get_rssi(self._channel_id)
        squelch_open = self._fg.get_squelch_open(self._channel_id)
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
        """Finalize current transmission: stop recording, log, enqueue WAV write.

        Synchronous (on poll thread): stop_recording, counters, TX log, CSV.
        Asynchronous (finalize worker): write_wav, apply_sox_filter, cleanup_audio.
        This keeps the poll loop responsive so new transmissions are detected immediately.
        """
        filename = None
        raw_audio = None
        job_id = None

        if self._audio_tap and self._audio_tap.is_recording():
            raw_audio = self._audio_tap.stop_recording()
            duration = len(raw_audio) / (2 * 8000)  # 16-bit mono

            if self._record and duration >= 0.5 and self._tx_start_time:
                self._tx_seq += 1
                ts = self._tx_start_time.strftime("%Y%m%d_%H%M%S_%f")
                job_id = f"{ts}_{self._tx_seq}"
                filename = os.path.join(
                    self._audio_dir, f"{self._file_prefix}_{job_id}.wav",
                )
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
            "_job_id": job_id,
        }

        # In strict mode, flag non-matching transmissions
        if self._dcs_mode == "strict" and not self._tx_dcs_confirmed:
            entry["dcs_mismatch"] = True

        self._tx_log.append(entry)

        # CSV logging
        self._log_csv(entry)

        # Enqueue background WAV write
        if filename and raw_audio and job_id:
            job = (job_id, filename, raw_audio)
            try:
                self._finalize_queue.put_nowait(job)
                with self._finalize_lock:
                    self._finalize_pending += 1
            except queue.Full:
                # Backpressure: queue full, skip sox and write raw WAV inline
                with self._finalize_lock:
                    self._finalize_dropped += 1
                print(f"[AppCore] finalize backlog full, writing raw WAV inline: {os.path.basename(filename)}")
                try:
                    write_wav(filename, raw_audio)
                    # If user deleted while inline write was in progress, remove the file.
                    with self._finalize_lock:
                        canceled = job_id in self._finalize_canceled
                        if canceled:
                            self._finalize_canceled.discard(job_id)
                            self._finalize_canceled_count += 1
                    if canceled:
                        try:
                            os.remove(filename)
                        except OSError:
                            pass
                except Exception as e:
                    print(f"[AppCore] inline WAV write error: {e}")

        # Reset
        self._squelch_open_count = 0
        self._squelch_closed_count = 0
        self._tx_start_time = None

    def _finalize_worker(self):
        """Background worker: dequeues (job_id, filename, raw_audio) jobs, writes WAV + sox."""
        while True:
            job = self._finalize_queue.get()
            if job is None:
                break  # shutdown sentinel

            job_id, filename, raw_audio = job

            # Check cancellation before writing
            with self._finalize_lock:
                if job_id in self._finalize_canceled:
                    self._finalize_canceled.discard(job_id)
                    self._finalize_canceled_count += 1
                    self._finalize_pending = max(0, self._finalize_pending - 1)
                    print(f"[AppCore] finalize canceled (pre-write): {os.path.basename(filename)}")
                    continue

            try:
                write_wav(filename, raw_audio)

                # Check cancellation after write but before sox
                with self._finalize_lock:
                    if job_id in self._finalize_canceled:
                        self._finalize_canceled.discard(job_id)
                        self._finalize_canceled_count += 1
                        self._finalize_pending = max(0, self._finalize_pending - 1)
                        # Clean up the file we just wrote
                        try:
                            os.remove(filename)
                        except OSError:
                            pass
                        print(f"[AppCore] finalize canceled (post-write): {os.path.basename(filename)}")
                        continue

                # Skip sox when backlog is building up — write raw WAV to keep up
                if self._finalize_queue.qsize() >= _FINALIZE_QUEUE_MAX - 1:
                    print(f"[AppCore] skipping sox (backlog): {os.path.basename(filename)}")
                else:
                    apply_sox_filter(filename)

                cleanup_audio(self._max_audio_mb, self._audio_dir)

                with self._finalize_lock:
                    self._finalize_written += 1
                    self._finalize_pending = max(0, self._finalize_pending - 1)
            except Exception as e:
                with self._finalize_lock:
                    self._finalize_failed += 1
                    self._finalize_pending = max(0, self._finalize_pending - 1)
                print(f"[AppCore] finalize error: {e}")

    def cancel_finalize(self, job_id):
        """Mark a job ID as canceled so the finalize writer skips/removes it."""
        if not job_id:
            return
        with self._finalize_lock:
            self._finalize_canceled.add(job_id)

    def _log_csv(self, entry):
        csv_path = os.path.join(self._channel_data_dir, f"{datetime.now().strftime('%Y%m%d')}.csv")
        write_header = not os.path.exists(csv_path)
        try:
            with open(csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    rssi_col = "peak_rssi_dBFS"
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
            "rssi_unit": "dBFS",
            "squelch_open": squelch_open,
            "squelch_threshold": self._fg.get_squelch_threshold(self._channel_id),
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
            "finalize_pending": self._finalize_pending,
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

        # Cancel pending finalize and/or delete existing WAV file
        if entry.get("filename"):
            self.cancel_finalize(entry.get("_job_id"))
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

            rssi_col = "peak_rssi_dBFS"
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
