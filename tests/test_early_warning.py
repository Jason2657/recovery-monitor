"""Unit tests for the deterministic early-warning score.

This tool is fully implemented (not stubbed), so it gets real assertions on
behaviour and boundaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from recovery_monitor.schemas import SensorReading
from recovery_monitor.tools.early_warning import compute_early_warning

T0 = datetime(2026, 1, 6, 12, 0, 0)


def _reading(**overrides) -> SensorReading:
    base = dict(
        timestamp=T0,
        heart_rate=70.0,
        mobility_index=0.7,
        ambient_light=10.0,
        touch_checkin=True,
    )
    base.update(overrides)
    return SensorReading(**base)


def test_healthy_reading_is_low_band():
    ews = compute_early_warning(_reading())
    assert ews.band == "low"
    assert ews.score == 0
    assert set(ews.components) == {"heart_rate", "mobility", "sleep_disruption", "missed_checkins"}


def test_deteriorated_reading_is_high_band():
    ews = compute_early_warning(
        _reading(heart_rate=112.0, mobility_index=0.1, ambient_light=350.0, touch_checkin=False)
    )
    assert ews.band == "high"
    assert ews.score >= 5


def test_low_mobility_raises_score():
    healthy = compute_early_warning(_reading(mobility_index=0.8))
    immobile = compute_early_warning(_reading(mobility_index=0.1))
    assert immobile.components["mobility"] > healthy.components["mobility"]


def test_missed_checkins_accumulate_over_window():
    window = [
        _reading(timestamp=T0 - timedelta(days=3), touch_checkin=False),
        _reading(timestamp=T0 - timedelta(days=2), touch_checkin=False),
        _reading(timestamp=T0 - timedelta(days=1), touch_checkin=False),
    ]
    latest = _reading(touch_checkin=False)
    ews = compute_early_warning(latest, window + [latest])
    assert ews.components["missed_checkins"] == 3  # capped at 3


def test_score_is_sum_of_components():
    ews = compute_early_warning(_reading(heart_rate=105.0, mobility_index=0.3))
    assert ews.score == sum(ews.components.values())


def test_bradycardia_flagged():
    ews = compute_early_warning(_reading(heart_rate=45.0))
    assert ews.components["heart_rate"] >= 2
