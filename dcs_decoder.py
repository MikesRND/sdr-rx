"""DCS (Digital Coded Squelch) decoder block.

Custom GNU Radio sync_block that decodes DCS Golay(23,12) codewords
from demodulated NBFM audio. Supports dual-polarity detection (N/I).
"""

import numpy as np
from gnuradio import gr

from config import DCS_BITRATE, DCS_GOLAY_POLY, DCS_CONSECUTIVE_MATCHES, AUDIO_RATE


def _compute_golay_parity(data_11bits):
    """Compute 12-bit Golay parity for an 11-bit data word using polynomial 0xAE3."""
    # Golay(23,12): 12 data bits (we use 11 + fixed 0 MSB) → 11 parity bits
    # Actually for DCS: 12-bit codeword = 3-bit fixed + 9-bit code, then 11-bit parity
    # Total 23 bits
    reg = data_11bits << 12
    for i in range(11, -1, -1):
        if reg & (1 << (i + 11)):
            reg ^= (DCS_GOLAY_POLY << i)
    return reg & 0x7FF


def _build_dcs_codeword(code_num):
    """Build the 23-bit DCS codeword for a given code number.

    DCS codeword format: [b0..b8=octal code LSB first] [b9..b11=fixed 001] [b12..b22=parity]
    The 9-bit code is the octal code in reversed bit order.
    """
    # Convert octal code number to 9-bit reversed
    octal_str = f"{code_num:03o}"
    code_bits = 0
    for i, ch in enumerate(octal_str):
        digit = int(ch)
        # Each octal digit is 3 bits, placed LSB first
        for b in range(3):
            if digit & (1 << b):
                code_bits |= 1 << (i * 3 + b)

    # 12-bit data: 9-bit code + 3-bit fixed (100 → bit9=1, bit10=0, bit11=0)
    data_12 = code_bits | (0b100 << 9)

    # Compute 11-bit Golay parity
    parity = _compute_golay_parity(data_12)

    # 23-bit codeword
    codeword = data_12 | (parity << 12)
    return codeword


class DCSDecoderBlock(gr.sync_block):
    """GNU Radio sync_block that decodes DCS from demodulated NBFM audio.

    Input: float audio samples at AUDIO_RATE (8 kHz)
    Output: none (message-based output via callback)

    Detection method:
    - LPF to isolate sub-300 Hz DCS signal
    - Zero-crossing bit clock recovery at 134.4 bps
    - 23-bit shift register correlation against target codeword
    - Dual-polarity: checks both normal and inverted codewords
    """

    def __init__(self, dcs_code, callback=None):
        gr.sync_block.__init__(
            self,
            name="DCSDecoder",
            in_sig=[np.float32],
            out_sig=None,
        )
        self._dcs_code = dcs_code
        self._callback = callback
        self._samples_per_bit = AUDIO_RATE / DCS_BITRATE  # ~59.5
        self._shift_reg = 0
        self._last_sign = 0
        self._samples_since_crossing = 0
        self._bit_phase = 0.0
        self._match_count_normal = 0
        self._match_count_inverted = 0
        self._detected = False
        self._detected_polarity = "unknown"

        # Compute target codewords from the configured DCS code
        self._codeword_normal = _build_dcs_codeword(dcs_code)
        self._codeword_inverted = self._codeword_normal ^ 0x7FFFFF
        self._codeword_mask = (1 << 23) - 1

        # Simple IIR low-pass filter state (cutoff ~250 Hz at 8 kHz)
        # Single-pole IIR: alpha = 2*pi*fc / (fs + 2*pi*fc)
        fc = 250.0
        self._lpf_alpha = (2 * np.pi * fc) / (AUDIO_RATE + 2 * np.pi * fc)
        self._lpf_state = 0.0

    def work(self, input_items, output_items):
        samples = input_items[0]

        for s in samples:
            # Low-pass filter
            self._lpf_state += self._lpf_alpha * (float(s) - self._lpf_state)
            filtered = self._lpf_state

            # Zero-crossing detection for bit clock
            current_sign = 1 if filtered >= 0 else 0
            self._samples_since_crossing += 1

            if current_sign != self._last_sign:
                # Zero crossing occurred — sync bit clock
                self._samples_since_crossing = 0
                self._last_sign = current_sign

            # Sample at mid-bit (half a bit period after crossing)
            self._bit_phase += 1.0
            if self._bit_phase >= self._samples_per_bit:
                self._bit_phase -= self._samples_per_bit

                # Determine bit value from current filtered sign
                bit = 1 if self._lpf_state >= 0 else 0

                # Shift into register
                self._shift_reg = ((self._shift_reg << 1) | bit) & self._codeword_mask

                # Check for normal polarity match
                if self._shift_reg == self._codeword_normal:
                    self._match_count_normal += 1
                    self._match_count_inverted = 0
                    if self._match_count_normal >= DCS_CONSECUTIVE_MATCHES and not self._detected:
                        self._detected = True
                        self._detected_polarity = "N"
                        if self._callback:
                            self._callback("N")
                # Check for inverted polarity match
                elif self._shift_reg == self._codeword_inverted:
                    self._match_count_inverted += 1
                    self._match_count_normal = 0
                    if self._match_count_inverted >= DCS_CONSECUTIVE_MATCHES and not self._detected:
                        self._detected = True
                        self._detected_polarity = "I"
                        if self._callback:
                            self._callback("I")
                else:
                    self._match_count_normal = 0
                    self._match_count_inverted = 0

        return len(samples)

    def get_detected(self):
        """Return True if configured DCS code has been detected."""
        return self._detected

    def get_polarity(self):
        """Return detected polarity: 'N', 'I', or 'unknown'."""
        return self._detected_polarity

    def reset(self):
        """Reset detection state for a new transmission."""
        self._detected = False
        self._detected_polarity = "unknown"
        self._match_count_normal = 0
        self._match_count_inverted = 0
        self._shift_reg = 0
        self._bit_phase = 0.0
        self._samples_since_crossing = 0
