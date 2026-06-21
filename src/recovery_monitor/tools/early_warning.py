"""Deterministic simplified early-warning score.

IMPORTANT — this is a *simplified* score inspired by NEWS2, NOT clinical NEWS2.
Real NEWS2 needs respiratory rate, SpO2, temperature, blood pressure and level of
consciousness — none of which this home sensor kit (heart-rate, tilt/mobility,
ambient-light, touch check-in) captures. This score is decision-support input
only; it is one signal the Reconciler weighs, never a diagnosis.

Each component contributes 0–3 points (NEWS2-style). The components sum to a
total that maps to a low / medium / high band. This module is fully implemented
(no stubs): it is pure, deterministic, and unit-tested in
``tests/test_early_warning.py``.
"""

from __future__ import annotations

from recovery_monitor.schemas import EarlyWarningScore, SensorReading


# --------------------------------------------------------------------------- #
# Per-component sub-scores (each 0–3)
# --------------------------------------------------------------------------- #
def _heart_rate_score(hr: float) -> int:
    """Resting heart rate. Both tachy- and brady-cardia raise the score."""
    if hr <= 50:
        return 2
    if hr <= 90:
        return 0
    if hr <= 100:
        return 1
    if hr <= 110:
        return 2
    return 3


def _mobility_score(mobility_index: float) -> int:
    """Mobility (0–1, higher = more movement). LOW mobility is the worry.

    Low post-op mobility is a validated predictor of readmission in colorectal
    recovery, so it is weighted as heavily as heart rate here.
    """
    if mobility_index >= 0.60:
        return 0
    if mobility_index >= 0.40:
        return 1
    if mobility_index >= 0.20:
        return 2
    return 3


def _sleep_disruption_score(ambient_light: float) -> int:
    """Ambient light used as a night-time sleep-disruption proxy.

    Higher sustained ambient light (lights left on overnight, restless nights) is
    associated with fragmented sleep, which tracks with recovery setbacks. The
    thresholds are a placeholder calibration.

    TODO(sensing-owner): replace the raw ambient-light proxy with a proper
    night-time light-on *frequency* derived from the rolling window once the
    sensor firmware timestamps day/night.
    """
    if ambient_light < 50:
        return 0
    if ambient_light < 150:
        return 1
    if ambient_light < 300:
        return 2
    return 3


def _missed_checkin_score(missed: int) -> int:
    """Missed touch check-ins (engagement / orientation proxy)."""
    if missed <= 0:
        return 0
    if missed == 1:
        return 1
    if missed == 2:
        return 2
    return 3


def _count_missed_checkins(reading: SensorReading, window: list[SensorReading] | None) -> int:
    """Count missed touch check-ins across the window + the current reading."""
    readings = list(window or [])
    if reading not in readings:
        readings.append(reading)
    return sum(1 for r in readings if not r.touch_checkin)


def _band_for(score: int) -> str:
    """Map a total score to a low / medium / high band."""
    if score <= 2:
        return "low"
    if score <= 4:
        return "medium"
    return "high"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def compute_early_warning(
    reading: SensorReading,
    window: list[SensorReading] | None = None,
) -> EarlyWarningScore:
    """Compute the simplified early-warning score for one reading.

    Args:
        reading: the latest sensor reading.
        window: recent readings, used only to count missed touch check-ins. Pass
            the patient's rolling window; ``None`` scores the single reading.

    Returns:
        An :class:`~recovery_monitor.schemas.EarlyWarningScore` with the total,
        the per-component breakdown, and the band.
    """
    missed = _count_missed_checkins(reading, window)
    components = {
        "heart_rate": _heart_rate_score(reading.heart_rate),
        "mobility": _mobility_score(reading.mobility_index),
        "sleep_disruption": _sleep_disruption_score(reading.ambient_light),
        "missed_checkins": _missed_checkin_score(missed),
    }
    total = sum(components.values())
    return EarlyWarningScore(score=total, components=components, band=_band_for(total))
