from __future__ import annotations

import json
import math
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


def iq_i8_to_complex(raw: bytes) -> np.ndarray:
    if len(raw) < 4:
        return np.empty(0, dtype=np.complex64)
    usable = len(raw) - (len(raw) % 2)
    values = np.frombuffer(raw[:usable], dtype=np.int8).astype(np.float32)
    i = values[0::2] / 128.0
    q = values[1::2] / 128.0
    return (i + 1j * q).astype(np.complex64, copy=False)


def complex_to_i8_bytes(iq: np.ndarray) -> bytes:
    if iq.size == 0:
        return b""
    interleaved = np.empty(iq.size * 2, dtype=np.int8)
    interleaved[0::2] = np.clip(np.rint(iq.real * 127.0), -128, 127).astype(np.int8)
    interleaved[1::2] = np.clip(np.rint(iq.imag * 127.0), -128, 127).astype(np.int8)
    return interleaved.tobytes()


def dbfs_rms(x: np.ndarray, floor: float = -120.0) -> float:
    if x.size == 0:
        return floor
    rms = float(np.sqrt(np.mean(np.square(np.abs(x).astype(np.float32, copy=False)))))
    if rms <= 0 or not math.isfinite(rms):
        return floor
    return max(floor, 20.0 * math.log10(rms))


def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    if x.size == 0:
        return x.astype(np.float32, copy=False)
    window = max(1, int(window))
    if window == 1 or x.size < window:
        return x.astype(np.float32, copy=False)
    kernel = np.ones(window, dtype=np.float32) / float(window)
    return np.convolve(x.astype(np.float32, copy=False), kernel, mode="same").astype(np.float32)


def _resample_linear(x: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    if x.size < 2 or in_rate <= 0 or out_rate <= 0:
        return np.empty(0, dtype=np.float32)
    duration_s = float(x.size) / float(in_rate)
    out_len = max(1, int(round(duration_s * float(out_rate))))
    if out_len <= 1:
        return np.asarray(x[:1], dtype=np.float32)
    src = np.linspace(0.0, 1.0, x.size, endpoint=False, dtype=np.float64)
    dst = np.linspace(0.0, 1.0, out_len, endpoint=False, dtype=np.float64)
    return np.interp(dst, src, x.astype(np.float32, copy=False)).astype(np.float32)


def demodulate_nbfm_audio(iq: np.ndarray, sample_rate_sps: int, audio_rate_hz: int = 16_000) -> np.ndarray:
    if iq.size < 32 or sample_rate_sps <= 0:
        return np.empty(0, dtype=np.float32)
    centered = iq.astype(np.complex64, copy=False) - np.complex64(np.mean(iq))
    decim = max(1, int(round(float(sample_rate_sps) / 240_000.0)))
    narrow = centered[::decim]
    if narrow.size < 16:
        return np.empty(0, dtype=np.float32)
    prev = narrow[:-1]
    cur = narrow[1:]
    demod = np.angle(cur * np.conj(prev)).astype(np.float32, copy=False)
    if demod.size < 16:
        return np.empty(0, dtype=np.float32)
    demod -= float(np.mean(demod))
    smoothed = moving_average(demod, window=5)
    filtered = moving_average(smoothed, window=9)
    working_rate = max(1, int(round(float(sample_rate_sps) / float(decim))))
    audio = _resample_linear(filtered, working_rate, audio_rate_hz)
    if audio.size == 0:
        return audio
    audio -= float(np.mean(audio))
    peak = float(np.max(np.abs(audio)))
    if peak > 1e-6:
        audio = np.clip(audio / peak, -1.0, 1.0)
    return audio.astype(np.float32, copy=False)


def _bandwidth_from_audio(audio: np.ndarray, sample_rate_hz: int) -> tuple[float, float]:
    if audio.size < 256 or sample_rate_hz <= 0:
        return 0.0, 0.0
    n = min(audio.size, 1 << int(np.floor(np.log2(audio.size))))
    if n < 256:
        return 0.0, 0.0
    windowed = audio[-n:] * np.hanning(n).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / float(sample_rate_hz))
    speech_mask = (freqs >= 250.0) & (freqs <= 4_000.0)
    if not np.any(speech_mask):
        return 0.0, 0.0
    speech_power = float(np.sum(spectrum[speech_mask]))
    total_mask = (freqs >= 50.0) & (freqs <= 6_000.0)
    total_power = float(np.sum(spectrum[total_mask])) + 1e-12
    if speech_power <= 0:
        return 0.0, 0.0
    speech_freqs = freqs[speech_mask]
    speech_spec = spectrum[speech_mask]
    centroid = float(np.sum(speech_freqs * speech_spec) / (np.sum(speech_spec) + 1e-12))
    cumulative = np.cumsum(speech_spec)
    lo = float(speech_freqs[np.searchsorted(cumulative, cumulative[-1] * 0.1)])
    hi = float(speech_freqs[min(len(speech_freqs) - 1, np.searchsorted(cumulative, cumulative[-1] * 0.9))])
    return max(0.0, hi - lo), max(0.0, speech_power / total_power)


@dataclass(frozen=True)
class CaptureMetadata:
    center_freq_hz: int
    sample_rate_sps: int
    duration_s: float
    device_id: str
    baseband_filter_hz: int
    lna_gain_db: int
    vga_gain_db: int
    amp_enable: bool
    captured_at: str
    iq_path: str


@dataclass(frozen=True)
class WalkieFeatures:
    signal_dbfs: float
    envelope_cv: float
    occupied_ratio: float
    saturation_ratio: float
    audio_rms_dbfs: float
    audio_peak: float
    audio_bandwidth_hz: float
    voice_band_ratio: float
    voice_activity_ratio: float
    freq_std_hz: float
    zero_crossing_rate: float


@dataclass(frozen=True)
class WalkieClassification:
    label: str
    confidence: float
    modulation: str
    features: WalkieFeatures

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["features"] = asdict(self.features)
        return payload


def classify_walkie_signal(iq: np.ndarray, sample_rate_sps: int) -> tuple[WalkieClassification, np.ndarray]:
    if iq.size < 64 or sample_rate_sps <= 0:
        features = WalkieFeatures(-120.0, 0.0, 0.0, 0.0, -120.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return WalkieClassification("no_signal", 0.0, "unknown", features), np.empty(0, dtype=np.float32)

    amp = np.abs(iq).astype(np.float32, copy=False)
    amp_mean = float(np.mean(amp)) + 1e-9
    amp_std = float(np.std(amp))
    envelope_cv = amp_std / amp_mean
    threshold = float(np.median(amp) + (0.5 * np.std(amp)))
    occupied_ratio = float(np.mean(amp > threshold))
    saturation_ratio = float(np.mean(amp >= 1.20))
    inst_phase = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32, copy=False)
    freq_std_hz = float(np.std(inst_phase) * float(sample_rate_sps) / (2.0 * math.pi)) if inst_phase.size else 0.0
    audio = demodulate_nbfm_audio(iq, sample_rate_sps)
    audio_rms_dbfs = dbfs_rms(audio)
    audio_peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    audio_bandwidth_hz, voice_band_ratio = _bandwidth_from_audio(audio, 16_000)
    if audio.size:
        env = moving_average(np.abs(audio).astype(np.float32, copy=False), window=max(8, audio.size // 200))
        voice_threshold = float(np.percentile(env, 65.0)) if env.size else 0.0
        voice_activity_ratio = float(np.mean(env >= voice_threshold)) if voice_threshold > 0 else 0.0
        zero_crossing_rate = float(np.mean(np.signbit(audio[1:]) != np.signbit(audio[:-1]))) if audio.size > 1 else 0.0
    else:
        voice_activity_ratio = 0.0
        zero_crossing_rate = 0.0

    features = WalkieFeatures(
        signal_dbfs=round(dbfs_rms(iq), 3),
        envelope_cv=round(envelope_cv, 4),
        occupied_ratio=round(occupied_ratio, 4),
        saturation_ratio=round(saturation_ratio, 4),
        audio_rms_dbfs=round(audio_rms_dbfs, 3),
        audio_peak=round(audio_peak, 4),
        audio_bandwidth_hz=round(audio_bandwidth_hz, 1),
        voice_band_ratio=round(voice_band_ratio, 4),
        voice_activity_ratio=round(voice_activity_ratio, 4),
        freq_std_hz=round(freq_std_hz, 1),
        zero_crossing_rate=round(zero_crossing_rate, 4),
    )

    label = "unknown"
    confidence = 0.3
    modulation = "unknown"
    if features.signal_dbfs < -48.0 or features.occupied_ratio < 0.03:
        label = "no_signal"
        confidence = 0.98
    elif (
        features.audio_rms_dbfs > -34.0
        and features.voice_band_ratio >= 0.55
        and 500.0 <= features.audio_bandwidth_hz <= 5_500.0
        and 0.15 <= features.voice_activity_ratio <= 0.8
        and features.envelope_cv <= 0.45
    ):
        label = "narrowband_fm_voice"
        confidence = min(
            0.98,
            0.55
            + max(0.0, (-features.audio_rms_dbfs - 10.0) / 80.0)
            + min(0.2, features.voice_band_ratio * 0.2)
            + min(0.15, features.occupied_ratio * 0.15),
        )
        modulation = "NBFM"
    elif (
        features.signal_dbfs > -40.0
        and features.audio_rms_dbfs < -36.0
        and features.envelope_cv <= 0.35
        and features.occupied_ratio >= 0.25
    ):
        label = "narrowband_fm_carrier"
        confidence = 0.84
        modulation = "NBFM"
    elif (
        features.signal_dbfs > -10.0
        and features.saturation_ratio >= 0.03
        and features.occupied_ratio >= 0.20
        and features.freq_std_hz <= 30_000.0
        and features.voice_band_ratio >= 0.45
    ):
        label = "narrowband_fm_audio"
        confidence = 0.78
        modulation = "NBFM"
    elif features.audio_rms_dbfs > -36.0 and features.voice_band_ratio >= 0.4 and features.envelope_cv <= 0.55:
        label = "narrowband_fm_audio"
        confidence = 0.72
        modulation = "NBFM"
    elif features.envelope_cv > 0.5 and features.occupied_ratio < 0.6:
        label = "bursty_unknown"
        confidence = 0.6

    return WalkieClassification(label, round(confidence, 4), modulation, features), audio


def load_capture(iq_path: Path, metadata_path: Path | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    metadata_file = metadata_path
    if metadata_file is None:
        sibling = iq_path.with_suffix(".json")
        if sibling.exists():
            metadata_file = sibling
    metadata: dict[str, Any] = {}
    if metadata_file is not None and metadata_file.exists():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            metadata = {}
    iq = iq_i8_to_complex(iq_path.read_bytes())
    return iq, metadata


def save_capture(iq: np.ndarray, metadata: CaptureMetadata, output_dir: Path, stem: str) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    iq_path = output_dir / f"{stem}.iq"
    meta_path = output_dir / f"{stem}.json"
    iq_path.write_bytes(complex_to_i8_bytes(iq))
    payload = asdict(metadata)
    payload["iq_path"] = iq_path.name
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return iq_path, meta_path


def save_audio_wav(audio: np.ndarray, output_path: Path, sample_rate_hz: int = 16_000) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio.astype(np.float32, copy=False), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate_hz))
        handle.writeframes(pcm.tobytes())
    return output_path
