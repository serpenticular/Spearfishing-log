#!/usr/bin/env python3
"""
Turns the raw logged CSVs (data/readings_actual.csv, data/forecast_latest.csv,
locations.csv) into a single compact JSON blob for the mobile app/map
artifact to render. This is NOT part of the GitHub Actions logging pipeline -
it's a separate, on-demand build step (run by Claude when refreshing the
published dashboard), since a published Artifact can't fetch the GitHub data
live itself.

What it computes, per location:
  - "current": nearest actual reading to now (current weather + score)
  - "days": for today + next 4 forecast days, a morning-window (06:00-12:00
    local) average of score/wind/swell - a "expected conditions" summary per
    day, since that's the usual dive window
  - "wind_timeline": hourly wind speed/dir/onshore-offshore for the last 3
    days through the 5-day forecast (for a Windy-style arrow strip)
  - "swell_timeline": same window, swell height/period/direction
  - "history_14d": whatever actual daily-average score history has
    accumulated so far (will be sparse/short until ~14 days of logging have
    run - this script does not fabricate missing days)
  - "rain_badges": mild-rain-in-last-3-days / significant-rain-in-last-7-days
  - "recovery": onshore/offshore wind-streak based visibility recovery
    estimate (see RECOVERY METHODOLOGY below - this is Claude's own
    heuristic built from qualitative research, not a scientific formula)
  - "moon_phase": today's moon phase name + illumination fraction

ONSHORE/OFFSHORE METHODOLOGY:
  Each location in locations.csv has a `facing_bearing_deg` - the compass
  bearing from the location out to open water, assigned by Claude from
  general knowledge of the Victorian/SA/NSW coastline (not surveyed per
  site). Wind is "onshore" when it blows FROM within ~67.5 degrees of that
  bearing (i.e. from the sea toward land), "offshore" when from within
  ~67.5 degrees of the opposite bearing, and "cross-shore" otherwise.
  Corrections welcome - see the decisions log in the Claude project doc.

RECOVERY METHODOLOGY (Claude's heuristic, not a published formula):
  Research across diving/spearfishing forums and guides (DeeperBlue,
  ScubaDoctor, noobspearo, spearfishing.world, a1spearfishing - see the
  project doc for links) turned up consistent qualitative agreement that
  onshore wind/swell stirs up turbidity and offshore wind lets it settle,
  but NO authoritative numeric formula for how many days that takes.
  Reported anecdotes ranged ~2-4 days ("it took 3-4 days for the water to
  clear", "a few days to settle down after large swell"). Given that, the
  heuristic here is: count consecutive recent days with predominantly
  onshore wind >15km/h ("onshore_streak"); the recovery time needed is
  ceil(onshore_streak / 1.5) days, capped at 4. Compare that to the current
  consecutive-days of predominantly offshore/calm wind ("clearing_streak")
  to say Recovered / Recovering (+ how many more days) / insufficient data.
  This is a rough guide, not a guarantee - treat it as one input alongside
  the visibility_score, not a replacement for it.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_CSV = REPO_ROOT / "locations.csv"
ACTUAL_CSV = REPO_ROOT / "data" / "readings_actual.csv"
FORECAST_CSV = REPO_ROOT / "data" / "forecast_latest.csv"
OUT_JSON = REPO_ROOT / "data" / "dashboard.json"

ONSHORE_HALF_ARC = 67.5   # degrees either side of facing_bearing counted as onshore
ONSHORE_WIND_THRESHOLD_KMH = 15  # below this, a day isn't counted as a "blow" either way
MILD_RAIN_MAX_MM = 5.0
SIGNIFICANT_RAIN_MM = 30.0
RECENT_DAYS_FOR_RAIN_MILD = 3
RECENT_DAYS_FOR_RAIN_SIGNIFICANT = 7
WIND_TIMELINE_PAST_DAYS = 3
WIND_TIMELINE_FUTURE_DAYS = 5


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# CSV rows come back from csv.DictReader as all-string dicts. These are the
# columns that are actually numbers - anything logged straight into the
# "current" block for the dashboard JSON needs these cast, or every consumer
# (the app's JS included) gets strings where it expects numbers.
NUMERIC_ROW_FIELDS = {
    "visibility_score", "lead_time_hours", "wind_speed_kmh", "wind_dir_deg",
    "wind_gust_kmh", "rainfall_mm", "swell_height_m", "swell_period_s",
    "swell_dir_deg", "wave_height_m", "wind_wave_height_m",
    "current_velocity_kmh", "current_dir_deg", "sea_level_height_m",
    "sea_surface_temp_c", "chlorophyll_mg_m3",
}


def numeric_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in NUMERIC_ROW_FIELDS:
        if k in out:
            out[k] = to_float(out[k])
    return out


def parse_utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def angular_diff(a: float, b: float) -> float:
    """Smallest difference between two compass bearings, 0-180."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def onshore_offshore(wind_dir_deg: float | None, facing_bearing_deg: float) -> str:
    if wind_dir_deg is None:
        return "unknown"
    onshore_diff = angular_diff(wind_dir_deg, facing_bearing_deg)
    if onshore_diff <= ONSHORE_HALF_ARC:
        return "onshore"
    offshore_diff = angular_diff(wind_dir_deg, (facing_bearing_deg + 180) % 360)
    if offshore_diff <= ONSHORE_HALF_ARC:
        return "offshore"
    return "cross-shore"


def moon_phase(date: dt.date) -> dict:
    """Simple synodic-month approximation - no API needed. Reference new
    moon: 2000-01-06 18:14 UTC (a well-known epoch for this calculation)."""
    synodic = 29.53058867
    ref = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)
    now = dt.datetime(date.year, date.month, date.day, 12, tzinfo=dt.timezone.utc)
    days = (now - ref).total_seconds() / 86400.0
    phase = (days % synodic) / synodic  # 0=new, 0.5=full
    illumination = round((1 - math.cos(2 * math.pi * phase)) / 2, 2)
    if phase < 0.03 or phase > 0.97:
        name = "New Moon"
    elif phase < 0.22:
        name = "Waxing Crescent"
    elif phase < 0.28:
        name = "First Quarter"
    elif phase < 0.47:
        name = "Waxing Gibbous"
    elif phase < 0.53:
        name = "Full Moon"
    elif phase < 0.72:
        name = "Waning Gibbous"
    elif phase < 0.78:
        name = "Last Quarter"
    else:
        name = "Waning Crescent"
    return {"name": name, "illumination": illumination, "is_spring_tide": phase < 0.06 or 0.44 < phase < 0.56}


def build():
    locations = read_csv(LOCATIONS_CSV)
    actual = read_csv(ACTUAL_CSV)
    forecast = read_csv(FORECAST_CSV)
    now_utc = dt.datetime.now(dt.timezone.utc)

    by_loc_actual = defaultdict(list)
    for r in actual:
        by_loc_actual[r["location"]].append(r)
    by_loc_forecast = defaultdict(list)
    for r in forecast:
        by_loc_forecast[r["location"]].append(r)

    out_locations = []
    for loc in locations:
        if loc.get("active", "true").strip().lower() == "false":
            continue
        name = loc["name"]
        facing = to_float(loc.get("facing_bearing_deg"))
        a_rows = sorted(by_loc_actual.get(name, []), key=lambda r: r["valid_time_utc"])
        f_rows = sorted(by_loc_forecast.get(name, []), key=lambda r: r["valid_time_utc"])
        all_rows = a_rows + f_rows  # actual (past/now) + forecast (future), already time-ordered within each

        # ---- current conditions: latest actual row (or earliest forecast if none) ----
        current = numeric_row(a_rows[-1] if a_rows else (f_rows[0] if f_rows else None))

        # ---- per-day (today..+4) morning-window (06:00-12:00 local) summary ----
        days = []
        by_date = defaultdict(list)
        for r in all_rows:
            date_str = r["valid_time_local"][:10]
            hour = int(r["valid_time_local"][11:13])
            by_date[date_str].append((hour, r))
        sorted_dates = sorted(by_date.keys())
        # pick today (from now_utc) + next 4 calendar days present in the data
        today_str = now_utc.astimezone(dt.timezone(dt.timedelta(hours=10))).strftime("%Y-%m-%d")
        candidate_dates = [d for d in sorted_dates if d >= today_str][:5]
        for date_str in candidate_dates:
            morning_rows = [r for h, r in by_date[date_str] if 6 <= h <= 12]
            use_rows = morning_rows if morning_rows else [r for _, r in by_date[date_str]]
            scores = [to_float(r.get("visibility_score")) for r in use_rows]
            scores = [s for s in scores if s is not None]
            winds = [to_float(r.get("wind_speed_kmh")) for r in use_rows]
            winds = [w for w in winds if w is not None]
            wind_dirs = [to_float(r.get("wind_dir_deg")) for r in use_rows if to_float(r.get("wind_dir_deg")) is not None]
            swells = [to_float(r.get("swell_height_m")) for r in use_rows]
            swells = [s for s in swells if s is not None]
            avg_wind_dir = None
            if wind_dirs:
                # circular mean
                sin_sum = sum(math.sin(math.radians(d)) for d in wind_dirs)
                cos_sum = sum(math.cos(math.radians(d)) for d in wind_dirs)
                avg_wind_dir = round(math.degrees(math.atan2(sin_sum, cos_sum)) % 360, 0)
            days.append({
                "date": date_str,
                "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
                "label": use_rows[0].get("visibility_label") if use_rows else None,
                "avg_wind_kmh": round(sum(winds) / len(winds), 1) if winds else None,
                "avg_wind_dir_deg": avg_wind_dir,
                "onshore_offshore": onshore_offshore(avg_wind_dir, facing) if facing is not None else "unknown",
                "avg_swell_m": round(sum(swells) / len(swells), 2) if swells else None,
                "swell_band": use_rows[0].get("swell_band") if use_rows else None,
            })

        # ---- wind/swell timeline: past N days -> future M days, hourly ----
        cutoff_past = now_utc - dt.timedelta(days=WIND_TIMELINE_PAST_DAYS)
        cutoff_future = now_utc + dt.timedelta(days=WIND_TIMELINE_FUTURE_DAYS)
        timeline = []
        for r in all_rows:
            t = parse_utc(r["valid_time_utc"])
            if cutoff_past <= t <= cutoff_future:
                wd = to_float(r.get("wind_dir_deg"))
                timeline.append({
                    "t": r["valid_time_utc"],
                    "wind_kmh": to_float(r.get("wind_speed_kmh")),
                    "wind_dir_deg": wd,
                    "onshore_offshore": onshore_offshore(wd, facing) if facing is not None else "unknown",
                    "swell_m": to_float(r.get("swell_height_m")),
                    "swell_period_s": to_float(r.get("swell_period_s")),
                    "swell_dir_deg": to_float(r.get("swell_dir_deg")),
                    "is_forecast": r in f_rows,
                })

        # ---- 14-day history (as accumulated) - daily average score from actuals ----
        hist_by_date = defaultdict(list)
        for r in a_rows:
            s = to_float(r.get("visibility_score"))
            if s is not None:
                hist_by_date[r["valid_time_local"][:10]].append(s)
        history_14d = [
            {"date": d, "avg_score": round(sum(v) / len(v), 1)}
            for d, v in sorted(hist_by_date.items())
        ][-14:]

        # ---- rainfall badges ----
        rain_by_date = defaultdict(float)
        for r in a_rows:
            rf = to_float(r.get("rainfall_mm"))
            if rf is not None:
                rain_by_date[r["valid_time_local"][:10]] += rf
        recent_dates_sorted = sorted(rain_by_date.keys())
        last3 = recent_dates_sorted[-RECENT_DAYS_FOR_RAIN_MILD:]
        last7 = recent_dates_sorted[-RECENT_DAYS_FOR_RAIN_SIGNIFICANT:]
        mild_rain_recent = any(0 < rain_by_date[d] <= MILD_RAIN_MAX_MM for d in last3)
        significant_rain_week = sum(rain_by_date[d] for d in last7) > SIGNIFICANT_RAIN_MM
        enough_days_for_rain = len(recent_dates_sorted) >= RECENT_DAYS_FOR_RAIN_SIGNIFICANT

        # ---- recovery estimate from onshore/offshore day streaks ----
        wind_by_date = defaultdict(list)
        for r in a_rows:
            wd = to_float(r.get("wind_dir_deg"))
            ws = to_float(r.get("wind_speed_kmh"))
            if wd is not None and ws is not None:
                wind_by_date[r["valid_time_local"][:10]].append((wd, ws))
        day_classification = {}
        for d, vals in wind_by_date.items():
            onshore_strong_hours = sum(
                1 for wd, ws in vals
                if ws >= ONSHORE_WIND_THRESHOLD_KMH and onshore_offshore(wd, facing) == "onshore"
            )
            offshore_or_calm_hours = sum(
                1 for wd, ws in vals
                if ws < ONSHORE_WIND_THRESHOLD_KMH or onshore_offshore(wd, facing) == "offshore"
            )
            if onshore_strong_hours > len(vals) / 2:
                day_classification[d] = "onshore_blow"
            elif offshore_or_calm_hours > len(vals) / 2:
                day_classification[d] = "offshore_or_calm"
            else:
                day_classification[d] = "mixed"
        ordered_days = sorted(day_classification.keys())
        onshore_streak = 0
        for d in reversed(ordered_days):
            cls = day_classification[d]
            if cls == "offshore_or_calm":
                break
            if cls == "onshore_blow":
                onshore_streak += 1
            else:
                break
        clearing_streak = 0
        for d in reversed(ordered_days):
            if day_classification[d] == "offshore_or_calm":
                clearing_streak += 1
            else:
                break
        if len(ordered_days) < 3:
            recovery = {"status": "insufficient_history", "detail": "Fewer than 3 days of wind history logged yet."}
        elif onshore_streak == 0:
            recovery = {"status": "clear", "detail": "No recent onshore blow detected."}
        else:
            needed = min(4, math.ceil(onshore_streak / 1.5))
            if clearing_streak >= needed:
                recovery = {"status": "recovered", "onshore_streak_days": onshore_streak,
                            "offshore_days_needed": needed, "offshore_days_so_far": clearing_streak}
            else:
                recovery = {"status": "recovering", "onshore_streak_days": onshore_streak,
                            "offshore_days_needed": needed, "offshore_days_so_far": clearing_streak,
                            "more_days_needed": needed - clearing_streak}

        out_locations.append({
            "name": name,
            "lat": to_float(loc["lat"]),
            "lon": to_float(loc["lon"]),
            "facing_bearing_deg": facing,
            "notes": loc.get("notes", ""),
            "current": current,
            "days": days,
            "timeline": timeline,
            "history_14d": history_14d,
            "rain_badges": {
                "mild_rain_last_3_days": mild_rain_recent,
                "significant_rain_last_7_days": significant_rain_week,
                "enough_data": enough_days_for_rain,
            },
            "recovery": recovery,
        })

    out = {
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "moon_phase": moon_phase(now_utc.date()),
        "locations": out_locations,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {OUT_JSON} ({len(out_locations)} locations)", file=sys.stderr)


if __name__ == "__main__":
    build()
