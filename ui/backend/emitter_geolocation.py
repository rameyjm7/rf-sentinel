#!/usr/bin/env python3
"""Weighted Centroid Localization (WCL) for a stationary RF emitter,
estimated from repeated (lat, lon, rssi_dbfs) sightings gathered while
the operator moves around with this station.

This is the same technique and the same formula as rm520n-survey's
cellular_survey.py estimate_cell_location() (validated there against
synthetic ground truth across several iterations of the uncertainty
model) - ported here rather than reinvented, since RF-Sentinel and
rm520n-survey are separate repos with no shared package. Domain is
different (any RSSI-keyed entity RF-Sentinel already tracks - BLE, BT
Classic, WiFi, Zigbee, TPMS, walkie, FM, cellular_signal - not cell
towers), but the math and its caveats are identical: this estimates
where a STATIONARY emitter physically is, not a moving one, and it's
only as good as the geometry of the sightings gathered (see
uncertainty_radius_m's own docstring below).
"""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6371000.0
MIN_OBSERVATIONS = 3
MIN_SPATIAL_SPREAD_M = 15.0
SECTOR_COUNT = 8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _bearing_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.atan2(x, y)


def _geographic_spread_m(positions: Sequence[Tuple[float, float]]) -> Optional[float]:
    if not positions:
        return None
    center_lat = sum(lat for lat, _ in positions) / len(positions)
    center_lon = sum(lon for _, lon in positions) / len(positions)
    return max(haversine_m(center_lat, center_lon, lat, lon) for lat, lon in positions)


def estimate_emitter_location(
    observations: Sequence[Tuple[float, float, Optional[float]]],
    *,
    min_observations: int = MIN_OBSERVATIONS,
    min_spread_m: float = MIN_SPATIAL_SPREAD_M,
) -> Optional[dict[str, Any]]:
    """observations is a list of (lat, lon, rssi_dbfs) - the operator's
    own GPS position and the RSSI of that sighting, one per re-detection
    of a specific tracked entity. Returns None below min_observations
    usable points or if they're all clustered within min_spread_m (the
    "operator stood still" case - the estimate would just reproduce the
    operator's own position, not the emitter's).

    uncertainty_radius_m is a heuristic, not a statistical confidence
    interval: weighted_rms_m (signal-weighted average distance from the
    estimate) scaled up by how many of 8 bearing sectors around the
    estimate actually have a sighting in them. An operator who only ever
    approached from one direction gets a correspondingly large penalty -
    more sightings from that same one-sided approach don't shrink it,
    since the resulting bias is systematic, not averaged-out noise."""
    usable = [(lat, lon, rssi) for lat, lon, rssi in observations if rssi is not None]
    if len(usable) < min_observations:
        return None

    raw_spread_m = _geographic_spread_m([(lat, lon) for lat, lon, _ in usable])
    if raw_spread_m is None or raw_spread_m < min_spread_m:
        return None

    weights = [10 ** (rssi / 10.0) for _, _, rssi in usable]
    total_weight = sum(weights)
    if total_weight <= 0:
        return None

    est_lat = sum(w * lat for (lat, _, _), w in zip(usable, weights)) / total_weight
    est_lon = sum(w * lon for (_, lon, _), w in zip(usable, weights)) / total_weight

    sq_dist_sum = 0.0
    sector_hits = [False] * SECTOR_COUNT
    for (lat, lon, _), w in zip(usable, weights):
        dist = haversine_m(est_lat, est_lon, lat, lon)
        sq_dist_sum += w * dist * dist
        bearing_deg = math.degrees(_bearing_rad(est_lat, est_lon, lat, lon)) % 360.0
        sector_hits[int(bearing_deg // (360.0 / SECTOR_COUNT))] = True
    weighted_rms_m = math.sqrt(sq_dist_sum / total_weight)
    sector_coverage = sum(sector_hits) / SECTOR_COUNT
    # Normalized against how many sectors THIS MANY sightings could
    # possibly have hit, not a flat /SECTOR_COUNT - fewer than
    # SECTOR_COUNT sightings can never fill every sector even with ideal
    # geometry (3 sightings can light up at most 3 of 8), so a flat
    # penalty would over-penalize a well-arranged handful of sightings
    # just as much as a badly-arranged one. sector_coverage itself
    # (returned below) stays the raw, unnormalized fraction.
    achievable_sector_ceiling = min(1.0, len(usable) / SECTOR_COUNT)
    diversity_penalty = 1.0 / max(0.15, sector_coverage / achievable_sector_ceiling)
    uncertainty_radius_m = weighted_rms_m * diversity_penalty

    return {
        "latitude": est_lat,
        "longitude": est_lon,
        "uncertainty_radius_m": uncertainty_radius_m,
        "weighted_rms_m": weighted_rms_m,
        "sector_coverage": sector_coverage,
        "raw_spread_m": raw_spread_m,
        "observation_count": len(usable),
        "strongest_rssi_dbfs": max(rssi for _, _, rssi in usable),
    }
