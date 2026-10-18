"""Read-only integration with the Things 3 task manager (macOS).

Things stores its data in a Core Data SQLite database. This module reads that DB
directly (no AppleScript) to surface today's tasks, upcoming tasks, the inbox
count, and recently completed work for the dashboard's Things tab. The single
public entry point is :func:`fetch_things_data`; everything else is a helper.

Two Things-specific encoding quirks dominate this file:

* **Core Data epoch.** Timestamps (e.g. ``stopDate``) are seconds since
  2001-01-01 UTC, not the Unix epoch. ``CORE_DATA_EPOCH`` converts between them.

* **Dual-format dates.** ``startDate`` / ``deadline`` columns hold one of two
  encodings, and older and newer rows can use different ones:
    - legacy: a "day integer" (days since the Core Data epoch), value < 100000;
    - modern: a bit-packed Y-M-D integer (``year<<16 | month<<12 | day<<7``).
  The ``_start_date_*_sql`` helpers therefore branch on the ``< 100000`` cutoff,
  and queries pass both a packed value and a day-integer value for comparison.

Safety: the DB is snapshotted to a temp file and opened read-only, so reads are
safe even while Things is running (see :func:`things_connection`).
"""

import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Seconds between the Unix epoch and the Core Data epoch (2001-01-01 UTC).
CORE_DATA_EPOCH = 978307200


def things_cfg(config):
    """Return the ``things`` config block merged over defaults."""
    defaults = {
        "enabled": True,
        "db_path": None,
        "areas": [],
        "tags": [],
        "today_limit": 20,
        "upcoming_limit": 10,
        "upcoming_days": 7,
        "completed_days": 30,
        "recent_completed_limit": 10,
    }
    user = config.get("things") or {}
    return {**defaults, **user}


PROJECT_DIR = Path(__file__).resolve().parent


def detect_db_path(explicit_path=None):
    """Resolve overrides, project snapshots, then Things containers without hiding denial."""
    override = explicit_path or os.environ.get("THINGS_DB_PATH")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = PROJECT_DIR / path
        path.stat()  # Permission failures must not look like missing databases.
        if not path.is_file():
            raise FileNotFoundError(f"Things database is not a file: {path}")
        return str(path)

    for path in (PROJECT_DIR / "data/things/main.sqlite",
                 PROJECT_DIR / "data/things/Things Database.thingsdatabase/main.sqlite"):
        try:
            path.stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            return str(path)

    base = Path.home() / "Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac"
    try:
        folders = sorted(base.iterdir(), reverse=True)
    except FileNotFoundError:
        folders = []
    except PermissionError as exc:
        raise PermissionError(
            "macOS blocked access to the Things data folder. Enable Full Disk Access "
            "for the app running this dashboard (ChatGPT/Codex or Terminal), then quit "
            "and reopen that app and restart the dashboard. Alternatively, copy the "
            "complete Things Database.thingsdatabase folder into data/things/."
        ) from exc
    for folder in folders:
        if not folder.name.startswith("ThingsData-"):
            continue
        path = folder / "Things Database.thingsdatabase/main.sqlite"
        try:
            path.stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            return str(path)
    raise FileNotFoundError(
        "Things database not found. Set things.db_path in config.yaml or place a "
        "recovered database at data/things/main.sqlite."
    )


# --- Things date encoding/decoding (see module docstring for the two formats) ---


def today_as_day_integer():
    """Today as a legacy "day integer" (days since the Core Data epoch)."""
    now = datetime.now(timezone.utc).timestamp()
    return int((now - CORE_DATA_EPOCH) // 86400)


def day_integer_to_date(day_int):
    """Decode a legacy day-integer date to a ``YYYY-MM-DD`` string."""
    ms = (day_int * 86400 + CORE_DATA_EPOCH) * 1000
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def things_date_to_ymd(value):
    """Decode Things startDate/deadline (packed int or legacy day integer)."""
    if value is None or value == 0:
        return None
    if value < 100000:
        return day_integer_to_date(value)
    year = value >> 16
    month = (value >> 12) & 15
    day = (value >> 7) & 31
    if year < 1 or month < 1 or day < 1:
        return None
    return f"{year}-{month:02d}-{day:02d}"


def datetime_to_things_packed(dt):
    """Encode a datetime as Things' modern bit-packed Y-M-D integer."""
    return (dt.year << 16) | (dt.month << 12) | (dt.day << 7)


def today_things_packed():
    now = datetime.now(timezone.utc)
    return datetime_to_things_packed(now)


def upcoming_end_things_packed(days):
    end = datetime.now(timezone.utc) + timedelta(days=days)
    return datetime_to_things_packed(end)


# --- SQL fragments that compare startDate across both encodings -------------- #
# Each returns a WHERE-clause snippet with one or more "?" placeholders; callers
# supply the matching params (typically a day-integer and a packed value).


def _start_date_equals_today_sql():
    """Match startDate stored as legacy day int or packed Y-M-D int."""
    return """(
        t.startDate = ?
        OR t.startDate = ?
    )"""


def _start_date_on_or_before_today_sql():
    return """(
        (t.startDate < 100000 AND t.startDate <= ?)
        OR (t.startDate >= 100000 AND t.startDate <= ?)
    )"""


def _start_date_before_today_sql():
    return """(
        (t.startDate < 100000 AND t.startDate < ?)
        OR (t.startDate >= 100000 AND t.startDate < ?)
    )"""


def _start_date_after_today_through_end_sql():
    return """(
        (t.startDate < 100000 AND t.startDate > ? AND t.startDate <= ?)
        OR (t.startDate >= 100000 AND t.startDate > ? AND t.startDate <= ?)
    )"""


def core_data_timestamp_to_iso(timestamp):
    return datetime.fromtimestamp(timestamp + CORE_DATA_EPOCH, tz=timezone.utc).isoformat()


@contextmanager
def things_connection(db_path):
    """Read-only snapshot; close all handles and remove DB/WAL/SHM on every exit."""
    with tempfile.TemporaryDirectory(prefix="dashboard-things-") as directory:
        tmp_path = Path(directory) / "snapshot.sqlite"
        source_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        # Nested closing also releases source if opening the destination fails.
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            with closing(sqlite3.connect(tmp_path)) as destination:
                source.backup(destination)
        with closing(sqlite3.connect(tmp_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn
    # TemporaryDirectory removes SQLite sidecars as well as the main snapshot.


# --- Query building & row formatting ----------------------------------------- #


def _filter_clauses(settings):
    """Build optional area/tag WHERE conditions from config filters.

    Returns ``(conditions, params)`` to splice into a task query; empty when no
    area/tag filters are configured (i.e. include everything).
    """
    conditions = []
    params = []

    if settings["areas"]:
        placeholders = ",".join("?" * len(settings["areas"]))
        conditions.append(f"a.title IN ({placeholders})")
        params.extend(settings["areas"])

    if settings["tags"]:
        placeholders = ",".join("?" * len(settings["tags"]))
        conditions.append(
            f"""t.uuid IN (
                SELECT tt.tasks FROM TMTaskTag tt
                JOIN TMTag tg ON tt.tags = tg.uuid
                WHERE tg.title IN ({placeholders})
            )"""
        )
        params.extend(settings["tags"])

    return conditions, params


def _format_todo(row, tags_map):
    """Shape a task DB row into the dict the frontend renders (incl. a ``things://`` deep link)."""
    uuid = row["uuid"]
    return {
        "uuid": uuid,
        "title": row["title"],
        "project": row["projectTitle"],
        "area": row["areaTitle"],
        "tags": tags_map.get(uuid, []),
        "start_date": things_date_to_ymd(row["startDate"]),
        "deadline": things_date_to_ymd(row["deadline"]),
        "things_url": f"things:///show?id={uuid}",
    }


def _batch_load_tags(conn, uuids):
    """Load tag titles for many tasks in one query -> ``{task_uuid: [tag, ...]}``.

    Batched to avoid an N+1 query per task when formatting a list.
    """
    if not uuids:
        return {}

    placeholders = ",".join("?" * len(uuids))
    rows = conn.execute(
        f"""
        SELECT tt.tasks AS task_uuid, tg.title
        FROM TMTaskTag tt
        JOIN TMTag tg ON tt.tags = tg.uuid
        WHERE tt.tasks IN ({placeholders})
        ORDER BY tg.title ASC
        """,
        uuids,
    ).fetchall()

    tags_map = {}
    for row in rows:
        tags_map.setdefault(row["task_uuid"], []).append(row["title"])
    return tags_map


def _query_todos(conn, settings, list_name):
    """Query open to-dos for a named list: ``today``, ``upcoming``, or ``inbox``.

    Filters to actionable to-dos (``type=0``, not trashed, status open) and
    applies list-specific startDate logic that mirrors Things' own definitions —
    e.g. "today" includes items starred for today (``todayIndex > 0``) and any
    started-but-not-done items whose start date has passed.
    """
    today_packed = today_things_packed()
    upcoming_end = upcoming_end_things_packed(settings["upcoming_days"])
    today_day_int = today_as_day_integer()
    conditions = ["t.type = 0", "t.trashed = 0", "t.status = 0"]
    params = []

    if list_name == "today":
        conditions.append(
            f"""(
                {_start_date_equals_today_sql()}
                OR (
                    t.todayIndex > 0
                    AND (
                        t.startDate IS NULL
                        OR {_start_date_on_or_before_today_sql()}
                    )
                )
                OR (
                    t.startDate IS NOT NULL
                    AND t.todayIndex <= 0
                    AND {_start_date_before_today_sql()}
                )
            )"""
        )
        params.extend(
            [today_packed, today_day_int, today_day_int, today_packed, today_day_int, today_packed]
        )
        order_by = "t.startDate ASC, t.title ASC"
        limit = settings["today_limit"]
    elif list_name == "upcoming":
        conditions.append("t.startDate IS NOT NULL")
        conditions.append(_start_date_after_today_through_end_sql())
        upcoming_day_end = today_day_int + settings["upcoming_days"]
        params.extend([today_day_int, upcoming_day_end, today_packed, upcoming_end])
        order_by = "t.startDate ASC, t.title ASC"
        limit = settings["upcoming_limit"]
    elif list_name == "inbox":
        conditions.extend(["t.start = 0", "t.project IS NULL"])
        order_by = "t.creationDate DESC"
        limit = 50
    else:
        raise ValueError(f"Unknown list: {list_name}")

    filter_conditions, filter_params = _filter_clauses(settings)
    conditions.extend(filter_conditions)
    params.extend(filter_params)

    sql = f"""
        SELECT
            t.uuid, t.title, t.startDate, t.deadline,
            p.title AS projectTitle,
            a.title AS areaTitle
        FROM TMTask t
        LEFT JOIN TMTask p ON t.project = p.uuid
        LEFT JOIN TMArea a ON COALESCE(t.area, p.area) = a.uuid
        WHERE {" AND ".join(conditions)}
        ORDER BY {order_by}
        LIMIT ?
    """
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    uuids = [row["uuid"] for row in rows]
    tags_map = _batch_load_tags(conn, uuids)
    return [_format_todo(row, tags_map) for row in rows]


def _count_inbox(conn, settings):
    """Count open inbox to-dos (no project, not yet scheduled: ``start = 0``)."""
    conditions = ["t.type = 0", "t.trashed = 0", "t.status = 0", "t.start = 0", "t.project IS NULL"]
    params = []
    filter_conditions, filter_params = _filter_clauses(settings)
    conditions.extend(filter_conditions)
    params.extend(filter_params)

    sql = f"""
        SELECT COUNT(*) AS cnt
        FROM TMTask t
        LEFT JOIN TMTask p ON t.project = p.uuid
        LEFT JOIN TMArea a ON COALESCE(t.area, p.area) = a.uuid
        WHERE {" AND ".join(conditions)}
    """
    return conn.execute(sql, params).fetchone()["cnt"]


def _core_data_cutoff(dt):
    return dt.timestamp() - CORE_DATA_EPOCH


def _completed_stats(conn, settings):
    """Completed-task counts (this week / this month) plus a recent-completions list.

    Matches done to-dos (``status = 3`` with a ``stopDate``). ``stopDate`` is a
    Core Data timestamp, so cutoffs are converted via :func:`_core_data_cutoff`.
    """
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    completed_since = now - timedelta(days=settings["completed_days"])

    week_cutoff = _core_data_cutoff(week_start)
    month_cutoff = _core_data_cutoff(month_start)
    days_cutoff = _core_data_cutoff(completed_since.replace(hour=0, minute=0, second=0, microsecond=0))

    conditions = ["t.type = 0", "t.trashed = 0", "t.status = 3", "t.stopDate IS NOT NULL"]
    params = []
    filter_conditions, filter_params = _filter_clauses(settings)
    conditions.extend(filter_conditions)
    params.extend(filter_params)

    base_from = """
        FROM TMTask t
        LEFT JOIN TMTask p ON t.project = p.uuid
        LEFT JOIN TMArea a ON COALESCE(t.area, p.area) = a.uuid
    """
    where = " AND ".join(conditions)

    week_count = conn.execute(
        f"SELECT COUNT(*) AS cnt {base_from} WHERE {where} AND t.stopDate >= ?",
        params + [week_cutoff],
    ).fetchone()["cnt"]

    month_count = conn.execute(
        f"SELECT COUNT(*) AS cnt {base_from} WHERE {where} AND t.stopDate >= ?",
        params + [month_cutoff],
    ).fetchone()["cnt"]

    recent_rows = conn.execute(
        f"""
        SELECT t.uuid, t.title, t.stopDate, t.startDate, t.deadline,
               p.title AS projectTitle, a.title AS areaTitle
        {base_from}
        WHERE {where} AND t.stopDate >= ?
        ORDER BY t.stopDate DESC
        LIMIT ?
        """,
        params + [days_cutoff, settings["recent_completed_limit"]],
    ).fetchall()

    # Full daily completion counts, independent of the short recent-task list.
    history_start = (now - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_rows = conn.execute(
        f"SELECT t.stopDate {base_from} WHERE {where} AND t.stopDate >= ? AND t.stopDate < ?",
        params + [_core_data_cutoff(history_start), _core_data_cutoff(now)],
    ).fetchall()
    completed_daily = {}
    for row in daily_rows:
        key = core_data_timestamp_to_iso(row["stopDate"])[:10]
        completed_daily[key] = completed_daily.get(key, 0) + 1

    uuids = [row["uuid"] for row in recent_rows]
    tags_map = _batch_load_tags(conn, uuids)
    recent = []
    for row in recent_rows:
        item = _format_todo(row, tags_map)
        item["completed_at"] = core_data_timestamp_to_iso(row["stopDate"])
        recent.append(item)

    return {
        "this_week": week_count,
        "this_month": month_count,
        "recent": recent,
        "daily": completed_daily,
        "history_start": history_start.date().isoformat(),
    }


def fetch_things_data(config):
    """Public entry point: return the full Things payload for ``/api/things``.

    Always returns a dict with ``enabled`` and ``available`` flags so the
    frontend can degrade gracefully. Failure modes never raise: a disabled
    integration, a missing database, or any query error all come back as
    ``available: False`` with an ``error`` message.
    """
    settings = things_cfg(config)
    if not settings["enabled"]:
        return {"enabled": False, "available": False}

    try:
        db_path = detect_db_path(settings["db_path"])
    except OSError as exc:
        return {"enabled": True, "available": False, "error": str(exc)}

    try:
        with things_connection(db_path) as conn:
            today = _query_todos(conn, settings, "today")
            upcoming = _query_todos(conn, settings, "upcoming")
            inbox_count = _count_inbox(conn, settings)
            completed = _completed_stats(conn, settings)

        return {
            "enabled": True,
            "available": True,
            "error": None,
            "db_path": db_path,
            "source_mode": "snapshot" if PROJECT_DIR / "data/things" in Path(db_path).resolve().parents else "live",
            "inbox_count": inbox_count,
            "today": today,
            "upcoming": upcoming,
            "completed_this_week": completed["this_week"],
            "completed_this_month": completed["this_month"],
            "recent_completed": completed["recent"],
            "completed_daily": completed["daily"],
            "completion_history_start": completed["history_start"],
            "stats": {
                "today_count": len(today),
                "upcoming_count": len(upcoming),
            },
            "filters": {
                "areas": settings["areas"],
                "tags": settings["tags"],
            },
        }
    except Exception as exc:
        return {
            "enabled": True,
            "available": False,
            "error": str(exc),
        }
