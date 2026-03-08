"""GNU Radio flowgraph for SDR monitor.

RTL-SDR → freq_xlating_filter → squelch → NBFM → voice processing → audio
                                        → RSSI probe

Voice processing chain (after NBFM, before audio tap):
  DC blocker → HPF → LPF → AGC (optional) → headroom limiter → float_to_short

DCS decoder taps raw NBFM output (before voice filtering) to preserve the
146 Hz sub-audible tone needed for decoding.
"""

from gnuradio import gr, analog, blocks, filter as gr_filter
from gnuradio.fft import window
import osmosdr
import numpy as np

from config import (
    FREQ_HZ, SDR_SAMPLE_RATE, CHANNEL_RATE, AUDIO_RATE, DECIMATION,
    RF_GAIN, SQUELCH_THRESHOLD_DB, SQUELCH_ALPHA, SQUELCH_RAMP,
    RSSI_AVERAGE_LEN, AUDIO_PRESETS, DEFAULT_AUDIO_PRESET, FM_TAU,
)


class MonitorFlowgraph(gr.top_block):
    def __init__(self, freq=FREQ_HZ, gain=RF_GAIN,
                 squelch_threshold=SQUELCH_THRESHOLD_DB,
                 audio_tap=None, dcs_decoder=None,
                 audio_preset=None, tau=None):
        gr.top_block.__init__(self, "SDR Monitor")

        self._freq = freq
        self._gain = gain

        preset_name = audio_preset or DEFAULT_AUDIO_PRESET
        preset = AUDIO_PRESETS[preset_name]
        deemph_tau = tau if tau is not None else FM_TAU

        # ── RTL-SDR Source ──────────────────────────────
        self.src = osmosdr.source(args="numchan=1")
        self.src.set_sample_rate(SDR_SAMPLE_RATE)
        self.src.set_center_freq(freq)
        self.src.set_gain(gain)
        self.src.set_if_gain(20)
        self.src.set_bb_gain(20)
        self.src.set_bandwidth(0)  # auto

        # ── Channel filter: decimate 240k → 24k ────────
        channel_taps = gr_filter.firdes.low_pass(
            1.0,                    # gain
            SDR_SAMPLE_RATE,        # sampling rate
            7500,                   # cutoff (NFM ±5kHz deviation + margin)
            2500,                   # transition width
            window.WIN_HAMMING,
        )
        self.chan_filter = gr_filter.freq_xlating_fir_filter_ccc(
            DECIMATION,             # decimation factor
            channel_taps,
            0,                      # center freq offset (already centered)
            SDR_SAMPLE_RATE,
        )

        # ── RF Power Squelch ────────────────────────────
        self.squelch = analog.pwr_squelch_cc(
            squelch_threshold,
            SQUELCH_ALPHA,
            SQUELCH_RAMP,
            False,                  # gate=False: output zeros when squelched
        )

        # ── NBFM Demodulator ───────────────────────────
        # nbfm_rx requires tau > 0 (divides by it internally). For "no de-emphasis",
        # use tau=1e-9: corner freq = 1 GHz, filter is flat (<0.01 dB across audio).
        effective_tau = deemph_tau if deemph_tau and deemph_tau > 0 else 1e-9
        self.nbfm = analog.nbfm_rx(
            audio_rate=AUDIO_RATE,
            quad_rate=CHANNEL_RATE,
            tau=effective_tau,
            max_dev=5000,
        )

        # ── RSSI Probe (pre-squelch) ──────────────────
        self.mag_sq = blocks.complex_to_mag_squared(1)
        self.avg = blocks.moving_average_ff(RSSI_AVERAGE_LEN, 1.0 / RSSI_AVERAGE_LEN)
        self.log = blocks.nlog10_ff(10, 1, 0)
        self.rssi_probe = blocks.probe_signal_f()

        # ── Voice Processing Chain ─────────────────────
        # Order: DC blocker → HPF → LPF → [AGC] → headroom → float_to_short

        # DC blocker: removes demod baseline wander
        self.dc_blocker = gr_filter.dc_blocker_ff(32, True)

        # High-pass filter: removes DCS tone and sub-audio rumble
        hpf_taps = gr_filter.firdes.high_pass(
            1.0,
            AUDIO_RATE,
            preset["hpf_cutoff"],
            preset["hpf_transition"],
            window.WIN_HAMMING,
        )
        self.audio_hpf = gr_filter.fir_filter_fff(1, hpf_taps)

        # Low-pass filter: removes high-frequency hiss
        lpf_taps = gr_filter.firdes.low_pass(
            1.0,
            AUDIO_RATE,
            preset["lpf_cutoff"],
            preset["lpf_transition"],
            window.WIN_HAMMING,
        )
        self.audio_lpf = gr_filter.fir_filter_fff(1, lpf_taps)

        # AGC: stabilizes loudness across weak/strong signals
        self._agc_enabled = preset["agc_enabled"]
        if self._agc_enabled:
            self.agc = analog.agc2_ff(
                preset["agc_attack"],
                preset["agc_decay"],
                preset["agc_reference"],
                preset["agc_gain"],
            )
            self.agc.set_max_gain(preset["agc_max_gain"])

        # Headroom limiter: prevents clipping at int16 conversion
        self.headroom = blocks.multiply_const_ff(preset["headroom"])

        # Float to int16
        self.float_to_short = blocks.float_to_short(1, 32767)

        # ── Connect the flowgraph ──────────────────────

        # Main signal path: src → chan_filter
        self.connect(self.src, self.chan_filter)

        # RSSI path (pre-squelch): chan_filter → mag² → avg → log10 → probe
        self.connect(self.chan_filter, self.mag_sq, self.avg, self.log, self.rssi_probe)

        # Demod path: chan_filter → squelch → nbfm
        self.connect(self.chan_filter, self.squelch, self.nbfm)

        # Voice path: nbfm → dc_blocker → hpf → lpf → [agc] → headroom → float_to_short
        if self._agc_enabled:
            self.connect(self.nbfm, self.dc_blocker, self.audio_hpf,
                         self.audio_lpf, self.agc, self.headroom, self.float_to_short)
        else:
            self.connect(self.nbfm, self.dc_blocker, self.audio_hpf,
                         self.audio_lpf, self.headroom, self.float_to_short)

        # Audio tap (recording + streaming) — after full voice processing
        if audio_tap is not None:
            self.connect(self.float_to_short, audio_tap)

        # DCS decoder — taps raw NBFM output BEFORE voice filtering
        if dcs_decoder is not None:
            self.connect(self.nbfm, dcs_decoder)

        # Null sink if no audio tap (keep flowgraph running)
        if audio_tap is None:
            self.null_sink = blocks.null_sink(gr.sizeof_short)
            self.connect(self.float_to_short, self.null_sink)

    def get_rssi(self):
        """Return current RSSI in dBFS."""
        try:
            return self.rssi_probe.level()
        except Exception:
            return -100.0

    def get_squelch_open(self):
        """Return True if squelch is currently open (signal present)."""
        try:
            return self.squelch.unmuted()
        except Exception:
            return False

    def set_squelch_threshold(self, db):
        """Update squelch threshold at runtime."""
        self.squelch.set_threshold(db)

    def set_gain(self, gain):
        """Update tuner gain at runtime."""
        self._gain = gain
        self.src.set_gain(gain)

    def get_gain(self):
        return self._gain

    def get_squelch_threshold(self):
        return self.squelch.threshold()


if __name__ == "__main__":
    """Standalone test: print RSSI and squelch state."""
    import time
    import signal
    import sys

    fg = MonitorFlowgraph()
    fg.start()

    def sighandler(sig, frame):
        print("\nStopping...")
        fg.stop()
        fg.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, sighandler)

    print(f"Monitoring {FREQ_HZ/1e6:.3f} MHz | Squelch: {SQUELCH_THRESHOLD_DB} dB")
    print(f"{'RSSI (dBFS)':>14}  {'Squelch':>8}")
    print("-" * 30)

    while True:
        rssi = fg.get_rssi()
        sq = fg.get_squelch_open()
        status = "OPEN" if sq else "closed"
        print(f"{rssi:14.1f}  {status:>8}", end="\r")
        time.sleep(0.1)
