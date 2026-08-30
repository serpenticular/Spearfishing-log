#!/usr/bin/env python3
"""
Spearfishing weather/visibility logger.

Fetches marine + weather forecast data (and a best-effort water-clarity
proxy: sea surface temperature anomaly and satellite chlorophyll-a) for
every active location in locations.csv, computes a simple transparent
"visibility outlook" score, and:

  * appends newly-observed hours to data/readings_actual.csv (append-only,
    de-duplicated by location + valid_time_utc — safe to re-run)
  * overwrites data/forecast_latest.csv with the freshest forecast snapshot
    for the next FORECAST_HOURS hours (this file always reflects "as of the
    most recent run", it is not a history)

Designed to be run every ~6 hours by the GitHub Actions workflow in
.github/workflows/log-weather.yml, but is also safe to run by hand:

    pip install -r requirements.txt
    python scripts/fetch_and_log.py

Data sources (all free, no API key required):
  - Open-Meteo Marine API   https://open-meteo.com/en/docs/marine-weather-api
      swell height/period/direction, wind-wave height, sea surface temperature,
      ocean current speed/direction, sea level height (tide proxy)
  - Open-Meteo Forecast API https://open-meteo.com/en/docs/
      wind speed/direction/gusts, rainfall
  - NOAA CoastWatch ERDDAP (best-effort, see fetch_chlorophyll)
      satellite chlorophyll-a, as a plankton/turbidity proxy

Ocean current + sea level height were added after a round of research into
what generally affects dive/spearfishing visibility (wind chop, rainfall
runoff, swell/wave action, tidal current stirring sediment, algae blooms,
bottom composition). Current speed is folded into the visibility score
(higher current -> more resuspended sediment); sea level height is stored
but NOT scored, since divers report tide effects are highly site-specific
with no universal "high tide is better" rule - it's surfaced as raw
rising/falling context instead. See README.md for the full write-up and
sourcing.

Notes / known limitations (see README.md for the full write-up):
  - Open-Meteo's marine model is a global wave model. For a sheltered,
    shallow embayment like Port Phillip Bay it will under/over-resolve
    true local wind-chop; treat swell/wave figures for bay locations as
    indicative, not precise. Wind + rainfall are the more trustworthy
    signals there.
  - "Actual" data from Open-Meteo is the model's best analysis for recent
    past hours, not a buoy/instrument reading. There is no free, no-signup
    marine buoy network for Port Phillip Bay, so this is the best available
    free proxy.
  - Chlorophyll-a fetch is experimental: satellite passes are infrequent
    and cloud cover creates gaps, and the exact ERDDAP dataset id may need
    to be revisited over time as NOAA rotates products. Failures here are
    caught and logged as a blank value + reason; they never break the rest
    of the run.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCATIONS_CSV = REPO_ROOT / "locations.csv"
ACTUAL_CSV = REPO_ROOT / "data" / "readings_actual.csv"
FORECAST_CSV = REPO_ROOT / "data" / "forecast_latest.csv"

PAST_HOURS_TO_CHECK = 48   # how far back we ask the API for, dedup handles overlap
FORECAST_HOURS = 120       # how far ahead the forecast snapshot covers (5 days)
PAST_DAYS = -(-PAST_HOURS_TO_CHECK // 24)      # ceil division -> days to request
FORECAST_DAYS = -(-FORECAST_HOURS // 24) + 1   # ceil + 1 day buffer -> days to request
HTTP_TIMEOUT = 30
USER_AGENT = "spearfishing-visibility-logger/1.0 (personal weather/visibility log)"

FIELDNAMES = [
    "visibility_score",
    "visibility_label",
    "swell_band",
    "logged_at_utc",
    "location",
    "valid_time_utc",
    "valid_time_local",
    "wind_speed_kmh",
    "wind_dir_deg",
    "wind_gust_kmh",
    "rainfall_mm",
    "swell_height_m",
    "swell_period_s",
    "swell_dir_deg",
    "wave_height_m",
    "wind_wave_height_m",
    "current_velocity_kmh",
    "current_dir_deg",
    "sea_level_height_m",
    "sea_surface_temp_c",
    "chlorophyll_mg_m3",
    "chlorophyll_source",
    "notes",
]
FORECAST_FIELDNAMES = [
    "visibility_score",
    "visibility_label",
    "swell_band",
    "forecast_issued_at_utc",
    "lead_time_hours",
    "location",
    "valid_time_utc",
    "valid_time_local",
    "wind_speed_kmh",
    "wind_dir_deg",
    "wind_gust_kmh",
    "rainfall_mm",
    "swell_height_m",
    "swell_period_s",
    "swell_dir_deg",
    "wave_height_m",
    "wind_wave_height_m",
    "current_velocity_kmh",
    "current_dir_deg",
    "sea_level_height_m",
    "sea_surface_temp_c",
    "chlorophyll_mg_m3",
    "chlorophyll_source",
    "notes",
]


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------

HTTP_MAX_ATTEMPTS = 3  # 1 try + 2 retries
HTTP_RETRY_BACKOFF_S = 3  # doubles each retry: 3s, 6s


def http_get_json(url: str) -> dict:
    """GET url as JSON, retrying transient network/SSL hiccups only.

    Sequential runs of 70-100+ requests per job (35 locations x 2-3 calls
    each) occasionally hit an isolated SSL handshake timeout against
    Open-Meteo/ERDDAP with no fault of the location itself - seen in
    practice as a handful of "<urlopen error ... handshake operation timed
    out>" failures per run, each silently dropping that location's data for
    the whole 6-hour cycle. A couple of short retries clears essentially
    all of these.

    IMPORTANT: urllib.error.HTTPError (a real HTTP response - 404, 429, 500,
    etc.) is a *subclass* of URLError, but is deliberately NOT retried here.
    An early version of this retry loop caught HTTPError too, which turned
    a burst of 404s (seen in testing after several manual runs in quick
    succession - most likely Open-Meteo rate-limiting/blocking the shared
    GitHub Actions IP range rather than anything wrong with the request)
    into 3x as many requests and a much slower failure, without ever having
    a chance of succeeding. Only a connection-level failure (timeout, DNS,
    connection reset - a plain URLError, not an HTTPError) is worth retrying;
    an HTTP error response should fail this location fast, same as before
    this retry logic existed.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc: urllib.error.URLError | None = None
    for attempt in range(1, HTTP_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise  # real HTTP response (404/429/5xx/...) - fail fast, don't retry
        except urllib.error.URLError as exc:
            last_exc = exc
            if attempt < HTTP_MAX_ATTEMPTS:
                wait_s = HTTP_RETRY_BACKOFF_S * (2 ** (attempt - 1))
                print(f"  (retrying after {exc} - attempt {attempt}/{HTTP_MAX_ATTEMPTS})", file=sys.stderr)
                time.sleep(wait_s)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------

def fetch_marine(lat: float, lon: float) -> dict:
    """Open-Meteo Marine API: swell/wave + sea surface temperature."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "wave_height",
            "swell_wave_height",
            "swell_wave_period",
            "swell_wave_direction",
            "wind_wave_height",
            "sea_surface_temperature",
            "ocean_current_velocity",
            "ocean_current_direction",
            "sea_level_height_msl",
        ]),
        "timezone": "Australia/Sydney",
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
    }
    url = "https://marine-api.open-meteo.com/v1/marine?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def fetch_weather(lat: float, lon: float) -> dict:
    """Open-Meteo Forecast API: wind + rainfall."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
            "precipitation",
        ]),
        "timezone": "Australia/Sydney",
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def fetch_chlorophyll(lat: float, lon: float) -> tuple[float | None, str]:
    """
    Best-effort satellite chlorophyll-a lookup (mg/m^3) via NOAA CoastWatch
    ERDDAP. This is experimental: dataset availability, satellite revisit
    time, and cloud cover mean this frequently returns None. Any failure is
    swallowed here and reported as (None, "unavailable: <reason>") so a bad
    or rotated dataset id never breaks the wind/swell/rain logging above.
    """
    dataset_candidates = [
        # (dataset_id, variable_name) — tried in order; first success wins.
        ("noaacwNPPVIIRSchlaDaily", "chlor_a"),
        ("erdVH2chlamday", "chla"),
    ]
    for dataset_id, var in dataset_candidates:
        try:
            url = (
                f"https://coastwatch.pfeg.noaa.gov/erddap/griddap/{dataset_id}.json?"
                f"{var}%5B(last)%5D%5B({lat})%5D%5B({lon})%5D"
            )
            data = http_get_json(url)
            rows = data["table"]["rows"]
            if rows and rows[0][-1] is not None:
                return float(rows[0][-1]), f"NOAA CoastWatch ERDDAP ({dataset_id})"
        except Exception:
            continue
    return None, "unavailable (no satellite pass / dataset error)"


# --------------------------------------------------------------------------
# Visibility heuristic
# --------------------------------------------------------------------------
# Transparent, hand-tuned v1 scoring. Each factor maps to a 0-100 "good for
# visibility" sub-score, then a weighted average is taken over whichever
# factors we actually have data for (missing factors are dropped and the
# remaining weights renormalised, rather than penalising missing data).
#
# Tune these constants once you have logged enough real dives against the
# score to see where it's over/under-calling conditions.

WEIGHTS = {
    "wind": 0.25,          # lower wind -> less chop / resuspended sediment
    "rainfall": 0.20,      # less recent rain -> less runoff turbidity
    "swell": 0.20,         # lower swell/wave height -> less bottom disturbance
    "current": 0.15,       # lower tidal current -> less resuspended sediment
    "sst_anomaly": 0.15,   # colder-than-recent-average SST -> possible upwelling
    "chlorophyll": 0.05,   # lower chlorophyll -> less algae/plankton haze
}


def swell_band(swell_height_m: float | None) -> str:
    """
    Diveability band from swell height, using thresholds supplied directly
    by the user (their own on-the-water judgement, not a turbidity measure -
    this is deliberately separate from the visibility_score above, which is
    about water clarity, not sea state/comfort).
    """
    if swell_height_m is None:
        return ""
    if swell_height_m <= 0.3:
        return "Amazing"
    if swell_height_m <= 0.8:
        return "Great"
    if swell_height_m <= 1.2:
        return "Doable"
    if swell_height_m <= 1.5:
        return "Bit dodgy"
    return "Getting rough"


def _linear_score(value: float, good_at: float, bad_at: float) -> float:
    """100 at good_at, 0 at bad_at (or worse), linear between."""
    if bad_at == good_at:
        return 100.0
    frac = (value - good_at) / (bad_at - good_at)
    frac = max(0.0, min(1.0, frac))
    return 100.0 * (1.0 - frac)


def compute_visibility(
    wind_speed_kmh: float | None,
    rainfall_24h_mm: float | None,
    swell_or_wave_m: float | None,
    sst_c: float | None,
    sst_recent_avg_c: float | None,
    chlorophyll_mg_m3: float | None,
    current_velocity_kmh: float | None = None,
) -> tuple[float | None, str]:
    scores: dict[str, float] = {}

    if wind_speed_kmh is not None:
        scores["wind"] = _linear_score(wind_speed_kmh, good_at=0, bad_at=35)

    if rainfall_24h_mm is not None:
        scores["rainfall"] = _linear_score(rainfall_24h_mm, good_at=0, bad_at=20)

    if swell_or_wave_m is not None:
        scores["swell"] = _linear_score(swell_or_wave_m, good_at=0, bad_at=2.5)

    if current_velocity_kmh is not None:
        # Generic threshold, not tuned per site: a handful of these 35
        # locations (e.g. Port Phillip Heads/The Rip) are known strong-current
        # sites where this will correctly score "poor" near peak tidal flow
        # even though the site can be excellent at slack water.
        scores["current"] = _linear_score(current_velocity_kmh, good_at=0, bad_at=8)

    if sst_c is not None and sst_recent_avg_c is not None:
        anomaly = sst_recent_avg_c - sst_c  # positive = colder than recent avg
        scores["sst_anomaly"] = _linear_score(anomaly, good_at=0, bad_at=3)

    if chlorophyll_mg_m3 is not None:
        scores["chlorophyll"] = _linear_score(chlorophyll_mg_m3, good_at=0.5, bad_at=5.0)

    if not scores:
        return None, "insufficient data"

    total_weight = sum(WEIGHTS[k] for k in scores)
    weighted = sum(WEIGHTS[k] * scores[k] for k in scores) / total_weight
    score = round(weighted, 1)

    if score >= 80:
        label = "Excellent"
    elif score >= 60:
        label = "Good"
    elif score >= 40:
        label = "Fair"
    else:
        label = "Poor"
    return score, label


# --------------------------------------------------------------------------
# CSV helpers
# --------------------------------------------------------------------------

def read_locations() -> list[dict]:
    with open(LOCATIONS_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("active", "true").strip().lower() != "false"]


def load_existing_actual_keys() -> set[tuple[str, str]]:
    if not ACTUAL_CSV.exists():
        return set()
    with open(ACTUAL_CSV, newline="", encoding="utf-8") as f:
        return {(r["location"], r["valid_time_utc"]) for r in csv.DictReader(f)}


def migrate_schema() -> None:
    """
    Self-healing: if readings_actual.csv already exists with an old header -
    columns in a different order (e.g. before visibility_score/label moved to
    the front) and/or missing columns that were added later (e.g. the
    current/tide fields) - rewrite it in place against the current
    FIELDNAMES, rather than requiring anyone to hand-edit a growing history
    file. Old rows get blank values for any newly-added columns. A no-op
    once the file already matches FIELDNAMES exactly. Only ever adds/
    reorders columns; if the on-disk header contains something FIELDNAMES
    doesn't recognise at all, this leaves the file alone rather than guess.
    """
    if not ACTUAL_CSV.exists():
        return
    with open(ACTUAL_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        if header == FIELDNAMES:
            return  # already correct, nothing to do
        if header is None or not set(header).issubset(set(FIELDNAMES)):
            return  # unexpected/unrecognised shape - leave it alone rather than guess
        rows = list(reader)
    write_rows(ACTUAL_CSV, FIELDNAMES, rows)
    print(f"Migrated {ACTUAL_CSV} to the current schema.", file=sys.stderr)


def recent_sst_average(location: str, before_utc: str, days: int = 14) -> float | None:
    """Rolling average SST from our own accumulated actuals, used as the
    'normal for this time of year, at this place' baseline for the upwelling
    proxy. Returns None until enough history has accumulated."""
    if not ACTUAL_CSV.exists():
        return None
    cutoff = dt.datetime.fromisoformat(before_utc.replace("Z", "+00:00")) - dt.timedelta(days=days)
    vals = []
    with open(ACTUAL_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["location"] != location or not r.get("sea_surface_temp_c"):
                continue
            try:
                t = dt.datetime.fromisoformat(r["valid_time_utc"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if cutoff <= t < dt.datetime.fromisoformat(before_utc.replace("Z", "+00:00")):
                vals.append(float(r["sea_surface_temp_c"]))
    if len(vals) < 8:  # need at least a couple of days of readings
        return None
    return sum(vals) / len(vals)


def append_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def write_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# Main per-location pipeline
# --------------------------------------------------------------------------

def process_location(loc: dict, run_started_utc: str, existing_keys: set) -> tuple[list[dict], list[dict]]:
    name = loc["name"]
    lat, lon = float(loc["lat"]), float(loc["lon"])
    print(f"[{name}] fetching marine + weather data...", file=sys.stderr)

    marine = fetch_marine(lat, lon)
    weather = fetch_weather(lat, lon)
    chlor_value, chlor_source = fetch_chlorophyll(lat, lon)

    m_hourly = marine["hourly"]
    w_hourly = weather["hourly"]
    utc_offset = dt.timedelta(seconds=marine.get("utc_offset_seconds", 36000))

    # Both APIs were asked for the same past_days/forecast_days window with
    # the same timezone, so their local-time arrays line up hour-for-hour.
    times_local = m_hourly["time"]
    assert times_local == w_hourly["time"], f"[{name}] marine/weather time arrays out of sync"

    now_utc = dt.datetime.fromisoformat(run_started_utc.replace("Z", "+00:00"))

    actual_rows: list[dict] = []
    forecast_rows: list[dict] = []
    rainfall_series = w_hourly["precipitation"]

    for i, t_local_str in enumerate(times_local):
        t_local = dt.datetime.fromisoformat(t_local_str)
        t_utc = (t_local - utc_offset).replace(tzinfo=dt.timezone.utc)
        t_utc_str = t_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        is_future = t_utc > now_utc

        wind_speed = w_hourly["wind_speed_10m"][i]
        wind_dir = w_hourly["wind_direction_10m"][i]
        wind_gust = w_hourly["wind_gusts_10m"][i]
        rainfall = w_hourly["precipitation"][i]
        rainfall_24h = sum(rainfall_series[max(0, i - 23): i + 1])

        swell_height = m_hourly["swell_wave_height"][i]
        swell_period = m_hourly["swell_wave_period"][i]
        swell_dir = m_hourly["swell_wave_direction"][i]
        wave_height = m_hourly["wave_height"][i]
        wind_wave_height = m_hourly["wind_wave_height"][i]
        current_velocity = m_hourly.get("ocean_current_velocity", [None] * len(times_local))[i]
        current_dir = m_hourly.get("ocean_current_direction", [None] * len(times_local))[i]
        sea_level_height = m_hourly.get("sea_level_height_msl", [None] * len(times_local))[i]
        sst = m_hourly["sea_surface_temperature"][i]

        sst_avg = recent_sst_average(name, t_utc_str) if not is_future else None
        score, label = compute_visibility(
            wind_speed_kmh=wind_speed,
            rainfall_24h_mm=rainfall_24h,
            swell_or_wave_m=wave_height,
            sst_c=sst,
            sst_recent_avg_c=sst_avg,
            chlorophyll_mg_m3=chlor_value,
            current_velocity_kmh=current_velocity,
        )

        row = {
            "location": name,
            "valid_time_utc": t_utc_str,
            "valid_time_local": t_local_str,
            "wind_speed_kmh": wind_speed,
            "wind_dir_deg": wind_dir,
            "wind_gust_kmh": wind_gust,
            "rainfall_mm": rainfall,
            "swell_height_m": swell_height,
            "swell_period_s": swell_period,
            "swell_dir_deg": swell_dir,
            "wave_height_m": wave_height,
            "wind_wave_height_m": wind_wave_height,
            "current_velocity_kmh": current_velocity if current_velocity is not None else "",
            "current_dir_deg": current_dir if current_dir is not None else "",
            "sea_level_height_m": sea_level_height if sea_level_height is not None else "",
            "sea_surface_temp_c": sst,
            "chlorophyll_mg_m3": chlor_value if chlor_value is not None else "",
            "chlorophyll_source": chlor_source,
            "visibility_score": score if score is not None else "",
            "visibility_label": label,
            "swell_band": swell_band(swell_height),
            "notes": "",
        }

        if is_future:
            lead_hours = round((t_utc - now_utc).total_seconds() / 3600)
            if 0 <= lead_hours <= FORECAST_HOURS:
                forecast_rows.append({
                    "forecast_issued_at_utc": run_started_utc,
                    "lead_time_hours": lead_hours,
                    **row,
                })
        else:
            key = (name, t_utc_str)
            if key not in existing_keys:
                actual_rows.append({"logged_at_utc": run_started_utc, **row})

    return actual_rows, forecast_rows


def main() -> None:
    run_started_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    locations = read_locations()
    if not locations:
        print("No active locations in locations.csv - nothing to do.", file=sys.stderr)
        return

    migrate_schema()
    existing_keys = load_existing_actual_keys()
    all_new_actuals: list[dict] = []
    all_forecast_rows: list[dict] = []

    for loc in locations:
        try:
            actuals, forecasts = process_location(loc, run_started_utc, existing_keys)
        except (urllib.error.URLError, KeyError, AssertionError, ValueError) as exc:
            print(f"[{loc.get('name')}] FAILED: {exc}", file=sys.stderr)
            continue
        all_new_actuals.extend(actuals)
        all_forecast_rows.extend(forecasts)
        existing_keys.update((r["location"], r["valid_time_utc"]) for r in actuals)

    if all_new_actuals:
        append_rows(ACTUAL_CSV, FIELDNAMES, all_new_actuals)
        print(f"Appended {len(all_new_actuals)} new actual rows to {ACTUAL_CSV}", file=sys.stderr)
    else:
        print("No new actual rows (nothing newer than what's already logged).", file=sys.stderr)

    if all_forecast_rows:
        # forecast_latest.csv is a snapshot, not a history: always overwrite.
        write_rows(FORECAST_CSV, FORECAST_FIELDNAMES, all_forecast_rows)
        print(f"Wrote {len(all_forecast_rows)} forecast rows to {FORECAST_CSV}", file=sys.stderr)


if __name__ == "__main__":
    main()
