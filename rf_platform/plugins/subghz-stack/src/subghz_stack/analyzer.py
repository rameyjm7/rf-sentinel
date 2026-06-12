from __future__ import annotations

from dataclasses import dataclass, field
import math
import time

import numpy as np

from .decoder import Burst


@dataclass
class BurstFamily:
    key: str
    duration_bin_ms: int
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    total_duration_ms: float = 0.0
    total_peak: float = 0.0
    total_average: float = 0.0
    interarrival_samples: list[float] = field(default_factory=list)
    modulation_counts: dict[str, int] = field(default_factory=dict)

    def observe(self, burst: Burst, hint: str) -> None:
        when = burst.ended_at
        if self.count == 0:
            self.first_seen = when
        else:
            self.interarrival_samples.append(when - self.last_seen)
            if len(self.interarrival_samples) > 20:
                self.interarrival_samples.pop(0)
        self.last_seen = when
        self.count += 1
        self.total_duration_ms += burst.duration_seconds * 1000.0
        self.total_peak += burst.peak
        self.total_average += burst.average
        self.modulation_counts[hint] = self.modulation_counts.get(hint, 0) + 1

    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / max(1, self.count)

    def avg_peak(self) -> float:
        return self.total_peak / max(1, self.count)

    def avg_average(self) -> float:
        return self.total_average / max(1, self.count)

    def dominant_hint(self) -> str:
        if not self.modulation_counts:
            return "unknown"
        return max(self.modulation_counts.items(), key=lambda item: item[1])[0]

    def median_interarrival_s(self) -> float:
        if not self.interarrival_samples:
            return 0.0
        return float(np.median(np.asarray(self.interarrival_samples, dtype=np.float32)))


class SignalAnalyzer:
    def __init__(self, family_bin_ms: float = 1.0, max_families: int = 12) -> None:
        self.family_bin_ms = max(0.25, float(family_bin_ms))
        self.max_families = max(3, int(max_families))
        self.total_bursts = 0
        self.families: dict[str, BurstFamily] = {}

    def observe(self, burst: Burst) -> tuple[BurstFamily, str]:
        hint = self.modulation_hint(burst)
        key = self.family_key(burst, hint)
        family = self.families.get(key)
        if family is None:
            duration_bin_ms = int(round((burst.duration_seconds * 1000.0) / self.family_bin_ms) * self.family_bin_ms)
            family = BurstFamily(key=key, duration_bin_ms=duration_bin_ms)
            self.families[key] = family
        family.observe(burst, hint)
        self.total_bursts += 1
        return family, hint

    def family_key(self, burst: Burst, hint: str) -> str:
        duration_ms = burst.duration_seconds * 1000.0
        duration_bin_ms = int(round(duration_ms / self.family_bin_ms) * self.family_bin_ms)
        return f"{int(burst.center_freq_hz)}|{duration_bin_ms}|{hint}"

    def modulation_hint(self, burst: Burst) -> str:
        iq = burst.iq.astype(np.complex64, copy=False)
        if iq.size < 8:
            return "unknown"

        amp = np.abs(iq).astype(np.float32, copy=False)
        amp_mean = float(np.mean(amp))
        amp_std = float(np.std(amp))
        amp_cv = amp_std / max(amp_mean, 1e-6)

        prev = iq[:-1]
        cur = iq[1:]
        phase = np.angle(cur * np.conj(prev)).astype(np.float32, copy=False)
        phase_std = float(np.std(phase))

        if amp_cv > 0.55 and phase_std < 1.0:
            return "ook-like"
        if amp_cv < 0.40 and phase_std > 0.9:
            return "fsk-like"
        if amp_cv > 0.45:
            return "am-like"
        if phase_std > 1.1:
            return "angle-like"
        return "mixed"

    def summary_lines(self) -> list[str]:
        ranked = sorted(
            self.families.values(),
            key=lambda family: (family.count, family.avg_peak()),
            reverse=True,
        )[: self.max_families]
        lines: list[str] = []
        for family in ranked:
            lines.append(
                "family "
                f"dur_ms~{family.duration_bin_ms} count={family.count} "
                f"hint={family.dominant_hint()} "
                f"avg_peak={family.avg_peak():.3f} avg_rms={family.avg_average():.3f} "
                f"median_dt_s={family.median_interarrival_s():.2f}"
            )
        return lines

    def snapshot(self) -> dict[str, object]:
        ranked = sorted(
            self.families.values(),
            key=lambda family: (family.count, family.avg_peak()),
            reverse=True,
        )[: self.max_families]
        return {
            "total_bursts": self.total_bursts,
            "families": [
                {
                    "duration_bin_ms": family.duration_bin_ms,
                    "count": family.count,
                    "hint": family.dominant_hint(),
                    "avg_peak": round(family.avg_peak(), 4),
                    "avg_rms": round(family.avg_average(), 4),
                    "median_interarrival_s": round(family.median_interarrival_s(), 3),
                }
                for family in ranked
            ],
        }
