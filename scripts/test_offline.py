"""
Offline smoke test: replays real Open-Meteo responses (captured live during
development, since this sandbox itself can't reach the internet) through the
actual parsing/scoring pipeline in fetch_and_log.py, to catch bugs before
this ever runs for real in GitHub Actions.
"""
import datetime as real_dt_module
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_and_log as mod

RealDateTime = real_dt_module.datetime  # grab the real class before anything gets patched

MARINE_FIXTURE = json.loads(Path(__file__).with_name("fixture_marine.json").read_text())
WEATHER_FIXTURE = json.loads(Path(__file__).with_name("fixture_weather.json").read_text())


def fake_fetch_marine(lat, lon):
    return MARINE_FIXTURE


def fake_fetch_weather(lat, lon):
    return WEATHER_FIXTURE


def fake_fetch_chlorophyll(lat, lon):
    return 1.2, "TEST FIXTURE"


class FrozenDateTime(RealDateTime):
    _fixed_now = None

    @classmethod
    def now(cls, tz=None):
        return cls._fixed_now if tz is None else cls._fixed_now.astimezone(tz)


def run_main_at(fixed_now_str: str):
    FrozenDateTime._fixed_now = RealDateTime.fromisoformat(fixed_now_str.replace("Z", "+00:00"))
    with mock.patch.object(mod, "fetch_marine", fake_fetch_marine), \
         mock.patch.object(mod, "fetch_weather", fake_fetch_weather), \
         mock.patch.object(mod, "fetch_chlorophyll", fake_fetch_chlorophyll), \
         mock.patch.object(mod.dt, "datetime", FrozenDateTime):
        mod.main()


tmp_dir = Path("/tmp/spearfishing_test")
tmp_dir.mkdir(exist_ok=True)
mod.ACTUAL_CSV = tmp_dir / "readings_actual.csv"
mod.FORECAST_CSV = tmp_dir / "forecast_latest.csv"
mod.LOCATIONS_CSV = Path(__file__).resolve().parent.parent / "locations.csv"
for p in (mod.ACTUAL_CSV, mod.FORECAST_CSV):
    if p.exists():
        p.unlink()

# Pin "now" to a time inside the fixture's range so some rows land as
# "actual" (past) and some as "forecast" (future).
run_main_at("2026-08-30T06:00:00Z")

print("\n--- readings_actual.csv (first 5 lines) ---")
actual_lines = mod.ACTUAL_CSV.read_text().splitlines()
print("\n".join(actual_lines[:5]))
print(f"... total lines: {len(actual_lines)}")

print("\n--- forecast_latest.csv (first 5 lines) ---")
forecast_lines = mod.FORECAST_CSV.read_text().splitlines()
print("\n".join(forecast_lines[:5]))
print(f"... total lines: {len(forecast_lines)}")

# Re-run at the same instant to prove idempotency: no duplicate actual rows.
before = len(mod.ACTUAL_CSV.read_text().splitlines())
run_main_at("2026-08-30T06:00:00Z")
after = len(mod.ACTUAL_CSV.read_text().splitlines())
assert before == after, f"Re-run should not duplicate rows: {before} -> {after}"
print(f"\nIdempotency check OK: re-running did not add duplicate rows ({before} lines both times).")

# Advance "now" by 6 hours (simulating the next scheduled run) and confirm
# new actual rows get appended (not overwritten) while forecast is refreshed.
run_main_at("2026-08-30T12:00:00Z")
after2 = len(mod.ACTUAL_CSV.read_text().splitlines())
assert after2 > after, f"Expected new actual rows after advancing time, got {after} -> {after2}"
print(f"Rolling-forward check OK: actual rows grew from {after} to {after2} lines after +6h.")
