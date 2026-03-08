"""GNU Radio flowgraph for SDR monitor — multi-channel receiver.

Single RTL-SDR source feeds N parallel demodulation chains:

RTL-SDR (sample_rate IQ, center = midpoint of channels)
  ├→ freq_xlating_fir #1 (offset) → squelch → NBFM → voice → audio_tap_1
  │                                                        → dcs_decoder_1
  └→ freq_xlating_fir #2 (offset) → squelch → NBFM → voice → audio_tap_2
                                                           → dcs_decoder_2

Each channel has independent squelch, RSSI probe, voice processing,
audio tap, and DCS decoder.

Voice processing chain (after NBFM, before audio tap):
  DC blocker → HPF → LPF → AGC (optional) → headroom limiter → float_to_short

DCS decoder taps raw NBFM output (before voice filtering) to preserve the
146 Hz sub-audible tone needed for decoding.
"""

import dataclasses

from gnuradio import gr, analog, blocks, filter as gr_filter
from gnuradio.fft import window
import osmosdr
import numpy as np

from config import (
    CHANNEL_RATE, AUDIO_RATE, DECIMATION,
    RF_GAIN, SQUELCH_THRESHOLD_DB, SQUELCH_ALPHA, SQUELCH_RAMP,
    RSSI_AVERAGE_LEN, AUDIO_PRESETS, DEFAULT_AUDIO_PRESET, FM_TAU,
    DEFAULT_RECEIVER, Receiver,
)


@dataclasses.dataclass
class ChannelConfig:
    """Configuration for a single channel within a receiver flowgraph."""
    channel_id: str
    freq_hz: int
    squelch_threshold: float
    audio_tap: object     # AudioTapBlock
    dcs_decoder: object   # DCSDecoderBlock


class _ChannelBlocks:
    """Container for all GNU Radio blocks belonging to one channel."""
    pass


class ReceiverFlowgraph(gr.top_block):
    """Multi-channel receiver flowgraph.

    One RTL-SDR source, N parallel demodulation chains. Each channel has
    independent squelch, RSSI, voice processing, audio tap, and DCS decoder.
    """

    def __init__(self, channels, receiver=None, gain=RF_GAIN,
                 audio_preset=None, tau=None):
        gr.top_block.__init__(self, "SDR Receiver")

        if receiver is None:
            receiver = DEFAULT_RECEIVER

        self._receiver = receiver
        self._gain = gain
        self._channels = {}  # channel_id → _ChannelBlocks

        preset_name = audio_preset or DEFAULT_AUDIO_PRESET
        preset = AUDIO_PRESETS[preset_name]
        deemph_tau = tau if tau is not None else FM_TAU

        # ── Validate channels ─────────────────────────────
        if len(channels) > receiver.max_channels:
            raise ValueError(
                f"Too many channels ({len(channels)}), "
                f"max {receiver.max_channels} per receiver."
            )

        freqs = [ch.freq_hz for ch in channels]
        freq_spread = max(freqs) - min(freqs) if len(freqs) > 1 else 0
        max_spread = receiver.sample_rate - CHANNEL_RATE
        if freq_spread > max_spread:
            raise ValueError(
                f"Channel frequency spread {freq_spread/1e3:.1f} kHz exceeds "
                f"usable bandwidth {max_spread/1e3:.1f} kHz "
                f"(sample_rate={receiver.sample_rate}, channel_rate={CHANNEL_RATE})."
            )

        # ── Compute center frequency ─────────────────────
        if len(freqs) == 1:
            center_freq = freqs[0]
        else:
            center_freq = (min(freqs) + max(freqs)) // 2

        self._center_freq = center_freq

        # ── RTL-SDR Source ────────────────────────────────
        self.src = osmosdr.source(args=f"numchan=1 rtl={receiver.device_index}")
        self.src.set_sample_rate(receiver.sample_rate)
        self.src.set_center_freq(center_freq)
        self.src.set_gain(gain)
        self.src.set_if_gain(receiver.if_gain)
        self.src.set_bb_gain(receiver.bb_gain)
        self.src.set_bandwidth(0)  # auto

        # ── Channel filter taps (shared — same for all channels) ──
        channel_taps = gr_filter.firdes.low_pass(
            1.0,                    # gain
            receiver.sample_rate,   # sampling rate
            7500,                   # cutoff (NFM ±5kHz deviation + margin)
            2500,                   # transition width
            window.WIN_HAMMING,
        )

        # ── Voice processing filter taps (shared) ────────
        hpf_taps = gr_filter.firdes.high_pass(
            1.0, AUDIO_RATE,
            preset["hpf_cutoff"], preset["hpf_transition"],
            window.WIN_HAMMING,
        )
        lpf_taps = gr_filter.firdes.low_pass(
            1.0, AUDIO_RATE,
            preset["lpf_cutoff"], preset["lpf_transition"],
            window.WIN_HAMMING,
        )

        effective_tau = deemph_tau if deemph_tau and deemph_tau > 0 else 1e-9

        # ── Build per-channel demod chains ────────────────
        for ch in channels:
            cb = _ChannelBlocks()
            offset = ch.freq_hz - center_freq

            # Channel filter: decimate and shift to baseband
            cb.chan_filter = gr_filter.freq_xlating_fir_filter_ccc(
                DECIMATION, channel_taps, offset, receiver.sample_rate,
            )

            # RSSI probe (pre-squelch): chan_filter → mag² → avg → log10 → probe
            cb.mag_sq = blocks.complex_to_mag_squared(1)
            cb.avg = blocks.moving_average_ff(RSSI_AVERAGE_LEN, 1.0 / RSSI_AVERAGE_LEN)
            cb.log = blocks.nlog10_ff(10, 1, 0)
            cb.rssi_probe = blocks.probe_signal_f()

            # RF power squelch
            cb.squelch = analog.pwr_squelch_cc(
                ch.squelch_threshold, SQUELCH_ALPHA, SQUELCH_RAMP, False,
            )

            # NBFM demodulator
            cb.nbfm = analog.nbfm_rx(
                audio_rate=AUDIO_RATE, quad_rate=CHANNEL_RATE,
                tau=effective_tau, max_dev=5000,
            )

            # Voice processing: dc_blocker → hpf → lpf → [agc] → headroom → float_to_short
            cb.dc_blocker = gr_filter.dc_blocker_ff(32, True)
            cb.audio_hpf = gr_filter.fir_filter_fff(1, hpf_taps)
            cb.audio_lpf = gr_filter.fir_filter_fff(1, lpf_taps)

            cb.agc_enabled = preset["agc_enabled"]
            if cb.agc_enabled:
                cb.agc = analog.agc2_ff(
                    preset["agc_attack"], preset["agc_decay"],
                    preset["agc_reference"], preset["agc_gain"],
                )
                cb.agc.set_max_gain(preset["agc_max_gain"])

            cb.headroom = blocks.multiply_const_ff(preset["headroom"])
            cb.float_to_short = blocks.float_to_short(1, 32767)

            # ── Connect the chain ─────────────────────────
            # Source → channel filter
            self.connect(self.src, cb.chan_filter)

            # RSSI path (pre-squelch)
            self.connect(cb.chan_filter, cb.mag_sq, cb.avg, cb.log, cb.rssi_probe)

            # Demod path: chan_filter → squelch → nbfm
            self.connect(cb.chan_filter, cb.squelch, cb.nbfm)

            # Voice path
            if cb.agc_enabled:
                self.connect(cb.nbfm, cb.dc_blocker, cb.audio_hpf,
                             cb.audio_lpf, cb.agc, cb.headroom, cb.float_to_short)
            else:
                self.connect(cb.nbfm, cb.dc_blocker, cb.audio_hpf,
                             cb.audio_lpf, cb.headroom, cb.float_to_short)

            # Audio tap (recording + streaming)
            if ch.audio_tap is not None:
                self.connect(cb.float_to_short, ch.audio_tap)
            else:
                cb.null_sink = blocks.null_sink(gr.sizeof_short)
                self.connect(cb.float_to_short, cb.null_sink)

            # DCS decoder — taps raw NBFM output BEFORE voice filtering
            if ch.dcs_decoder is not None:
                self.connect(cb.nbfm, ch.dcs_decoder)

            self._channels[ch.channel_id] = cb

    # ── Per-channel accessors ─────────────────────────────

    def get_rssi(self, channel_id):
        """Return current RSSI in dBFS for the given channel."""
        try:
            return self._channels[channel_id].rssi_probe.level()
        except Exception:
            return -100.0

    def get_squelch_open(self, channel_id):
        """Return True if squelch is currently open for the given channel."""
        try:
            return self._channels[channel_id].squelch.unmuted()
        except Exception:
            return False

    def set_squelch_threshold(self, channel_id, db):
        """Update squelch threshold at runtime for the given channel."""
        self._channels[channel_id].squelch.set_threshold(db)

    def get_squelch_threshold(self, channel_id):
        """Return current squelch threshold for the given channel."""
        return self._channels[channel_id].squelch.threshold()

    # ── Shared (receiver-level) accessors ─────────────────

    def set_gain(self, gain):
        """Update tuner gain at runtime (shared across all channels)."""
        self._gain = gain
        self.src.set_gain(gain)

    def get_gain(self):
        return self._gain

    def get_center_freq(self):
        return self._center_freq

    def get_channel_ids(self):
        return list(self._channels.keys())


if __name__ == "__main__":
    """Standalone test: print RSSI and squelch state for default receiver."""
    import time
    import signal
    import sys
    from config import FREQ_HZ
    from audio_tap import AudioTapBlock
    from dcs_decoder import DCSDecoderBlock

    ch = ChannelConfig(
        channel_id="test",
        freq_hz=FREQ_HZ,
        squelch_threshold=SQUELCH_THRESHOLD_DB,
        audio_tap=AudioTapBlock(),
        dcs_decoder=DCSDecoderBlock(dcs_code=0),
    )
    fg = ReceiverFlowgraph(channels=[ch])
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
        rssi = fg.get_rssi("test")
        sq = fg.get_squelch_open("test")
        status = "OPEN" if sq else "closed"
        print(f"{rssi:14.1f}  {status:>8}", end="\r")
        time.sleep(0.1)
