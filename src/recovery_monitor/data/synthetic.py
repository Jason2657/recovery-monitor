"""Synthetic recovery timelines — two labelled 5–7 day profiles.

Owner: Person A (Drift & data) — ships with the Trend agent; the agent is only as
good as the timelines it is tested against.

Profiles
--------
``deterioration``  Slow, real decline. Resting HR creeps up (~+2 bpm/day), sleep
    fragments (rising night-time light-on), mobility trends down, and self-reports
    drift "fine" -> "more tired" -> "short of breath". GROUND TRUTH: should escalate.

``benign_decoy``   Looks alarming around days 2–3 (an HR bump + bad sleep) but has
    a benign explanation the Skeptic should catch — the self-report mentions
    visitors / travel, and it resolves on its own. GROUND TRUTH: should NOT escalate.

The generator is deterministic (fixed RNG seed) so the demo and tests are stable.
Hourly readings are written to ``data/profiles/`` (gitignored); the demo collapses
them to one decision point per day via :func:`daily_summaries`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

Profile = Literal["deterioration", "benign_decoy"]

#: Ground-truth outcome label per profile (did the patient warrant escalation?),
#: consumed by the self-correction loop and the smoke test.
PROFILE_LABELS: dict[str, bool] = {"deterioration": True, "benign_decoy": False}

#: Deterministic clock so generation is reproducible (no wall-clock dependency).
DEFAULT_START = datetime(2026, 1, 6, 0, 0, 0)

_SELF_REPORTS: dict[str, list[str]] = {
    "deterioration": [
        "Feeling good today, resting comfortably and walked a few laps.",
        "Pretty good. Slept okay.",
        "A little more tired than yesterday.",
        "Tired today. Didn't sleep great, kept waking up.",
        "Quite tired. Not walking as much, it feels like real effort.",
        "Worn out. Slept badly again and stayed in bed most of the day.",
        "Really tired and a bit short of breath walking to the kitchen.",
    ],
    "benign_decoy": [
        "Feeling fine, walking around the house.",
        "Good day, nothing to report.",
        "Didn't sleep well — had visitors over and we stayed up late.",
        "Tired today, we traveled back from my daughter's, a long day.",
        "Much better, back in my own bed and slept well.",
        "Good, pretty much back to normal.",
        "Fine, walked my usual laps.",
    ],
}


def profiles_dir() -> Path:
    """Directory where generated profiles land (created if missing)."""
    d = Path(__file__).resolve().parent / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_day(hour: int) -> bool:
    return 8 <= hour < 22


def _hourly_row(profile: Profile, day: int, hour: int, rng: np.random.Generator) -> dict:
    """Generate one hourly sensor row for a profile/day/hour."""
    is_day = _is_day(hour)

    if profile == "deterioration":
        # Resting HR creeps up ~+2 bpm/day (the Trend agent's target signal).
        hr = 68 + 2.0 * day + (3 if is_day else -2) + rng.normal(0, 1.5)
        # Mobility trends down day over day (validated readmission predictor).
        day_mobility = float(np.clip(0.72 - 0.09 * day + rng.normal(0, 0.03), 0.05, 1.0))
        mobility = day_mobility if is_day else float(np.clip(rng.normal(0.03, 0.02), 0.0, 0.1))
        # Sleep fragments: night-time light-on grows more frequent/brighter.
        if is_day:
            light = float(max(0.0, 250 + rng.normal(0, 30)))
        else:
            frag_prob = min(0.15 + 0.14 * day, 0.95)
            on = rng.random() < frag_prob
            light = float(max(0.0, 5 + rng.normal(0, 2) + (150 + 30 * day if on else 0)))
        # Missed touch check-ins become more common as the patient declines.
        miss_prob = min(0.02 + 0.07 * day, 0.55)
        checkin = bool(rng.random() > miss_prob)

    elif profile == "benign_decoy":
        # Looks alarming on days 2–3 (HR bump + bad sleep) then self-resolves.
        bump = day in (2, 3)
        hr = 70 + (36 if bump else 0) + (3 if is_day else -2) + rng.normal(0, 1.5)
        day_mobility = float(np.clip((0.50 if bump else 0.62) + rng.normal(0, 0.03), 0.05, 1.0))
        mobility = day_mobility if is_day else float(np.clip(rng.normal(0.03, 0.02), 0.0, 0.1))
        if is_day:
            light = float(max(0.0, 250 + rng.normal(0, 30)))
        else:
            on = bump and rng.random() < 0.9  # bad sleep only on the visitor/travel nights
            light = float(max(0.0, 8 + rng.normal(0, 2) + (320 if on else 0)))
        checkin = True  # engaged throughout — another benign tell

    else:  # pragma: no cover - guarded by the Literal type
        raise ValueError(f"unknown profile: {profile!r}")

    return {
        "timestamp": DEFAULT_START + timedelta(days=day, hours=hour),
        "heart_rate": round(hr, 1),
        "mobility_index": round(mobility, 3),
        "ambient_light": round(light, 1),
        "touch_checkin": checkin,
        "self_report": _SELF_REPORTS[profile][day] if hour == 9 else "",
    }


def generate(
    profile: Profile,
    days: int = 7,
    *,
    seed: int = 7,
    start: datetime | None = None,
    write: bool = True,
) -> pd.DataFrame:
    """Generate an hourly recovery timeline for ``profile``.

    Args:
        profile: ``"deterioration"`` or ``"benign_decoy"``.
        days: number of days to generate (5–7 is the intended range).
        seed: RNG seed for reproducibility.
        start: base timestamp (defaults to the deterministic clock).
        write: also persist CSV + self-report JSON under ``data/profiles/``.

    Returns:
        A DataFrame of hourly readings with columns:
        ``timestamp, heart_rate, mobility_index, ambient_light, touch_checkin,
        self_report``.
    """
    if profile not in _SELF_REPORTS:
        raise ValueError(f"unknown profile: {profile!r}")
    if days > len(_SELF_REPORTS[profile]):
        raise ValueError(f"{profile} has scripted self-reports for {len(_SELF_REPORTS[profile])} days max")

    rng = np.random.default_rng(seed)
    base = start or DEFAULT_START
    rows = [
        _hourly_row(profile, day, hour, rng) | {"timestamp": base + timedelta(days=day, hours=hour)}
        for day in range(days)
        for hour in range(24)
    ]
    df = pd.DataFrame(rows)

    if write:
        out = profiles_dir()
        df.to_csv(out / f"{profile}.csv", index=False)
        reports = [
            {"timestamp": r["timestamp"].isoformat(), "raw_text": r["self_report"]}
            for r in rows
            if r["self_report"]
        ]
        (out / f"{profile}_self_reports.json").write_text(json.dumps(reports, indent=2))

    return df


def daily_summaries(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse an hourly timeline into one decision point per day.

    Aggregation reflects the clinical meaning of each signal:
      * ``heart_rate``    — mean of resting (early-morning) hours,
      * ``mobility_index``— mean of daytime hours,
      * ``ambient_light`` — mean of night-time hours (the sleep-disruption proxy),
      * ``touch_checkin`` — True if the patient checked in for the majority of the day,
      * ``self_report``   — that day's scripted note.

    Returns one row per day, sorted by timestamp — exactly the 5–7 day rolling
    window the Trend agent reasons over.
    """
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["day"] = df["timestamp"].dt.date
    df["hour"] = df["timestamp"].dt.hour

    out_rows = []
    for day, g in df.groupby("day", sort=True):
        resting = g[g["hour"].between(4, 7)]["heart_rate"]
        daytime_mob = g[g["hour"].between(8, 20)]["mobility_index"]
        night = g[(g["hour"] <= 6) | (g["hour"] >= 22)]["ambient_light"]
        report = next((s for s in g["self_report"] if s), "")
        out_rows.append(
            {
                "timestamp": datetime.combine(day, datetime.min.time()) + timedelta(hours=12),
                "heart_rate": round(float(resting.mean()), 1),
                "mobility_index": round(float(daytime_mob.mean()), 3),
                "ambient_light": round(float(night.mean()), 1),
                "touch_checkin": bool(g["touch_checkin"].mean() >= 0.5),
                "self_report": report,
            }
        )
    return pd.DataFrame(out_rows)


def load_profile(path: str | Path) -> pd.DataFrame:
    """Load a previously generated CSV, parsing the boolean column correctly."""
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["touch_checkin"] = df["touch_checkin"].astype(str).str.lower().map({"true": True, "false": False})
    df["self_report"] = df["self_report"].fillna("")
    return df


if __name__ == "__main__":  # pragma: no cover - convenience entrypoint
    for name in PROFILE_LABELS:
        frame = generate(name)
        print(f"generated {name}: {len(frame)} hourly readings -> {profiles_dir() / (name + '.csv')}")
