"""WAV file writing, sox filtering, and disk cleanup."""

import glob
import os
import subprocess
import wave

from config import AUDIO_RATE


def write_wav(filename, raw_bytes, sample_rate=AUDIO_RATE):
    """Write raw 16-bit PCM to WAV file."""
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw_bytes)


def apply_sox_filter(filename):
    """Apply high-pass filter to strip DPL sub-audible tone."""
    try:
        filtered = filename.replace(".wav", "_filtered.wav")
        subprocess.run(
            ["sox", filename, filtered,
             "highpass", "300", "lowpass", "3400",
             "remix", "1", "1"],          # mono → stereo (both ears)
            capture_output=True, timeout=10,
        )
        os.replace(filtered, filename)
    except Exception:
        pass


def cleanup_audio(max_mb, audio_dir):
    """Delete oldest WAV files if total exceeds max_mb."""
    if not os.path.isdir(audio_dir):
        return 0
    files = sorted(glob.glob(os.path.join(audio_dir, "*.wav")))
    total = sum(os.path.getsize(f) for f in files)
    deleted = 0
    while total > max_mb * 1024 * 1024 and files:
        oldest = files.pop(0)
        total -= os.path.getsize(oldest)
        os.remove(oldest)
        deleted += 1
    return deleted
