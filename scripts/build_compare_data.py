#!/usr/bin/env python3
"""
Builds the JSON data blob for the "Conditions Compare" page - a second view
onto the same logged data as the main dashboard, but laid out so Will can
compare weather/wind/rain/tide/swell across many locations at once rather
than drilling into one location at a time.

Like build_dashboard_data.py, this is NOT part of the GitHub Actions logging
pipeline - it's a separate, on-demand build step run locally before
publishing/refreshing the Compare artifact, because a published Artifact
can't fetch the GitHub data live itself.

Requested time windows (confirmed with Will, 2026-10-14):
  - weather (temp/sky), wind, rainfall, swell: previous 4 days + the coming
    week forward.
  - tide: 2 days back, 10 days forward - though in practice the Marine API's
    own documented forecast_days cap (8, see fetch_and_log.py) means tide
    forward coverage tops out around 7-8 days, not the full 10. This is a
    known, flagged shortfall, not a bug in this script.
  - All forward coverage is limited by whatever forecast_latest.csv actually
    contains (a snapshot as of the last pipeline run), so the real number of
    forecast days shown will vary slightly run to run.

SWELL SUBSTITUTION (confirmed with Will, 2026-10-14):
  Three locations show another location's swell/wave columns (height,
  period, direction) instead of their own, because their own coordinates are
  either unusably sheltered or (for Eden specifically) a genuine data gap:
    - Eden                  <- Red Point (role=swell_reference in
                                locations.csv; not a displayed site)
    - Batemans Bay (Inside) <- Batemans Bay (Outside)
    - Jervis Bay (Inside)   <- Jervis Bay (Outside - Point Perpendicular)
  Everything else for these three (wind, rain, tide, temperature) still
  comes from the location's own coordinates - only the swell/wave columns
  are swapped. Williamstown gets no swell panel at all (sheltered, no nearby
  alternate). The remaining locations are tagged "open" (normal) or
  "caution" (partially sheltered - own numbers kept but flagged as possibly
  understating real conditions). See classification below.

Locations tagged role=swell_reference in locations.csv (currently just Red
Point) are fetched/logged like any other row but never appear in `locations`
output - they exist purely to be borrowed from by the `source` lookup.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_CSV = REPO_ROOT / "locations.csv"
ACTUAL_CSV = REPO_ROOT / "data" / "readings_actual.csv"
FORECAST_CSV = REPO_ROOT / "data" / "forecast_latest.csv"
OUT_JSON = REPO_ROOT / "data" / "compare.json"

# Swell classification - class: open | caution | alternate | omit.
# "source" (alternate only) names the location whose swell/wave columns are
# substituted in. Kept here (not in locations.csv) since it's specific to
# this page, not the core pipeline.
SWELL_CLASS: dict[str, tuple[str, str, str | None]] = {
    "Williamstown": ("omit", "Fully inside Port Phillip Bay behind The Heads. No nearby alternate within ~20km - swell panel dropped entirely.", None),
    "Portland Bay": ("caution", "Harbour side of the peninsula - some shelter. Cape Bridgewater (~15km) is the more exposed alternate if you'd rather see that instead.", None),
    "Apollo Bay": ("caution", "Some shelter from the cape itself, but still open to Bass Strait - own number should still be meaningful.", None),
    "Queenscliff": ("caution", "Inside The Heads - more sheltered than the open coast, but close enough to The Rip that swell can still push through.", None),
    "Eden": ("alternate", "Eden's own coordinates have returned zero swell/wave data since logging began (a wave-model land/sea-mask gap). Swell/wave shown here comes from Red Point, ~3km away at the mouth of Twofold Bay.", "Red Point"),
    "Batemans Bay (Inside)": ("alternate", "Inside the Clyde River estuary. Swell/wave shown here comes from Batemans Bay (Outside), ~3km away.", "Batemans Bay (Outside)"),
    "Ulladulla": ("caution", "Small harbour gives some shelter, open coast is close by - own number kept but flagged.", None),
    "Jervis Bay (Inside)": ("alternate", "Well inside the bay. Swell/wave shown here comes from Jervis Bay (Outside - Point Perpendicular), ~9km away.", "Jervis Bay (Outside - Point Perpendicular)"),
    "Shellharbour": ("caution", "Semi-sheltered small harbour, open coast nearby - own number kept but flagged.", None),
    "Fingal Bay": ("caution", "Moderately sheltered bay next to the spit. Broughton Island and Seal Rocks are open-coast alternates nearby if you want harsher numbers.", None),
    "South West Rocks": ("caution", "Trial Bay gives some shelter, open coast nearby - own number kept but flagged.", None),
}

PAST_DAYS_MAIN = 4     # weather/wind/rain/swell history window
PAST_DAYS_TIDE = 2     # tide history window (narrower, per Will's ask)
DOWNSAMPLE_STEP_HOURLY_METRICS = 3  # every 3rd hour for weather/wind/swell


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


def parse_utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# WMO weather_code -> rough severity rank, used to pick one representative
# code per day (the worst seen, not whatever happened at a fixed hour).
_WMO_SEVERITY = {
    0: 0, 1: 1, 2: 2, 3: 3,
    45: 4, 48: 4,
    51: 5, 53: 5, 55: 5, 71: 5, 73: 5, 75: 5, 77: 5,
    56: 6, 57: 6,
    61: 7, 63: 7, 65: 7, 80: 7, 81: 7, 82: 7, 85: 7, 86: 7,
    66: 8, 67: 8,
    95: 9, 96: 10, 99: 10,
}


def _weather_code_severity(code: float) -> int:
    return _WMO_SEVERITY.get(int(code), 0)


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

    def rows_for(name: str) -> list[dict]:
        a = sorted(by_loc_actual.get(name, []), key=lambda r: r["valid_time_utc"])
        f = sorted(by_loc_forecast.get(name, []), key=lambda r: r["valid_time_utc"])
        return a + f

    # Index every location's full row set by valid_time_utc once, so swell
    # substitution is a plain timestamp lookup into the source location's
    # own rows rather than re-deriving anything.
    rows_by_loc_by_time: dict[str, dict[str, dict]] = {}
    for loc in locations:
        name = loc["name"]
        rows_by_loc_by_time[name] = {r["valid_time_utc"]: r for r in rows_for(name)}

    out_locations = []
    for loc in locations:
        if loc.get("role", "site") != "site":
            continue  # swell-reference-only rows (Red Point) aren't displayed
        if loc.get("active", "true").strip().lower() == "false":
            continue
        name = loc["name"]
        own_rows = rows_for(name)
        if not own_rows:
            continue

        cls, note, source_name = SWELL_CLASS.get(name, ("open", "", None))
        source_rows = rows_by_loc_by_time.get(source_name, {}) if source_name else {}

        cutoff_past_main = now_utc - dt.timedelta(days=PAST_DAYS_MAIN)
        cutoff_past_tide = now_utc - dt.timedelta(days=PAST_DAYS_TIDE)

        wind, swell = [], []
        tide = []
        rain_hourly = []    # aggregated to daily totals below
        weather_hourly = []  # aggregated to daily hi/lo/code below

        for i, r in enumerate(own_rows):
            t = parse_utc(r["valid_time_utc"])
            if t < cutoff_past_main:
                continue
            is_forecast = "forecast_issued_at_utc" in r and r.get("forecast_issued_at_utc", "") != ""
            keep_hourly = (i % DOWNSAMPLE_STEP_HOURLY_METRICS == 0)

            weather_hourly.append((r["valid_time_local"][:10], to_float(r.get("air_temp_c")), to_float(r.get("weather_code")), is_forecast))

            if keep_hourly:
                wind.append({
                    "t": r["valid_time_utc"],
                    "wind_kmh": to_float(r.get("wind_speed_kmh")),
                    "gust_kmh": to_float(r.get("wind_gust_kmh")),
                    "dir_deg": to_float(r.get("wind_dir_deg")),
                    "fc": is_forecast,
                })
                if cls == "omit":
                    pass
                else:
                    src_row = source_rows.get(r["valid_time_utc"]) if source_name else r
                    swell.append({
                        "t": r["valid_time_utc"],
                        "swell_m": to_float((src_row or {}).get("swell_height_m")),
                        "period_s": to_float((src_row or {}).get("swell_period_s")),
                        "dir_deg": to_float((src_row or {}).get("swell_dir_deg")),
                        "fc": is_forecast,
                    })

            rain_hourly.append((r["valid_time_local"][:10], to_float(r.get("rainfall_mm")), is_forecast))

            if t >= cutoff_past_tide:
                tide.append({
                    "t": r["valid_time_utc"],
                    "height_m": to_float(r.get("sea_level_height_m")),
                    "fc": is_forecast,
                })

        # Daily rainfall totals (actual days get a real sum; the "today" /
        # forecast days mix actual-so-far + forecast-remaining, which is
        # fine - it's still "how much rain fell/will fall that day").
        rain_by_date: dict[str, float] = defaultdict(float)
        rain_is_forecast: dict[str, bool] = {}
        for date_str, mm, is_fc in rain_hourly:
            if mm is not None:
                rain_by_date[date_str] += mm
            rain_is_forecast[date_str] = rain_is_forecast.get(date_str, False) or is_fc
        rain_daily = [
            {"date": d, "mm": round(v, 1), "fc": rain_is_forecast.get(d, False)}
            for d, v in sorted(rain_by_date.items())
        ]

        # Daily weather summary (hi/lo air temp + a single representative
        # WMO weather_code). Picks the most *severe* code seen that day
        # (rain/storm over clear) rather than e.g. whatever the code was at
        # noon, since "it rained at some point" matters more for dive
        # planning than the midday snapshot. air_temp_c/weather_code are
        # new fields (added 2026-10-14) - days logged before that will come
        # back empty here until ~4 days of real history accumulate; this
        # deliberately shows that gap rather than inventing a value.
        temps_by_date: dict[str, list[float]] = defaultdict(list)
        codes_by_date: dict[str, list[float]] = defaultdict(list)
        weather_is_forecast: dict[str, bool] = {}
        for date_str, temp_c, code, is_fc in weather_hourly:
            if temp_c is not None:
                temps_by_date[date_str].append(temp_c)
            if code is not None:
                codes_by_date[date_str].append(code)
            weather_is_forecast[date_str] = weather_is_forecast.get(date_str, False) or is_fc
        all_weather_dates = sorted(set(temps_by_date) | set(codes_by_date) | set(rain_by_date))
        weather_daily = []
        for d in all_weather_dates:
            temps = temps_by_date.get(d, [])
            codes = codes_by_date.get(d, [])
            weather_daily.append({
                "date": d,
                "hi_c": round(max(temps), 1) if temps else None,
                "lo_c": round(min(temps), 1) if temps else None,
                "weather_code": max(codes, key=_weather_code_severity) if codes else None,
                "fc": weather_is_forecast.get(d, False),
            })

        out_locations.append({
            "name": name,
            "lat": to_float(loc["lat"]),
            "lon": to_float(loc["lon"]),
            "notes": loc.get("notes", ""),
            "swell_class": cls,       # open | caution | alternate | omit
            "swell_note": note,
            "swell_source": source_name,
            "series": {
                "weather_daily": weather_daily,
                "wind": wind,
                "rain_daily": rain_daily,
                "tide": tide,
                "swell": swell,
            },
        })

    out = {
        "generated_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "locations": out_locations,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"Wrote {OUT_JSON} ({len(out_locations)} locations)", file=sys.stderr)


if __name__ == "__main__":
    build()
