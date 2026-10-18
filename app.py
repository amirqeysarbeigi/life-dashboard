"""Flask backend for the 10,000-hour mastery dashboard.

The app reads time-tracked events from several Google Calendars and turns them
into the JSON payloads consumed by ``index.html``. Each "domain" maps to one
calendar and one API route:

    domain        calendar(s)            route             cache
    ------------  ---------------------  ----------------  ------------------
    education     courses + research     /api/analytics    _analytics_cache
    growth        growth                 /api/growth       _growth_cache
    operations    operations             /api/operations   _operations_cache
    fun           fun                    /api/fun          _fun_cache
    fucked        fucked                 /api/fucked       _fucked_cache
    things        Things 3 SQLite DB     /api/things       _things_cache

High-level request flow for every domain:

    get_<domain>_analytics()      # cache gate (TTL from config)
      -> fetch_* events from Google Calendar (or Things DB)
      -> build_<domain>_payload() # pure transform: events -> JSON dict
      -> stamp payload["meta"] and store in the module-level cache

All hour math lives in the ``build_*`` / ``compute_*`` helpers, which are pure
functions over parsed event dicts (see ``parse_event`` for that shape). Keeping
them side-effect free makes them easy to test and reason about.

Auth: OAuth happens once via ``credentials.json`` -> ``token.json`` (see
``get_credentials``). Config and tunable thresholds live in ``config.yaml``
(see ``load_config`` for the schema and defaults).
"""

import csv
import io
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory
from things import fetch_things_data
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
ADJUSTMENTS_PATH = os.path.join(BASE_DIR, "adjustments.json")
CACHE_DB_PATH = os.path.join(BASE_DIR, "cache.db")

# Only explicitly registered routes are public; project files include OAuth secrets.
app = Flask(__name__, static_folder=None)

_config = None
_analytics_cache = {"payload": None, "fetched_at": None}
_growth_cache = {"payload": None, "fetched_at": None}
_operations_cache = {"payload": None, "fetched_at": None}
_work_cache = {"payload": None, "fetched_at": None}
_fun_cache = {"payload": None, "fetched_at": None}
_fucked_cache = {"payload": None, "fetched_at": None}
_things_cache = {"payload": None, "fetched_at": None}


# --------------------------------------------------------------------------- #
# Configuration                                                                 #
# --------------------------------------------------------------------------- #


def load_config():
    """Load ``config.yaml`` merged over built-in defaults, memoized for the process.

    User config is shallow-merged over ``defaults``; a few nested dicts
    (``balance_target``, ``operations``, ``growth``, ...) are merged one level
    deeper so a user can override a single key without redefining the whole
    block. ``operations.categories`` is replaced wholesale when provided, since
    merging a list of category objects by position would be surprising.
    """
    global _config
    if _config is not None:
        return _config

    defaults = {
        "goal_hours": 10_000,
        "target_date": "2030-12-31",
        "calendars": ["courses", "research"],
        "balance_target": {"courses_percent": 70, "research_percent": 30},
        "milestones": [100, 500, 1000, 2500, 5000, 7500, 10000],
        "cache_ttl_minutes": 10,
        "focus_tags": {
            "deep_work_prefixes": ["[deep]", "deep:"],
            "light_review_prefixes": ["[review]", "review:"],
        },
        "chart_buckets": {
            "daily": 30,
            "weekly": 16,
            "monthly": 12,
            "yearly": 5,
            "heatmap_days": 365,
        },
        "history_years": 30,
        "event_breakdown_limit": 25,
        "operations": {
            "calendar": "operations",
            "sleep_timezone": "Asia/Tehran",
            "categories": [
                {
                    "id": "rest",
                    "label": "Sleep & rest",
                    "keywords": ["sleep", "sleeping", "nap", "rest", "bedtime"],
                },
                {
                    "id": "transport",
                    "label": "Transport",
                    "keywords": ["go"],
                },
                {
                    "id": "food",
                    "label": "Food",
                    "whole_word_keywords": ["breakfast", "lunch", "dinner", "food", "eat", "eating", "meal", "meals", "snack", "snacks", "brunch", "supper"],
                },
                {
                    "id": "daily_life",
                    "label": "Daily life",
                    "fallback": True,
                },
            ],
        },
        "growth": {
            "calendar": "growth",
            "workout_keywords": ["gym", "workout", "boxing"],
            "weekly_goal_min": 8,
            "weekly_goal_max": 12,
        },
        "work": {"calendar": "work"},
        "fun": {
            "calendar": "fun",
            "weekly_goal_min": 5,
            "weekly_goal_max": 15,
        },
        "fucked": {
            "calendar": "fucked",
            "weekly_cap": 7,
            "daily_cap": 1.5,
        },
        "things": {
            "enabled": True,
            "db_path": None,
            "areas": [],
            "tags": [],
            "today_limit": 20,
            "upcoming_limit": 10,
            "upcoming_days": 7,
            "completed_days": 30,
            "recent_completed_limit": 10,
        },
    }

    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        merged = {**defaults, **user_cfg}
        merged["balance_target"] = {**defaults["balance_target"], **(user_cfg.get("balance_target") or {})}
        merged["focus_tags"] = {**defaults["focus_tags"], **(user_cfg.get("focus_tags") or {})}
        merged["chart_buckets"] = {**defaults["chart_buckets"], **(user_cfg.get("chart_buckets") or {})}
        merged["operations"] = {**defaults["operations"], **(user_cfg.get("operations") or {})}
        if user_cfg.get("operations", {}).get("categories"):
            merged["operations"]["categories"] = user_cfg["operations"]["categories"]
        merged["growth"] = {**defaults["growth"], **(user_cfg.get("growth") or {})}
        merged["work"] = {**defaults["work"], **(user_cfg.get("work") or {})}
        merged["fun"] = {**defaults["fun"], **(user_cfg.get("fun") or {})}
        merged["fucked"] = {**defaults["fucked"], **(user_cfg.get("fucked") or {})}
        merged["things"] = {**defaults["things"], **(user_cfg.get("things") or {})}
        _config = merged
    else:
        _config = defaults

    return _config


def cfg(key=None):
    """Shorthand config accessor: ``cfg()`` for the whole dict, ``cfg("key")`` for one entry."""
    config = load_config()
    return config if key is None else config[key]


# --------------------------------------------------------------------------- #
# Google Calendar auth & fetching                                              #
# --------------------------------------------------------------------------- #


def get_credentials():
    """Return valid OAuth credentials, refreshing or running the consent flow as needed.

    Loads ``token.json`` if present; refreshes it when expired; otherwise opens
    a one-time local-server browser consent flow against ``credentials.json``.
    The (possibly new) token is always written back to ``token.json``.
    """
    creds = None
    token_path = os.path.join(BASE_DIR, "token.json")
    credentials_path = os.path.join(BASE_DIR, "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                # Refresh token was revoked or expired (e.g. a "Testing" OAuth
                # app expires them after 7 days). Drop it and re-run consent.
                creds = None
        if not refreshed:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as token:
            token.write(creds.to_json())

    return creds


def get_calendar_service():
    return build("calendar", "v3", credentials=get_credentials())


def find_calendar_id(service, name):
    """Resolve a calendar's API id by its display name (case-insensitive, paginated)."""
    page_token = None
    while True:
        result = service.calendarList().list(pageToken=page_token).execute()
        for item in result.get("items", []):
            if item.get("summary", "").lower() == name.lower():
                return item["id"]
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return None


def utc_now():
    return datetime.now(timezone.utc)


def round_hours(value):
    return round(value, 2)


# --------------------------------------------------------------------------- #
# Event parsing                                                                 #
# --------------------------------------------------------------------------- #
#
# Every calendar event is normalized into a flat "parsed event" dict that the
# rest of the module operates on:
#
#     {
#         "start":    datetime (tz-aware, UTC),
#         "hours":    float,        # logged duration, capped at `now`
#         "title":    str,
#         "calendar": str,          # source calendar name
#         "focus":    str,          # "deep_work" | "light_review" | "other"
#         "manual":   bool,         # present only for adjustments.json entries
#     }


def parse_event(event, now, calendar_name):
    """Normalize one Google Calendar event into a parsed-event dict, or ``None`` to skip it.

    Skips cancelled events and all-day events (which lack a ``dateTime``). Only
    elapsed time counts: an in-progress event is credited up to ``now``, and
    events that start in the future contribute nothing. Returns ``None`` for any
    event that yields zero countable hours.
    """
    if event.get("status") == "cancelled":
        return None

    start = event.get("start", {})
    end = event.get("end", {})
    if "dateTime" not in start or "dateTime" not in end:
        return None

    start_dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
    end_dt = datetime.fromisoformat(end["dateTime"].replace("Z", "+00:00")).astimezone(timezone.utc)

    if start_dt >= now:
        return None

    elapsed_end = min(end_dt, now)
    hours = max(0.0, (elapsed_end - start_dt).total_seconds() / 3600)
    if hours <= 0:
        return None

    title = event.get("summary", "Untitled").strip()
    return {
        "start": start_dt,
        "hours": hours,
        "end": elapsed_end,
        "in_progress": end_dt > now,
        "title": title,
        "calendar": calendar_name,
        "focus": classify_focus(title),
    }


def classify_focus(title):
    """Tag an event as ``deep_work`` / ``light_review`` / ``other`` by title prefix.

    Prefixes are configurable under ``focus_tags`` in config.yaml (e.g. ``[deep]``,
    ``review:``). Used to surface session-quality metrics in the education tab.
    """
    lower = title.lower()
    for prefix in cfg("focus_tags")["deep_work_prefixes"]:
        if lower.startswith(prefix.lower()):
            return "deep_work"
    for prefix in cfg("focus_tags")["light_review_prefixes"]:
        if lower.startswith(prefix.lower()):
            return "light_review"
    return "other"


def normalize_title(title):
    """Collapse a session title to its topic/project for grouping in the breakdown.

    Strips a trailing detail after common separators (" — ", " - ", " | ", ": ")
    and any "Week N ..." suffix, so "Calc — Week 3: limits" and "Calc — Week 4"
    both group under "Calc".
    """
    text = title.strip()
    for sep in (" — ", " - ", " | ", ": "):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
    text = re.sub(r"\s*[Ww]eek\s+\d+.*", "", text).strip()
    return text or title.strip() or "Untitled"


def fetch_past_events(service, calendar_id, calendar_name):
    """Fetch and parse all past timed events for one calendar (paginated, ``history_years`` back)."""
    now = utc_now()
    time_min = (now - timedelta(days=365 * cfg("history_years"))).isoformat()
    time_max = now.isoformat()
    events = []
    page_token = None

    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
                maxResults=2500,
            )
            .execute()
        )

        for event in result.get("items", []):
            parsed = parse_event(event, now, calendar_name)
            if parsed:
                events.append(parsed)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


def load_manual_adjustments():
    """Load hand-entered hours from ``adjustments.json`` as synthetic parsed events.

    Lets you backfill time that predates the calendar (or was tracked elsewhere)
    without inventing fake calendar entries. Each entry carries ``"manual": True``
    so it can be reported separately. Malformed entries are skipped silently.
    """
    if not os.path.exists(ADJUSTMENTS_PATH):
        return []

    with open(ADJUSTMENTS_PATH, encoding="utf-8") as f:
        data = json.load(f)

    entries = []
    for item in data.get("entries", []):
        try:
            date_str = item["date"]
            hours = float(item["hours"])
            if hours == 0:
                continue
            calendar_name = item.get("calendar", "courses")
            note = item.get("note", "Manual adjustment")
            start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            entries.append(
                {
                    "start": start_dt,
                    "hours": abs(hours),
                    "title": note,
                    "calendar": calendar_name,
                    "focus": "other",
                    "manual": True,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    return entries


def split_events_by_calendar(all_events):
    calendar_names = cfg("calendars")
    buckets = {name: [] for name in calendar_names}
    for event in all_events:
        name = event.get("calendar")
        if name in buckets:
            buckets[name].append(event)
    return buckets


# --------------------------------------------------------------------------- #
# Time-bucketing & aggregation helpers                                          #
# --------------------------------------------------------------------------- #


def bucket_key(dt, period):
    """Map a datetime to a stable bucket key for the given period (day/week/month/year)."""
    if period == "day":
        return dt.strftime("%Y-%m-%d")
    if period == "week":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "month":
        return dt.strftime("%Y-%m")
    if period == "year":
        return dt.strftime("%Y")
    raise ValueError(f"Unknown period: {period}")


def format_bucket_label(key, period):
    if period == "day":
        dt = datetime.strptime(key, "%Y-%m-%d")
        return dt.strftime("%b %d")
    if period == "week":
        year, week = key.split("-W")
        return f"W{int(week)} '{year[2:]}"
    if period == "month":
        dt = datetime.strptime(key, "%Y-%m")
        return dt.strftime("%b %Y")
    if period == "year":
        return key
    return key


def generate_period_range(period, count, anchor=None):
    """Return the last ``count`` bucket keys up to ``anchor`` (today), oldest first.

    This fixes the x-axis of every time chart so empty buckets render as gaps
    instead of being dropped. Because all domains anchor on ``utc_now()``, their
    daily ranges line up key-for-key (the frontend relies on this to correlate
    sleep vs study, for example).
    """
    anchor = anchor or utc_now()
    keys = []

    if period == "day":
        for i in range(count - 1, -1, -1):
            keys.append(bucket_key(anchor - timedelta(days=i), period))
    elif period == "week":
        anchor_monday = anchor - timedelta(days=anchor.weekday())
        for i in range(count - 1, -1, -1):
            keys.append(bucket_key(anchor_monday - timedelta(weeks=i), period))
    elif period == "month":
        year, month = anchor.year, anchor.month
        for i in range(count - 1, -1, -1):
            m = month - i
            y = year
            while m <= 0:
                m += 12
                y -= 1
            keys.append(f"{y}-{m:02d}")
    elif period == "year":
        for i in range(count - 1, -1, -1):
            keys.append(str(anchor.year - i))

    return keys


def daily_hours_map(all_events):
    totals = defaultdict(float)
    for event in all_events:
        key = bucket_key(event["start"], "day")
        totals[key] += event["hours"]
    return totals


def aggregate_by_period(event_groups, period, bucket_count, anchor=None):
    """Sum hours per calendar over a fixed window of period buckets, for charting.

    ``event_groups`` maps a series name (calendar or category id) to its events.
    Returns ``{"labels", "keys", <name>: [...], "total": [...]}`` where every list
    is aligned to the same ``keys`` from :func:`generate_period_range`.
    """
    keys = generate_period_range(period, bucket_count, anchor=anchor)
    series = {name: {k: 0.0 for k in keys} for name in event_groups}

    for name, events in event_groups.items():
        for event in events:
            key = bucket_key(event["start"], period)
            if key in series[name]:
                series[name][key] += event["hours"]

    calendar_names = list(event_groups.keys())
    result_series = {}
    for name in calendar_names:
        result_series[name] = [round_hours(series[name][k]) for k in keys]

    totals = [
        round_hours(sum(result_series[name][i] for name in calendar_names))
        for i in range(len(keys))
    ]

    return {
        "labels": [format_bucket_label(k, period) for k in keys],
        "keys": keys,
        **result_series,
        "total": totals,
    }


def aggregate_day_of_week(all_events):
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    totals = [0.0] * 7
    for event in all_events:
        totals[event["start"].weekday()] += event["hours"]
    return {
        "labels": day_names,
        "total": [round_hours(h) for h in totals],
    }


def allocation_history(events):
    """Sparse UTC daily totals across fetched history for calendar-period navigation."""
    return {key: round_hours(hours) for key, hours in sorted(daily_hours_map(events).items())}


def recorded_history_days(events):
    """UTC calendar days from the first recorded event through today, inclusive."""
    if not events:
        return 0
    first_date = min(event["start"].date() for event in events)
    return max(1, (utc_now().date() - first_date).days + 1)


def rolling_totals(all_events, days):
    """Total and per-day-average hours over the trailing ``days`` window."""
    cutoff = utc_now() - timedelta(days=days)
    filtered = [e for e in all_events if e["start"] >= cutoff]
    total = sum(e["hours"] for e in filtered)
    return {
        "total": round_hours(total),
        "avg_per_day": round_hours(total / days) if days else 0,
    }


def rolling_totals_by_calendar(event_groups, days):
    cutoff = utc_now() - timedelta(days=days)
    result = {}
    for name, events in event_groups.items():
        hours = sum(e["hours"] for e in events if e["start"] >= cutoff)
        result[name] = round_hours(hours)
    result["total"] = round_hours(sum(result.values()))
    result["avg_per_day"] = round_hours(result["total"] / days) if days else 0
    return result


def compare_periods(all_events, days):
    """Compare the trailing ``days`` window against the one immediately before it.

    Returns current/previous totals plus a percent change and direction, used for
    the "vs prior period" deltas on the rolling-window cards.
    """
    now = utc_now()
    current_cutoff = now - timedelta(days=days)
    previous_cutoff = now - timedelta(days=days * 2)

    current = sum(e["hours"] for e in all_events if e["start"] >= current_cutoff)
    previous = sum(
        e["hours"] for e in all_events if previous_cutoff <= e["start"] < current_cutoff
    )

    if previous == 0:
        change_pct = 100.0 if current > 0 else 0.0
    else:
        change_pct = round((current - previous) / previous * 100, 1)

    return {
        "current": round_hours(current),
        "previous": round_hours(previous),
        "change_pct": change_pct,
        "direction": "up" if current > previous else ("down" if current < previous else "flat"),
    }


# --------------------------------------------------------------------------- #
# Analytics computations (education domain)                                      #
# --------------------------------------------------------------------------- #


def compute_streaks(daily_map):
    """Current and longest run of consecutive "active" days, plus active days this month.

    A day counts as active when it logs >= 1.0 h. The current streak counts back
    from today (or yesterday, so a not-yet-worked today doesn't break it). Drives
    the streak cards and the frontend XP-multiplier badge.
    """
    if not daily_map:
        return {"current": 0, "longest": 0, "days_active_this_month": 0}

    active_days = sorted(
        datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        for d, h in daily_map.items()
        if h >= 1.0
    )

    if not active_days:
        return {"current": 0, "longest": 0, "days_active_this_month": 0}

    today = utc_now().date()
    yesterday = today - timedelta(days=1)
    active_dates = {d.date() for d in active_days}

    current = 0
    cursor = today if today in active_dates else yesterday
    if cursor not in active_dates:
        current = 0
    else:
        while cursor in active_dates:
            current += 1
            cursor -= timedelta(days=1)

    longest = 0
    streak = 0
    prev = None
    for day in active_days:
        if prev and (day.date() - prev.date()).days == 1:
            streak += 1
        else:
            streak = 1
        longest = max(longest, streak)
        prev = day

    month_prefix = utc_now().strftime("%Y-%m")
    days_active_this_month = sum(
        1 for d, h in daily_map.items() if d.startswith(month_prefix) and h >= 1.0
    )

    return {
        "current": current,
        "longest": longest,
        "days_active_this_month": days_active_this_month,
    }


def compute_milestones(total_hours):
    """Progress toward the next configured milestone (config ``milestones``).

    Returns crossed milestones, the next target, and percent progress within the
    current milestone band. Note these are the *config* milestones; the RPG
    "boss battle" markers (every 250 h) are a separate, frontend-only concept.
    """
    milestones = sorted(cfg("milestones"))
    crossed = [m for m in milestones if total_hours >= m]
    next_milestone = next((m for m in milestones if m > total_hours), None)

    if next_milestone is None:
        return {
            "crossed": crossed,
            "next": None,
            "next_label": "Goal reached",
            "progress_pct": 100.0,
            "hours_to_next": 0,
            "range_start": crossed[-1] if crossed else 0,
        }

    prev = crossed[-1] if crossed else 0
    span = next_milestone - prev
    progress = (total_hours - prev) / span * 100 if span else 0

    return {
        "crossed": crossed,
        "next": next_milestone,
        "next_label": f"{next_milestone:,} h",
        "progress_pct": round(min(progress, 100), 1),
        "hours_to_next": round_hours(next_milestone - total_hours),
        "range_start": prev,
    }


def compute_pace(total_hours, hours_remaining, all_events):
    """Projected pace and ETAs from recent throughput.

    Computes hours/week and hours/month at both the 30- and 90-day rolling rates,
    an ETA for reaching the goal at each rate, and the hours/week required to hit
    the configured ``target_date``. ``hours_per_week_30d`` here is what the
    frontend "Path to Mastery" uses as the rolling-30-day weekly average.
    """
    roll30 = rolling_totals(all_events, 30)
    roll90 = rolling_totals(all_events, 90)

    hours_per_day_30 = roll30["avg_per_day"]
    hours_per_day_90 = roll90["avg_per_day"]
    hours_per_week_30 = round_hours(hours_per_day_30 * 7)
    hours_per_week_90 = round_hours(hours_per_day_90 * 7)
    hours_per_month_30 = round_hours(hours_per_day_30 * 30)
    hours_per_month_90 = round_hours(hours_per_day_90 * 30)

    def eta_from_daily_rate(daily_rate):
        if daily_rate <= 0 or hours_remaining <= 0:
            return None
        days_left = hours_remaining / daily_rate
        return (utc_now() + timedelta(days=days_left)).strftime("%Y-%m-%d")

    target_date_str = cfg("target_date")
    try:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        target_date = None

    hours_per_week_needed = None
    weeks_until_target = None
    if target_date and target_date > utc_now():
        weeks_until_target = max(1, (target_date - utc_now()).days / 7)
        hours_per_week_needed = round_hours(hours_remaining / weeks_until_target)

    return {
        "hours_per_week_30d": hours_per_week_30,
        "hours_per_week_90d": hours_per_week_90,
        "hours_per_month_30d": hours_per_month_30,
        "hours_per_month_90d": hours_per_month_90,
        "eta_30d_pace": eta_from_daily_rate(hours_per_day_30),
        "eta_90d_pace": eta_from_daily_rate(hours_per_day_90),
        "target_date": target_date_str,
        "hours_per_week_needed": hours_per_week_needed,
        "weeks_until_target": round(weeks_until_target, 1) if weeks_until_target else None,
    }


def this_week_summary(all_events, pace):
    """Hours so far this ISO week and an on-track / behind status vs the needed pace.

    Status projects the current daily average out to a full week and compares it
    to ``pace["hours_per_week_needed"]`` (>=95% on track, >=70% slightly behind).
    """
    now = utc_now()
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    week_hours = sum(e["hours"] for e in all_events if e["start"] >= week_start)
    days_elapsed = max(1, (now - week_start).days + 1)
    avg_per_day = week_hours / days_elapsed

    needed = pace.get("hours_per_week_needed")
    status = "no pace data"
    if needed is not None:
        projected_week = avg_per_day * 7
        if projected_week >= needed * 0.95:
            status = "on track"
        elif projected_week >= needed * 0.7:
            status = "slightly behind"
        else:
            status = "behind pace"

    return {
        "hours": round_hours(week_hours),
        "avg_per_day": round_hours(avg_per_day),
        "status": status,
        "days_elapsed": days_elapsed,
    }


def compute_balance(event_groups, days):
    """Actual vs target courses/research split over a trailing window, with deltas.

    Targets come from ``balance_target`` in config. (The frontend "Drift Gauge"
    is a separate view that hard-codes an 80/20 target over *all-time* hours.)
    """
    target = cfg("balance_target")
    cutoff = utc_now() - timedelta(days=days)
    hours_by_cal = {}
    for name, events in event_groups.items():
        hours_by_cal[name] = sum(e["hours"] for e in events if e["start"] >= cutoff)

    total = sum(hours_by_cal.values()) or 1
    actual = {name: round_hours(h / total * 100) for name, h in hours_by_cal.items()}

    calendar_names = cfg("calendars")
    target_courses = target.get("courses_percent", 70)
    target_research = target.get("research_percent", 30)

    actual_courses = actual.get(calendar_names[0], 0) if calendar_names else 0
    actual_research = actual.get(calendar_names[1], 0) if len(calendar_names) > 1 else 0

    return {
        "days": days,
        "target_courses_pct": target_courses,
        "target_research_pct": target_research,
        "actual_courses_pct": actual_courses,
        "actual_research_pct": actual_research,
        "courses_delta": round(actual_courses - target_courses, 1),
        "research_delta": round(actual_research - target_research, 1),
    }


def compute_focus_metrics(all_events):
    if not all_events:
        return {
            "deep_work_hours": 0,
            "light_review_hours": 0,
            "other_hours": 0,
            "avg_session_hours": 0,
            "weekend_pct": 0,
            "session_count": 0,
        }

    by_focus = {"deep_work": 0.0, "light_review": 0.0, "other": 0.0}
    weekend_hours = 0.0
    total = 0.0

    for event in all_events:
        by_focus[event["focus"]] += event["hours"]
        total += event["hours"]
        if event["start"].weekday() >= 5:
            weekend_hours += event["hours"]

    return {
        "deep_work_hours": round_hours(by_focus["deep_work"]),
        "light_review_hours": round_hours(by_focus["light_review"]),
        "other_hours": round_hours(by_focus["other"]),
        "avg_session_hours": round_hours(total / len(all_events)),
        "weekend_pct": round(weekend_hours / total * 100, 1) if total else 0,
        "session_count": len(all_events),
    }


def event_breakdown(all_events, limit):
    """Top ``limit`` topics/projects by total hours (grouped via :func:`normalize_title`).

    Each row has title, summed hours, session count, source calendar, and last
    session date. The frontend's research "Quest Log" is built from these rows,
    filtered to the research calendar.
    """
    groups = {}
    for event in all_events:
        key = normalize_title(event["title"])
        if key not in groups:
            groups[key] = {
                "title": key,
                "hours": 0.0,
                "last_session": event["start"],
                "calendar": event["calendar"],
                "sessions": 0,
            }
        groups[key]["hours"] += event["hours"]
        groups[key]["sessions"] += 1
        if event["start"] > groups[key]["last_session"]:
            groups[key]["last_session"] = event["start"]

    ranked = sorted(groups.values(), key=lambda x: x["hours"], reverse=True)[:limit]
    for item in ranked:
        item["hours"] = round_hours(item["hours"])
        item["last_session"] = item["last_session"].strftime("%Y-%m-%d")

    return ranked


def build_heatmap(daily_map, days):
    """Build a GitHub-style activity heatmap: one cell per day with a 0-4 intensity level.

    Levels are relative to the busiest day in the window (quartiles of max hours),
    so the scale auto-adjusts to your typical volume.
    """
    now = utc_now()
    cells = []
    max_hours = 0.0

    for i in range(days - 1, -1, -1):
        dt = now - timedelta(days=i)
        key = bucket_key(dt, "day")
        hours = round_hours(daily_map.get(key, 0))
        max_hours = max(max_hours, hours)
        cells.append({"date": key, "hours": hours, "weekday": dt.weekday()})

    for cell in cells:
        h = cell["hours"]
        if h <= 0:
            cell["level"] = 0
        elif max_hours <= 0:
            cell["level"] = 0
        else:
            ratio = h / max_hours
            if ratio <= 0.25:
                cell["level"] = 1
            elif ratio <= 0.5:
                cell["level"] = 2
            elif ratio <= 0.75:
                cell["level"] = 3
            else:
                cell["level"] = 4

    return {"days": days, "cells": cells, "max_hours": round_hours(max_hours)}


def cumulative_series(event_groups, period, bucket_count):
    """Running cumulative total across the period window (for the progress-over-time line)."""
    buckets = aggregate_by_period(event_groups, period, bucket_count)
    cumulative = []
    running = 0.0
    for total in buckets["total"]:
        running += total
        cumulative.append(round_hours(running))
    return {"labels": buckets["labels"], "cumulative": cumulative}


# --------------------------------------------------------------------------- #
# Local persistence & in-memory cache                                           #
# --------------------------------------------------------------------------- #


def init_db():
    """Create the SQLite cache tables (``daily_totals`` history, ``snapshots`` log) if absent."""
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_totals (
            date TEXT PRIMARY KEY,
            courses REAL NOT NULL DEFAULT 0,
            research REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def persist_snapshot(payload, event_groups):
    """Append the analytics payload to ``snapshots`` and upsert per-day totals.

    Gives the dashboard a durable history independent of Google Calendar (e.g.
    for trend backfill), refreshed on every live analytics fetch.
    """
    init_db()
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute(
        "INSERT INTO snapshots (fetched_at, payload) VALUES (?, ?)",
        (payload["meta"]["fetched_at"], json.dumps(payload)),
    )

    calendar_names = cfg("calendars")
    daily = defaultdict(lambda: {name: 0.0 for name in calendar_names})
    for name, events in event_groups.items():
        for event in events:
            key = bucket_key(event["start"], "day")
            daily[key][name] += event["hours"]

    for date_key, hours in daily.items():
        total = sum(hours.values())
        conn.execute(
            """
            INSERT INTO daily_totals (date, courses, research, total)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                courses = excluded.courses,
                research = excluded.research,
                total = excluded.total
            """,
            (
                date_key,
                hours.get(calendar_names[0], 0),
                hours.get(calendar_names[1], 0) if len(calendar_names) > 1 else 0,
                total,
            ),
        )

    conn.commit()
    conn.close()


def cache_valid(cache, force=False):
    """True if a cache slot holds a payload still within ``cache_ttl_minutes``.

    ``force=True`` (from ``?refresh=1``) always misses so the data is refetched.
    """
    if force:
        return False
    fetched_at = cache.get("fetched_at")
    if not fetched_at or not cache.get("payload"):
        return False
    ttl = timedelta(minutes=cfg("cache_ttl_minutes"))
    return utc_now() - fetched_at < ttl


# --------------------------------------------------------------------------- #
# Growth & Operations domains                                                   #
# --------------------------------------------------------------------------- #


def is_workout_event(event, keywords):
    """True if the event title contains any configured workout keyword (e.g. gym)."""
    title = event.get("title", "").lower()
    return any(keyword.lower() in title for keyword in keywords)


def filter_workout_events(events, keywords):
    return [event for event in events if is_workout_event(event, keywords)]


def current_week_start():
    now = utc_now()
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def events_since(events, start_dt):
    return [event for event in events if event["start"] >= start_dt]


def count_sessions(events):
    return len(events)


def workout_weekly_goal_status(hours, goal_min, goal_max):
    if hours < goal_min:
        return "below"
    if hours <= goal_max:
        return "on_target"
    return "above"


def operations_categories():
    return cfg("operations")["categories"]


def keyword_in_title(keyword, lower_title):
    """Substring match, but require a whole-word match for short (<=4 char) keywords.

    Prevents false positives like "nap" matching "snapp" or "gym" matching a
    longer word, while still allowing loose matching for longer keywords.
    """
    keyword = keyword.lower()
    if len(keyword) <= 4:
        return bool(re.search(r"\b" + re.escape(keyword) + r"\b", lower_title))
    return keyword in lower_title


def classify_operations_event(event, categories=None):
    """Assign an operations event to the first matching category, or ``None``.

    Each category (config ``operations.categories``) can match by exact title,
    title prefix, or keyword; they are tested in config order and first match
    wins. A fallback category receives every unmatched event. The "rest"
    category is what the frontend treats as sleep hours.
    """
    categories = categories or operations_categories()
    lower = event.get("title", "").strip().lower()

    for category in categories:
        cat_id = category["id"]

        exact = [word.lower() for word in category.get("exact_keywords", [])]
        if exact and lower in exact:
            return cat_id

        prefix = (category.get("title_prefix") or "").lower().strip()
        if prefix and lower.startswith(prefix):
            return cat_id

        whole_words = category.get("whole_word_keywords", [])
        if any(re.search(r"\b" + re.escape(word.lower()) + r"\b", lower) for word in whole_words):
            return cat_id

        keywords = [word.lower() for word in category.get("keywords", [])]
        if keywords and any(keyword_in_title(keyword, lower) for keyword in keywords):
            return cat_id

    return next((category["id"] for category in categories if category.get("fallback")), None)


def split_operations_events(events, categories=None):
    categories = categories or operations_categories()
    groups = {category["id"]: [] for category in categories}
    for event in events:
        cat_id = classify_operations_event(event, categories)
        if cat_id:
            groups[cat_id].append(event)
    return groups


def category_hours_summary(event_groups, categories=None):
    categories = categories or operations_categories()
    summary = {}
    for category in categories:
        cat_id = category["id"]
        hours = round_hours(sum(event["hours"] for event in event_groups.get(cat_id, [])))
        summary[cat_id] = {
            "id": cat_id,
            "label": category["label"],
            "hours": hours,
        }
    total = round_hours(sum(item["hours"] for item in summary.values()))
    for item in summary.values():
        item["percent"] = round(item["hours"] / total * 100, 1) if total else 0
    return summary, total


def fetch_operations_calendar_events(service):
    operations_cfg = cfg("operations")
    calendar_name = operations_cfg["calendar"]
    calendar_id = find_calendar_id(service, calendar_name)
    if not calendar_id:
        raise ValueError(f"Calendar not found: {calendar_name}")

    events = fetch_past_events(service, calendar_id, calendar_name)
    for adj in load_manual_adjustments():
        if adj["calendar"] == calendar_name:
            events.append(adj)
    return events


def is_timed_sleep(event):
    keywords = cfg("operations").get("sleep_keywords", ["sleep", "sleeping", "nap", "bedtime"])
    return not event.get("manual") and any(
        keyword_in_title(word, event.get("title", "").lower()) for word in keywords
    )


def sleep_end(event):
    return event.get("end") or event["start"] + timedelta(hours=event["hours"])


def sleep_day(event):
    """Local wake-up date; ongoing sessions provisionally use their elapsed end."""
    return sleep_end(event).astimezone(ZoneInfo(cfg("operations")["sleep_timezone"])).date()


def operations_history_events(events):
    """Date-only copies for charts. Never move original event timestamps or hours."""
    return [
        {**event, "start": datetime.combine(sleep_day(event), datetime.min.time(), tzinfo=timezone.utc)}
        if is_timed_sleep(event) else event
        for event in events
    ]


def sleep_sessions(events):
    """Timed sleep/naps only; generic rest and manual adjustments keep their dates."""
    sessions = []
    for event in events:
        if not is_timed_sleep(event):
            continue
        sessions.append({
            "start": event["start"].isoformat(), "end": sleep_end(event).isoformat(),
            "sleep_date": sleep_day(event).isoformat(),
            "hours": round_hours(event["hours"]), "title": event.get("title", "Sleep"),
            "in_progress": event.get("in_progress", False),
        })
    return sorted(sessions, key=lambda session: session["start"])


def build_operations_payload(operations_events):
    """Assemble the ``/api/operations`` payload: per-category totals, weekly split, charts.

    Includes all-time and last-30-day category totals, this-week breakdown,
    30/90-day weekly averages, and daily/weekly stacked-chart series. The daily
    series' ``rest`` array is consumed by the sleep-vs-study correlation view.
    """
    categories = operations_categories()
    buckets_cfg = cfg("chart_buckets")
    category_groups = split_operations_events(operations_events, categories)
    categorized_events = [event for events in category_groups.values() for event in events]
    history_groups = {
        key: operations_history_events(events) if key == "rest" else events
        for key, events in category_groups.items()
    }
    history_events = [event for events in history_groups.values() for event in events]
    # Include the current local wake date even when it is already tomorrow in UTC.
    local_today = utc_now().astimezone(ZoneInfo(cfg("operations")["sleep_timezone"])).date()
    history_anchor = max(utc_now(), datetime.combine(local_today, datetime.min.time(), tzinfo=timezone.utc))


    week_start = current_week_start()
    week_groups = {
        cat_id: events_since(events, week_start)
        for cat_id, events in category_groups.items()
    }

    roll_7 = rolling_totals(categorized_events, 7)
    roll_30 = rolling_totals(categorized_events, 30)
    roll_90 = rolling_totals(categorized_events, 90)

    totals, total_hours = category_hours_summary(category_groups, categories)
    week_totals, week_total_hours = category_hours_summary(week_groups, categories)
    roll_30_groups = {
        cat_id: events_since(events, utc_now() - timedelta(days=30))
        for cat_id, events in category_groups.items()
    }
    month_totals, _ = category_hours_summary(roll_30_groups, categories)

    averages = {}
    for category in categories:
        cat_id = category["id"]
        cat_events = category_groups[cat_id]
        roll_30_cat = rolling_totals(cat_events, 30)
        roll_90_cat = rolling_totals(cat_events, 90)
        averages[cat_id] = {
            "hours_per_week_30d": round_hours(roll_30_cat["total"] / (30 / 7)) if roll_30_cat["total"] else 0,
            "hours_per_week_90d": round_hours(roll_90_cat["total"] / (90 / 7)) if roll_90_cat["total"] else 0,
        }

    return {
        "sleep_sessions": sleep_sessions(category_groups.get("rest", [])),
        "segments": {
            category["id"]: {
                "allocation_history": allocation_history(history_groups[category["id"]]),
                "history_days": recorded_history_days(history_groups[category["id"]]),
                "total_hours": totals[category["id"]]["hours"],
                "this_week": {"hours": week_totals[category["id"]]["hours"]},
                "rolling": {
                    f"last_{days}_days": rolling_totals(category_groups[category["id"]], days)
                    for days in (1, 7, 30, 90, 365)
                },
                "timeframes": {
                    "daily": aggregate_by_period({category["id"]: history_groups[category["id"]]}, "day", buckets_cfg["daily"], anchor=history_anchor),
                    "weekly": aggregate_by_period({category["id"]: history_groups[category["id"]]}, "week", buckets_cfg["weekly"], anchor=history_anchor),
                },
            }
            for category in categories
        },
        "allocation_history": allocation_history(history_events),
        "history_days": recorded_history_days(history_events),
        "total_hours": total_hours,
        "this_week": {
            "total_hours": week_total_hours,
            "categories": week_totals,
        },
        "totals": totals,
        "last_30_days": month_totals,
        "averages": averages,
        "timeframes": {
            "daily": aggregate_by_period(history_groups, "day", buckets_cfg["daily"], anchor=history_anchor),
            "weekly": aggregate_by_period(history_groups, "week", buckets_cfg["weekly"], anchor=history_anchor),
        },
        "rolling": {
            "last_1_days": rolling_totals(categorized_events, 1),
            "last_365_days": rolling_totals(categorized_events, 365),
            "last_7_days": roll_7,
            "last_30_days": roll_30,
            "last_90_days": roll_90,
        },
        "config": {
            "calendar": cfg("operations")["calendar"],
            "sleep_timezone": cfg("operations")["sleep_timezone"],
            "categories": [
                {"id": c["id"], "label": c["label"]} for c in categories
            ],
        },
    }


def get_operations_analytics(force_refresh=False):
    if cache_valid(_operations_cache, force_refresh):
        payload = _operations_cache["payload"]
        payload["meta"]["from_cache"] = True
        return payload

    service = get_calendar_service()
    operations_events = fetch_operations_calendar_events(service)
    payload = build_operations_payload(operations_events)
    fetched_at = utc_now()
    payload["meta"] = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
    }
    _operations_cache["payload"] = payload
    _operations_cache["fetched_at"] = fetched_at
    return payload


def fetch_growth_calendar_events(service):
    growth_cfg = cfg("growth")
    calendar_name = growth_cfg["calendar"]
    calendar_id = find_calendar_id(service, calendar_name)
    if not calendar_id:
        raise ValueError(f"Calendar not found: {calendar_name}")

    events = fetch_past_events(service, calendar_id, calendar_name)
    for adj in load_manual_adjustments():
        if adj["calendar"] == calendar_name:
            events.append(adj)
    return events


def build_growth_payload(growth_events):
    """Assemble the ``/api/growth`` payload from workout events in the growth calendar.

    Reports this-week hours/sessions and a goal status (below / on_target / above
    relative to ``weekly_goal_min``..``weekly_goal_max``), rolling averages, and
    chart series. The frontend "Stamina Fuel" bar keys off this week's hours vs
    ``weekly_goal_min``.
    """
    growth_cfg = cfg("growth")
    keywords = growth_cfg["workout_keywords"]
    goal_min = growth_cfg["weekly_goal_min"]
    goal_max = growth_cfg["weekly_goal_max"]
    buckets_cfg = cfg("chart_buckets")

    workout_events = filter_workout_events(growth_events, keywords)
    workout_groups = {"workout": workout_events}
    week_start = current_week_start()
    week_events = events_since(workout_events, week_start)
    week_hours = round_hours(sum(event["hours"] for event in week_events))

    roll_30 = rolling_totals(workout_events, 30)
    roll_90 = rolling_totals(workout_events, 90)
    sessions_30 = count_sessions(events_since(workout_events, utc_now() - timedelta(days=30)))

    hours_per_week_30 = round_hours(roll_30["total"] / (30 / 7)) if roll_30["total"] else 0
    hours_per_week_90 = round_hours(roll_90["total"] / (90 / 7)) if roll_90["total"] else 0
    hours_per_session_30 = round_hours(roll_30["total"] / sessions_30) if sessions_30 else 0

    return {
        "summary": {
            "allocation_history": allocation_history(growth_events),
            "history_days": recorded_history_days(growth_events),
            "total_hours": round_hours(sum(event["hours"] for event in growth_events)),
            "this_week": {"hours": round_hours(sum(event["hours"] for event in events_since(growth_events, week_start)))},
            "rolling": {f"last_{days}_days": rolling_totals(growth_events, days) for days in (1, 7, 30, 90, 365)},
            "timeframes": {
                "daily": aggregate_by_period({"growth": growth_events}, "day", buckets_cfg["daily"]),
                "weekly": aggregate_by_period({"growth": growth_events}, "week", buckets_cfg["weekly"]),
            },
            "day_of_week": aggregate_day_of_week(growth_events),
        },
        "allocation_history": allocation_history(workout_events),
        "history_days": recorded_history_days(workout_events),
        "total_hours": round_hours(sum(event["hours"] for event in workout_events)),
        "this_week": {
            "hours": week_hours,
            "sessions": count_sessions(week_events),
            "status": workout_weekly_goal_status(week_hours, goal_min, goal_max),
            "goal_min": goal_min,
            "goal_max": goal_max,
            "percent_of_max": round(min(100.0, week_hours / goal_max * 100), 1) if goal_max else 0,
        },
        "averages": {
            "hours_per_week_30d": hours_per_week_30,
            "hours_per_week_90d": hours_per_week_90,
            "hours_per_session_30d": hours_per_session_30,
            "sessions_per_week_30d": round(sessions_30 / (30 / 7), 1) if sessions_30 else 0,
        },
        "timeframes": {
            "daily": aggregate_by_period(workout_groups, "day", buckets_cfg["daily"]),
            "weekly": aggregate_by_period(workout_groups, "week", buckets_cfg["weekly"]),
        },
        "day_of_week": aggregate_day_of_week(workout_events),
        "rolling": {
            "last_1_days": rolling_totals(workout_events, 1),
            "last_365_days": rolling_totals(workout_events, 365),
            "last_7_days": rolling_totals(workout_events, 7),
            "last_30_days": roll_30,
            "last_90_days": roll_90,
        },
        "config": {
            "calendar": growth_cfg["calendar"],
            "workout_label": "Workout",
            "weekly_goal_min": goal_min,
            "weekly_goal_max": goal_max,
        },
    }


def get_growth_analytics(force_refresh=False):
    if cache_valid(_growth_cache, force_refresh):
        payload = _growth_cache["payload"]
        payload["meta"]["from_cache"] = True
        return payload

    service = get_calendar_service()
    growth_events = fetch_growth_calendar_events(service)
    payload = build_growth_payload(growth_events)
    fetched_at = utc_now()
    payload["meta"] = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
    }
    _growth_cache["payload"] = payload
    _growth_cache["fetched_at"] = fetched_at
    return payload


# --------------------------------------------------------------------------- #
# Fun & Fucked domains                                                          #
# --------------------------------------------------------------------------- #
#
# Two single-calendar domains that bookend the "wellbeing" picture:
#
#   fun      leisure/joy time, scored against a healthy *band* (like growth):
#            too little under-recharges, too much over-indexes.
#   fucked   wasted/derailed time, scored against a *cap* you stay under. Lower
#            is better, so the gamification is inverted (containment, clean
#            streaks) rather than a goal you chase upward.
#
# Both reuse the generic event helpers; only the framing differs.


def fetch_named_calendar_events(service, calendar_name):
    """Fetch parsed past events for a single named calendar, plus its manual adjustments.

    Shared by the fun/fucked domains: resolve the calendar by name, pull its past
    timed events, and fold in any ``adjustments.json`` entries tagged with that
    calendar. Raises ``ValueError`` if no calendar matches the name.
    """
    calendar_id = find_calendar_id(service, calendar_name)
    if not calendar_id:
        raise ValueError(f"Calendar not found: {calendar_name}")

    events = fetch_past_events(service, calendar_id, calendar_name)
    for adj in load_manual_adjustments():
        if adj["calendar"] == calendar_name:
            events.append(adj)
    return events


def compute_clean_streak(daily_map, daily_cap):
    """Current/longest run of consecutive "clean" days for the fucked domain.

    A day is *clean* when its wasted hours are at or below ``daily_cap`` — and a
    day with no logged waste (absent from ``daily_map``) counts as clean too. The
    current streak counts back from today; the longest is scanned only across the
    active tracking range (first logged day .. today) so untracked history doesn't
    inflate it. The mirror image of :func:`compute_streaks` (which rewards
    *active* days), since here fewer hours is the win.
    """
    if not daily_map:
        return {"current": 0, "longest": 0, "daily_cap": daily_cap}

    today = utc_now().date()
    first = min(datetime.strptime(d, "%Y-%m-%d").date() for d in daily_map)

    current = 0
    cursor = today
    while cursor >= first:
        hours = daily_map.get(cursor.strftime("%Y-%m-%d"), 0.0)
        if hours <= daily_cap:
            current += 1
            cursor -= timedelta(days=1)
        else:
            break

    longest = run = 0
    day = first
    while day <= today:
        hours = daily_map.get(day.strftime("%Y-%m-%d"), 0.0)
        if hours <= daily_cap:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
        day += timedelta(days=1)

    return {"current": current, "longest": longest, "daily_cap": daily_cap}


def build_work_payload(events):
    """Recorded work time, without inferring task completion or imposing a target."""
    groups = {"work": events}
    week_events = events_since(events, current_week_start())
    rolling = {f"last_{days}_days": rolling_totals(events, days)
               for days in (1, 7, 30, 90, 365)}
    recent = events_since(events, utc_now() - timedelta(days=30))
    sessions = count_sessions([e for e in recent if not e.get("manual")])
    return {
        "total_hours": round_hours(sum(event["hours"] for event in events)),
        "allocation_history": allocation_history(events),
        "history_days": recorded_history_days(events),
        "this_week": {"hours": round_hours(sum(e["hours"] for e in week_events)),
                      "sessions": count_sessions([e for e in week_events if not e.get("manual")])},
        "rolling": rolling,
        "averages": {
            "hours_per_week_30d": round_hours(rolling["last_30_days"]["total"] * 7 / 30),
            "hours_per_session_30d": round_hours(sum(e["hours"] for e in recent if not e.get("manual")) / sessions) if sessions else 0,
        },
        "timeframes": {name: aggregate_by_period(groups, period, cfg("chart_buckets")[name])
                       for name, period in (("daily", "day"), ("weekly", "week"), ("monthly", "month"))},
        "day_of_week": aggregate_day_of_week(events),
        "tasks": event_breakdown([e for e in events if not e.get("manual")], cfg("event_breakdown_limit")),
        "config": {"calendar": cfg("work")["calendar"]},
    }


def get_work_analytics(force_refresh=False):
    if cache_valid(_work_cache, force_refresh):
        payload = _work_cache["payload"]
        return {**payload, "meta": {**payload["meta"], "from_cache": True}}
    events = fetch_named_calendar_events(get_calendar_service(), cfg("work")["calendar"])
    payload = build_work_payload(events)
    fetched_at = utc_now()
    payload["meta"] = {"fetched_at": fetched_at.isoformat(), "from_cache": False}
    _work_cache.update(payload=payload, fetched_at=fetched_at)
    return payload


def build_fun_payload(fun_events):
    """Assemble the ``/api/fun`` payload from leisure events in the fun calendar.

    Reports this-week hours/sessions and a goal status (below / on_target / above
    relative to the ``weekly_goal_min``..``weekly_goal_max`` healthy band), rolling
    averages, day-of-week, and chart series. The frontend "Joy battery" keys off
    this week's hours vs ``weekly_goal_min`` and the "Vibe level" off total hours.
    """
    fun_cfg = cfg("fun")
    goal_min = fun_cfg["weekly_goal_min"]
    goal_max = fun_cfg["weekly_goal_max"]
    buckets_cfg = cfg("chart_buckets")

    fun_groups = {"fun": fun_events}
    week_start = current_week_start()
    week_events = events_since(fun_events, week_start)
    week_hours = round_hours(sum(event["hours"] for event in week_events))

    roll_30 = rolling_totals(fun_events, 30)
    roll_90 = rolling_totals(fun_events, 90)
    sessions_30 = count_sessions(events_since(fun_events, utc_now() - timedelta(days=30)))

    hours_per_week_30 = round_hours(roll_30["total"] / (30 / 7)) if roll_30["total"] else 0
    hours_per_week_90 = round_hours(roll_90["total"] / (90 / 7)) if roll_90["total"] else 0
    hours_per_session_30 = round_hours(roll_30["total"] / sessions_30) if sessions_30 else 0

    return {
        "allocation_history": allocation_history(fun_events),
        "history_days": recorded_history_days(fun_events),
        "total_hours": round_hours(sum(event["hours"] for event in fun_events)),
        "this_week": {
            "hours": week_hours,
            "sessions": count_sessions(week_events),
            "status": workout_weekly_goal_status(week_hours, goal_min, goal_max),
            "goal_min": goal_min,
            "goal_max": goal_max,
            "percent_of_max": round(min(100.0, week_hours / goal_max * 100), 1) if goal_max else 0,
        },
        "averages": {
            "hours_per_week_30d": hours_per_week_30,
            "hours_per_week_90d": hours_per_week_90,
            "hours_per_session_30d": hours_per_session_30,
            "sessions_per_week_30d": round(sessions_30 / (30 / 7), 1) if sessions_30 else 0,
        },
        "timeframes": {
            "daily": aggregate_by_period(fun_groups, "day", buckets_cfg["daily"]),
            "weekly": aggregate_by_period(fun_groups, "week", buckets_cfg["weekly"]),
        },
        "day_of_week": aggregate_day_of_week(fun_events),
        "rolling": {
            "last_1_days": rolling_totals(fun_events, 1),
            "last_365_days": rolling_totals(fun_events, 365),
            "last_7_days": rolling_totals(fun_events, 7),
            "last_30_days": roll_30,
            "last_90_days": roll_90,
        },
        "config": {
            "calendar": fun_cfg["calendar"],
            "weekly_goal_min": goal_min,
            "weekly_goal_max": goal_max,
        },
    }


def get_fun_analytics(force_refresh=False):
    if cache_valid(_fun_cache, force_refresh):
        payload = _fun_cache["payload"]
        payload["meta"]["from_cache"] = True
        return payload

    service = get_calendar_service()
    fun_events = fetch_named_calendar_events(service, cfg("fun")["calendar"])
    payload = build_fun_payload(fun_events)
    fetched_at = utc_now()
    payload["meta"] = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
    }
    _fun_cache["payload"] = payload
    _fun_cache["fetched_at"] = fetched_at
    return payload


def build_fucked_payload(fucked_events):
    """Assemble the ``/api/fucked`` payload from wasted-time events.

    Inverted scoring vs the other domains: this-week hours are compared to a
    ``weekly_cap`` you want to stay under (status under / near / over), and a
    ``daily_cap`` defines "clean" days for :func:`compute_clean_streak`. Rolling
    averages, day-of-week, and chart series mirror the growth/fun shape. The
    frontend "Containment meter" keys off week hours vs the cap and the "Void
    level" off total wasted hours (higher = worse, shown as a warning).
    """
    fucked_cfg = cfg("fucked")
    weekly_cap = fucked_cfg["weekly_cap"]
    daily_cap = fucked_cfg["daily_cap"]
    buckets_cfg = cfg("chart_buckets")

    fucked_groups = {"fucked": fucked_events}
    daily_map = daily_hours_map(fucked_events)
    week_start = current_week_start()
    week_events = events_since(fucked_events, week_start)
    week_hours = round_hours(sum(event["hours"] for event in week_events))

    roll_7 = rolling_totals(fucked_events, 7)
    roll_30 = rolling_totals(fucked_events, 30)
    roll_90 = rolling_totals(fucked_events, 90)

    hours_per_week_30 = round_hours(roll_30["total"] / (30 / 7)) if roll_30["total"] else 0
    hours_per_week_90 = round_hours(roll_90["total"] / (90 / 7)) if roll_90["total"] else 0

    if weekly_cap and week_hours > weekly_cap:
        status = "over"
    elif weekly_cap and week_hours > weekly_cap * 0.75:
        status = "near"
    else:
        status = "under"

    return {
        "allocation_history": allocation_history(fucked_events),
        "history_days": recorded_history_days(fucked_events),
        "total_hours": round_hours(sum(event["hours"] for event in fucked_events)),
        "this_week": {
            "hours": week_hours,
            "sessions": count_sessions(week_events),
            "cap": weekly_cap,
            "over_cap": bool(weekly_cap and week_hours > weekly_cap),
            "status": status,
            "percent_of_cap": round(min(150.0, week_hours / weekly_cap * 100), 1) if weekly_cap else 0,
            "remaining": round_hours(max(0, weekly_cap - week_hours)) if weekly_cap else 0,
        },
        "averages": {
            "hours_per_week_30d": hours_per_week_30,
            "hours_per_week_90d": hours_per_week_90,
            "hours_per_day_30d": roll_30["avg_per_day"],
        },
        "clean_streak": compute_clean_streak(daily_map, daily_cap),
        "timeframes": {
            "daily": aggregate_by_period(fucked_groups, "day", buckets_cfg["daily"]),
            "weekly": aggregate_by_period(fucked_groups, "week", buckets_cfg["weekly"]),
        },
        "day_of_week": aggregate_day_of_week(fucked_events),
        "rolling": {
            "last_1_days": rolling_totals(fucked_events, 1),
            "last_365_days": rolling_totals(fucked_events, 365),
            "last_7_days": roll_7,
            "last_30_days": roll_30,
            "last_90_days": roll_90,
        },
        "config": {
            "calendar": fucked_cfg["calendar"],
            "weekly_cap": weekly_cap,
            "daily_cap": daily_cap,
        },
    }


def get_fucked_analytics(force_refresh=False):
    if cache_valid(_fucked_cache, force_refresh):
        payload = _fucked_cache["payload"]
        payload["meta"]["from_cache"] = True
        return payload

    service = get_calendar_service()
    fucked_events = fetch_named_calendar_events(service, cfg("fucked")["calendar"])
    payload = build_fucked_payload(fucked_events)
    fetched_at = utc_now()
    payload["meta"] = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
    }
    _fucked_cache["payload"] = payload
    _fucked_cache["fetched_at"] = fetched_at
    return payload


def get_things_analytics(force_refresh=False):
    if cache_valid(_things_cache, force_refresh):
        payload = _things_cache["payload"]
        payload["meta"]["from_cache"] = True
        return payload

    payload = fetch_things_data(load_config())
    fetched_at = utc_now()
    payload["meta"] = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
    }
    _things_cache["payload"] = payload
    _things_cache["fetched_at"] = fetched_at
    return payload


# --------------------------------------------------------------------------- #
# Education domain payload & orchestration                                      #
# --------------------------------------------------------------------------- #


def get_calendar_data(service):
    """Fetch parsed events for every education calendar plus manual adjustments.

    Returns ``(event_groups, all_events)`` where ``event_groups`` is keyed by
    calendar name and ``all_events`` is the flattened list.
    """
    calendar_names = cfg("calendars")
    ids = {}
    for name in calendar_names:
        cal_id = find_calendar_id(service, name)
        if not cal_id:
            raise ValueError(f"Calendar not found: {name}")
        ids[name] = cal_id

    event_groups = {}
    for name in calendar_names:
        event_groups[name] = fetch_past_events(service, ids[name], name)

    adjustments = load_manual_adjustments()
    for adj in adjustments:
        cal = adj["calendar"]
        if cal in event_groups:
            event_groups[cal].append(adj)

    all_events = [e for events in event_groups.values() for e in events]
    return event_groups, all_events


def build_analytics_payload(event_groups, all_events):
    """Assemble the full ``/api/analytics`` payload — the education tab's data contract.

    This is the single source of truth the frontend reads for the headline
    progress, KPIs, and every education-tab widget (including the gamification
    layer, which is derived entirely client-side from these fields). Top-level
    keys:

        goal_hours, total_hours, course_hours, research_hours, manual_hours,
        percent_to_goal, hours_remaining   -- headline numbers
        this_week        -- {hours, avg_per_day, status, days_elapsed}
        pace             -- weekly/monthly rates + ETAs (see compute_pace)
        milestones       -- crossed/next milestone progress
        streaks          -- {current, longest, days_active_this_month}
        balance          -- {last_30_days, last_90_days} courses/research split
        focus            -- deep/light/avg-session/weekend metrics
        comparison       -- {last_7/30/90_days} vs prior period
        event_breakdown  -- top topics/projects (powers the Quest Log)
        heatmap          -- 365-day activity grid
        timeframes       -- daily/weekly/monthly/yearly stacked series
        day_of_week, rolling, cumulative, config

    Note ``total_hours`` is courses + research only (not growth/operations);
    the RPG "Level" and "Path to Mastery" are computed from this value.
    """
    calendar_names = cfg("calendars")
    goal = cfg("goal_hours")
    buckets_cfg = cfg("chart_buckets")

    course_hours = round_hours(sum(e["hours"] for e in event_groups.get(calendar_names[0], [])))
    research_hours = round_hours(
        sum(e["hours"] for e in event_groups.get(calendar_names[1], []))
        if len(calendar_names) > 1
        else 0
    )
    manual_hours = round_hours(sum(e["hours"] for e in all_events if e.get("manual")))
    total_hours = round_hours(course_hours + research_hours)
    hours_remaining = round(max(0, goal - total_hours), 2)

    daily_map = daily_hours_map(all_events)
    pace = compute_pace(total_hours, hours_remaining, all_events)

    payload = {
        "goal_hours": goal,
        "allocation_history": allocation_history(all_events),
        "history_days": recorded_history_days(all_events),
        "total_hours": total_hours,
        "course_hours": course_hours,
        "research_hours": research_hours,
        "manual_hours": manual_hours,
        "percent_to_goal": round(total_hours / goal * 100, 2) if goal else 0,
        "hours_remaining": hours_remaining,
        "this_week": this_week_summary(all_events, pace),
        "pace": pace,
        "milestones": compute_milestones(total_hours),
        "streaks": compute_streaks(daily_map),
        "balance": {
            "last_30_days": compute_balance(event_groups, 30),
            "last_90_days": compute_balance(event_groups, 90),
        },
        "focus": compute_focus_metrics(all_events),
        "comparison": {
            "last_1_days": compare_periods(all_events, 1),
            "last_365_days": compare_periods(all_events, 365),
            "last_7_days": compare_periods(all_events, 7),
            "last_30_days": compare_periods(all_events, 30),
            "last_90_days": compare_periods(all_events, 90),
        },
        "event_breakdown": event_breakdown(all_events, cfg("event_breakdown_limit")),
        "heatmap": build_heatmap(daily_map, buckets_cfg["heatmap_days"]),
        "timeframes": {
            "daily": aggregate_by_period(event_groups, "day", buckets_cfg["daily"]),
            "weekly": aggregate_by_period(event_groups, "week", buckets_cfg["weekly"]),
            "monthly": aggregate_by_period(event_groups, "month", buckets_cfg["monthly"]),
            "yearly": aggregate_by_period(event_groups, "year", buckets_cfg["yearly"]),
        },
        "day_of_week": aggregate_day_of_week(all_events),
        "rolling": {
            "last_1_days": rolling_totals_by_calendar(event_groups, 1),
            "last_7_days": rolling_totals_by_calendar(event_groups, 7),
            "last_30_days": rolling_totals_by_calendar(event_groups, 30),
            "last_90_days": rolling_totals_by_calendar(event_groups, 90),
            "last_365_days": rolling_totals_by_calendar(event_groups, 365),
        },
        "cumulative": {
            "weekly": cumulative_series(event_groups, "week", buckets_cfg["weekly"]),
            "monthly": cumulative_series(event_groups, "month", buckets_cfg["monthly"]),
        },
        "config": {
            "calendars": calendar_names,
            "target_date": cfg("target_date"),
            "balance_target": cfg("balance_target"),
        },
    }
    return payload


def get_analytics(force_refresh=False):
    if cache_valid(_analytics_cache, force_refresh):
        payload = _analytics_cache["payload"]
        payload["meta"]["from_cache"] = True
        return payload

    service = get_calendar_service()
    event_groups, all_events = get_calendar_data(service)
    payload = build_analytics_payload(event_groups, all_events)
    fetched_at = utc_now()
    payload["meta"] = {
        "fetched_at": fetched_at.isoformat(),
        "from_cache": False,
    }

    _analytics_cache["payload"] = payload
    _analytics_cache["fetched_at"] = fetched_at
    persist_snapshot(payload, event_groups)
    return payload


def build_export_csv(payload):
    """Flatten the analytics ``timeframes`` into a downloadable per-bucket CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["period", "label", "courses", "research", "total"])

    calendar_names = payload["config"]["calendars"]
    c_name, r_name = calendar_names[0], calendar_names[1] if len(calendar_names) > 1 else ""

    for period_name, frame in payload["timeframes"].items():
        for i, label in enumerate(frame["labels"]):
            writer.writerow([
                period_name,
                label,
                frame.get(c_name, [0] * len(frame["labels"]))[i],
                frame.get(r_name, [0] * len(frame["labels"]))[i],
                frame["total"][i],
            ])

    return output.getvalue()


# --------------------------------------------------------------------------- #
# Flask routes                                                                  #
# --------------------------------------------------------------------------- #
#
# Each /api/* route shares the same contract: pass ``?refresh=1`` to bypass the
# cache, get a domain JSON payload on success, ``{"error": ...}`` with status
# 404 (calendar/resource missing) or 500 (anything else) on failure.


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "landing.html")


@app.route("/dashboard")
def dashboard():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/insights.js")
def insights_script():
    return send_from_directory(BASE_DIR, "insights.js")


@app.route("/theme.js")
def theme_script():
    return send_from_directory(BASE_DIR, "theme.js")


@app.route("/theme.css")
def theme_styles():
    return send_from_directory(BASE_DIR, "theme.css")


@app.route("/api/analytics")
def api_analytics():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(get_analytics(force_refresh=force))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/stats")
def api_stats():
    try:
        data = get_analytics()
        return jsonify(
            {
                "total_hours": data["total_hours"],
                "course_hours": data["course_hours"],
                "research_hours": data["research_hours"],
                "percent_to_goal": data["percent_to_goal"],
                "hours_remaining": data["hours_remaining"],
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/operations")
def api_operations():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(get_operations_analytics(force_refresh=force))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/growth")
def api_growth():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(get_growth_analytics(force_refresh=force))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/work")
def api_work():
    try:
        return jsonify(get_work_analytics(force_refresh=request.args.get("refresh") == "1"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/fun")
def api_fun():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(get_fun_analytics(force_refresh=force))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/fucked")
def api_fucked():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(get_fucked_analytics(force_refresh=force))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


@app.route("/api/things")
def api_things():
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(get_things_analytics(force_refresh=force))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/export")
def api_export():
    try:
        data = get_analytics()
        csv_data = build_export_csv(data)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=education-hours.csv"},
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": format_api_error(exc)}), 500


def format_api_error(exc):
    """Turn a raw exception into a user-facing message, with a hint for the common
    "Calendar API not enabled" Google error."""
    message = str(exc)
    if "accessNotConfigured" in message or "has not been used" in message:
        return (
            "Google Calendar API is not enabled. Enable it at "
            "console.cloud.google.com → APIs & Services → Library → Google Calendar API"
        )
    return message


if __name__ == "__main__":
    init_db()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="127.0.0.1", port=int(os.environ.get("PORT", "5001")))
