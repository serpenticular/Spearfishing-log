# Spearfishing weather & visibility logger

Logs wind, rainfall, swell, and sea temperature for chosen dive locations
every 6 hours, and computes a simple "visibility outlook" score to help
decide where and when conditions are likely to be best for spearfishing.
Currently tracks 35 locations across SA, VIC, and NSW (see `locations.csv`
for the full list and notes on each); add more any time by editing that
file. A few spots (Wilsons Prom, Batemans Bay, Jervis Bay) are logged as two
separate rows — an inside/sheltered point and an outside/ocean-facing point
— since conditions on the two sides can be very different.

**A note on comparing sites:** the visibility score uses the same fixed
scale everywhere (e.g. 0m swell = best, 2.5m = worst) rather than a scale
relative to each site's own "normal" conditions. That's deliberate — the
point is to answer "which of these places has the calmest water right
now", so a sheltered bay (Williamstown, Portland Bay) will often score
higher than a fully exposed Southern Ocean coast (Cape Otway, Port
MacDonnell) even on a good day for that coast. Read a low score at an
exposed site as "rougher than a bay, as expected", not as "something's
wrong" — the `notes` column on each location flags how exposed it typically is.

## How it runs

This repo runs itself. `.github/workflows/log-weather.yml` is a GitHub
Actions workflow that fires every 6 hours (cron `0 */6 * * *`, UTC),
regardless of whether your computer is on: it checks out this repo, runs
`scripts/fetch_and_log.py`, and commits any new data straight back into
`data/`. No server, no API keys, nothing to keep running yourself.

### One-time setup

1. Create a new **GitHub repository** (github.com/new). Public or private,
   your call — private is free too and keeps your logs to yourself.
2. Upload every file in this folder into that repo, preserving the folder
   structure (the `.github/workflows/log-weather.yml` path matters — GitHub's
   "Add file → Upload files" screen supports dragging the whole folder in
   one go and it will keep the paths).
3. Commit. GitHub Actions is on by default for personal repos, so the
   schedule starts working immediately — but the first run won't happen
   until the next 6-hour mark. To get data right away: go to the **Actions**
   tab → "Log weather & visibility data" → **Run workflow** (this is the
   `workflow_dispatch` trigger) to fire it immediately.
4. Check the run went green, then open `data/readings_actual.csv` and
   `data/forecast_latest.csv` in the repo to see the first rows land.

That's it — from here it logs itself. Coming back to add a feature (e.g. a
map) just means editing files in this same repo.

### Adding more locations later

Add a row to `locations.csv`, following the existing format (name, lat,
lon, active, and a free-text note on how exposed/sheltered the spot is —
useful context when comparing scores across sites later). The next scheduled run (or a manual "Run workflow") will start logging that
location too. Set `active` to `false` to pause a location without deleting
its history — with 35 locations already tracked, this is the easy way to
trim down to just the spots you're actually planning around this week
without losing the rest of the log.

## What gets logged

Two files in `data/`:

- **`readings_actual.csv`** — the permanent historical record. Append-only:
  every run adds whichever hours have newly passed since the last run (it
  never rewrites or deletes a row), so this file is the dataset to use for
  spotting patterns over time or eventually calibrating the score against
  real dive outcomes.
- **`forecast_latest.csv`** — a snapshot, not a history. Every run
  **overwrites** it with the freshest ~72-hour outlook, so it always
  answers "what's predicted from now". If you want to analyse forecast
  *accuracy* later (comparing a 48h-out forecast to what actually happened),
  that needs a small change to start keeping forecast history too — flag it
  when you're ready and it's a straightforward addition.

Columns (same set in both files, forecast rows also carry
`forecast_issued_at_utc` and `lead_time_hours`):

| Column | Meaning |
|---|---|
| `valid_time_utc` / `valid_time_local` | The hour this row describes (local = Australia/Melbourne, handles DST automatically) |
| `wind_speed_kmh`, `wind_dir_deg`, `wind_gust_kmh` | 10 m wind |
| `rainfall_mm` | Rain in that hour |
| `swell_height_m`, `swell_period_s`, `swell_dir_deg` | Swell component of the sea state |
| `wave_height_m` | Combined sea state (swell + wind waves) |
| `wind_wave_height_m` | Locally wind-driven chop component |
| `sea_surface_temp_c` | Sea surface temperature |
| `chlorophyll_mg_m3`, `chlorophyll_source` | Satellite chlorophyll-a, when a recent clear satellite pass is available (see limitations) |
| `visibility_score`, `visibility_label` | This logger's own heuristic outlook (0-100, Poor/Fair/Good/Excellent) — see below |

## The visibility score

A transparent, hand-tuned starting point — not a validated model. It
weighs five factors, each turned into a 0-100 "good for visibility"
sub-score, then combines whichever of them have data (missing factors are
dropped and the rest reweighted, rather than penalising a gap):

- **Wind (30%)** — calmer is better; scores 100 at 0 km/h down to 0 at 35 km/h.
- **Rainfall, trailing 24h (25%)** — less recent rain is better (runoff/turbidity); 100 at 0 mm down to 0 at 20 mm.
- **Swell/wave height (20%)** — lower is better (less bottom disturbance); 100 at 0 m down to 0 at 2.5 m.
- **Sea-surface-temperature anomaly (15%)** — colder than the trailing 14-day average at that location suggests upwelling of colder, often murkier water; needs at least a couple of weeks of your own logged history before it activates (returns "insufficient data" until then, which is normal early on).
- **Chlorophyll-a (10%)** — lower is better (less algae/plankton haze); 100 at 0.5 mg/m³ down to 0 at 5 mg/m³.

All five weights and thresholds live at the top of `scripts/fetch_and_log.py`
in the `WEIGHTS` dict and the `compute_visibility()` function — once you've
logged a season of real dives against this score, that's the place to
recalibrate it (e.g. if it's calling "Excellent" on days you actually found
murky, tighten the wind or rainfall threshold).

## Known limitations, read before trusting this blindly

- **Sheltered vs. exposed sites need different reading.** A handful of
  locations sit inside bays/estuaries (Williamstown, Portland Bay,
  Queenscliff, the "Inside" points at Batemans Bay and Jervis Bay) where
  Open-Meteo's marine model — a global wave model — won't perfectly resolve
  a shallow, enclosed body of water; expect their swell/wave figures to be
  indicative rather than precise, with wind and rainfall the more
  trustworthy signals there. Everything marked "Outside"/open-coast in
  `locations.csv`'s notes column is where the swell data is most meaningful
  and most likely to be the deciding factor.
- **"Actual" data is model reanalysis, not a buoy reading.** There's no
  free, no-signup marine buoy network for Port Phillip Bay, so recent-past
  hours from Open-Meteo (its best model analysis) stand in for a true
  observation. Good enough for spotting trends; not instrument-grade.
- **Chlorophyll is experimental and will often be blank.** Satellite
  ocean-colour passes are infrequent and blocked by cloud, and the exact
  NOAA CoastWatch dataset id may need revisiting over time as products get
  replaced. When it fails, `chlorophyll_source` says why — that's expected
  behaviour, not a bug, and it never blocks the rest of the row from logging.
- **The visibility score is a first guess**, not a physics model or
  something trained on real outcomes. Treat "Excellent" vs "Poor" as a
  rough sort, and recalibrate the weights once you have your own dive log
  to check it against (a `notes` column is included in each row for
  exactly that — jot down what you actually saw).

## Testing changes

`scripts/test_offline.py` replays real captured API responses
(`scripts/fixture_marine.json`, `fixture_weather.json`) through the actual
pipeline, so you can check a change to the scoring or parsing logic without
needing to wait for a live run:

```
python3 scripts/test_offline.py
```

It checks that rows parse correctly, that re-running doesn't duplicate
rows, and that rolling forward in time appends new actuals correctly.

## Roadmap (not built yet)

- A **map view** comparing all active locations at a glance (wind, swell,
  and visibility score side by side) — natural next step once there are a
  few locations logging. Come back and ask for this once you've got more
  than Williamstown in `locations.csv`, or even before — a first version
  can work off a single location.
- **Forecast-accuracy tracking** — keeping forecast history (not just the
  latest snapshot) so you can see how far in advance the forecast tends to
  be trustworthy.
- **Manual dive-log entries** (actual visibility you observed, in metres)
  joined against the logged conditions for that day, to properly calibrate
  the score instead of hand-tuning it.
