"""AudioTapBlock — GNU Radio sink for recording and live audio streaming.

Receives int16 audio samples, maintains a ring buffer for pre-trigger
recording, and supports live audio streaming via callback.

The ring buffer fills continuously regardless of recording state,
ensuring pre-trigger audio is always available for the next transmission.
"""

import threading
import numpy as np
from collections import deque
from gnuradio import gr

from config import AUDIO_RATE, CHUNK_SAMPLES, RING_SIZE


class AudioTapBlock(gr.sync_block):
    """GNU Radio sink block that captures audio for recording and streaming.

    Input: int16 (short) audio samples at AUDIO_RATE
    Output: none (sink block)

    Features:
    - Ring buffer of ~2s pre-trigger audio (always filling)
    - Recording mode: accumulates audio chunks alongside ring buffer
    - Live audio callback for WebSocket streaming
    """

    def __init__(self, live_callback=None):
        gr.sync_block.__init__(
            self,
            name="AudioTap",
            in_sig=[np.int16],
            out_sig=None,
        )
        self._lock = threading.Lock()
        self._ring = deque(maxlen=RING_SIZE)
        self._recording = False
        self._recorded_chunks = []
        self._chunk_buffer = []
        self._live_callback = live_callback
        self._seq = 0

    def work(self, input_items, output_items):
        samples = input_items[0]

        # Accumulate into chunk buffer
        self._chunk_buffer.extend(samples.tolist())

        while len(self._chunk_buffer) >= CHUNK_SAMPLES:
            chunk_data = self._chunk_buffer[:CHUNK_SAMPLES]
            self._chunk_buffer = self._chunk_buffer[CHUNK_SAMPLES:]

            chunk_bytes = np.array(chunk_data, dtype=np.int16).tobytes()

            with self._lock:
                # Always fill ring buffer — pre-trigger is never empty
                self._ring.append(chunk_bytes)

                # Also append to recording if active
                if self._recording:
                    self._recorded_chunks.append(chunk_bytes)

            # Live audio callback
            if self._live_callback:
                self._seq += 1
                try:
                    self._live_callback(self._seq, chunk_bytes)
                except Exception:
                    pass

        return len(samples)

    def start_recording(self):
        """Begin recording. Copies ring buffer contents as pre-trigger audio."""
        with self._lock:
            # Copy ring buffer as pre-trigger — ring keeps filling independently
            self._recorded_chunks = list(self._ring)
            self._recording = True

    def stop_recording(self):
        """Stop recording and return all captured audio as bytes."""
        with self._lock:
            self._recording = False
            audio = b"".join(self._recorded_chunks)
            self._recorded_chunks = []
            return audio

    def is_recording(self):
        with self._lock:
            return self._recording

    def get_recording_duration(self):
        """Return current recording duration in seconds."""
        with self._lock:
            n_chunks = len(self._recorded_chunks)
        return n_chunks * CHUNK_SAMPLES / AUDIO_RATE
