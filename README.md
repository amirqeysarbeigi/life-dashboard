# Life Dashboard

A personal analytics web app that turns Google Calendar events and optional Things 3 tasks into an interactive view of time allocation, progress, and daily routines.

Built to answer a personal question: **Where is my time going, and how does my recorded activity change over time?** The product combines a Python/Flask backend with a vanilla JavaScript and Chart.js interface. It runs locally and keeps account credentials and calendar data out of this repository.

## What it does

- Normalizes calendar events into a shared representation for duration, date, category, and focus.
- Compares recorded activity across learning, work, workouts, sleep, daily operations, leisure, and wasted time.
- Supports interactive time-allocation charts, category filters, period navigation, trends, heatmaps, milestones, and pace estimates.
- Explores same-day and next-day relationships with coverage checks, rank correlations, and multiple-comparison correction. These are exploratory associations, not causal findings.
- Keeps available areas usable when an optional calendar fails; offers retry states and preserves the last successful view during failed refreshes.
- Optionally reads Things 3 tasks on macOS through a read-only SQLite snapshot.

## Architecture

```text
Google Calendar API -> normalize timed events -> Flask analytics routes
                                             -> in-memory / SQLite cache
                                             -> JavaScript + Chart.js UI
Things 3 (optional) -> read-only snapshot -----^
```

`/` is the landing page; `/dashboard` is the analytics interface. There is no frontend build step. Fonts and Chart.js are loaded from public CDNs; calendar access uses Google's API.

## Run locally

Requires Python 3.10+ and a Google account. Node.js is needed only for JavaScript tests.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

1. In Google Cloud Console, enable the Google Calendar API and configure an OAuth consent screen for your own account.
2. Create an OAuth client of type **Desktop app** and save its downloaded JSON as `credentials.json` in the project directory.
3. Set calendar display names and goals in your local `config.yaml`. Examples are generic; they contain no account identifiers.
4. Run `python app.py`, open the local address below, and complete Google consent when a calendar endpoint first requests access. The app requests read-only Calendar access.

```sh
python app.py
# Open http://127.0.0.1:5001
```

The default development server binds to `127.0.0.1`; this app is intended for local, single-user use. It does not provide authentication for an Internet-facing deployment. `PORT=8080 python app.py` changes the port.

Things integration is disabled in the example configuration. Enable it explicitly if you use Things 3 on macOS; no Things database is distributed here. With it disabled, the rest of the dashboard remains usable.

## Verification

```sh
python -m unittest discover -s tests
node tests/insights_stats.cjs
PYTHON=python node tests/frontend_runtime.cjs
```

The tests use generated fixtures and temporary databases. They cover backend calculations, period boundaries, sleep-day handling, optional data availability, frontend behavior, and statistical routines. They do not require your Google credentials.

## Privacy and repository scope

This repository contains source code, tests, and an example configuration. It includes no OAuth credentials, access or refresh tokens, real calendar events, task databases, cached analytics, personal configuration, or local assistant history.

`.gitignore` excludes these local files and their common backup forms. Keep `credentials.json`, `token.json`, `config.yaml`, `adjustments.json`, database files, and exports private. Use synthetic data for any public screenshots or demo recording.

## Limitations

Recorded calendar time is not proof of task completion or productivity. Missing logs cannot reliably be distinguished from inactivity. Calendar entries can overlap, so recorded totals may exceed elapsed wall-clock time. Statistical relationships are exploratory and can be affected by changing routines and repeated selection of time windows.

This is a personal project, not a hosted service. Source publication does not provide a live demo or demo video.
