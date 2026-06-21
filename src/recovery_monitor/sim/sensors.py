"""Sensor stream replay — turn a generated profile into a 'live' reading stream.

Owner: Person C (Sensing & scoring).

In production these readings arrive from the hardware kit over BLE/serial. For
the demo we replay a generated timeline. ``stream`` yields one
:class:`~recovery_monitor.schemas.SensorReading` at a time with an optional speed
multiplier so a 7-day timeline can play back in seconds.

TODO(sensing-owner): add a real ``stream_from_device(port)`` that yields the same
SensorReading type off the hardware, so the rest of the pipeline is unchanged.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pandas as pd

from recovery_monitor.schemas import SelfReport, SensorReading


def reading_from_row(row: pd.Series) -> SensorReading:
    """Build a SensorReading from a (daily-summary or hourly) DataFrame row."""
    return SensorReading(
        timestamp=pd.to_datetime(row["timestamp"]).to_pydatetime(),
        heart_rate=float(row["heart_rate"]),
        mobility_index=float(row["mobility_index"]),
        ambient_light=float(row["ambient_light"]),
        touch_checkin=bool(row["touch_checkin"]),
    )


def to_readings(df: pd.DataFrame) -> list[SensorReading]:
    """Eagerly convert a DataFrame into a list of SensorReadings."""
    return [reading_from_row(row) for _, row in df.iterrows()]


def self_report_from_row(row: pd.Series) -> SelfReport | None:
    """Extract a SelfReport from a row, or None if the row has no report text."""
    text = str(row.get("self_report", "") or "").strip()
    if not text:
        return None
    return SelfReport(
        timestamp=pd.to_datetime(row["timestamp"]).to_pydatetime(),
        raw_text=text,
    )


async def stream(df: pd.DataFrame, speed: float = 0.0) -> AsyncIterator[SensorReading]:
    """Async-replay a profile as a live SensorReading stream.

    Args:
        df: a sensor timeline (hourly or daily-summary rows).
        speed: seconds to pause between readings (0 = as fast as possible, used by
            tests; use e.g. 0.3 for a watchable demo).
    """
    for _, row in df.iterrows():
        if speed:
            await asyncio.sleep(speed)
        yield reading_from_row(row)
