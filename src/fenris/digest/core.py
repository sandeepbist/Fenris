"""Scheduled digests: "what changed in my Drive" as a push product.

P2.4 of the roadmap (AnythingLLM Scheduled Jobs precedent, adapted to
Fenris's honesty rules). A digest is computed PURELY from the local
projection - no network, no generation call, works offline - because
the projection already records everything a digest needs:

- sources.created_at  -> when each representation was indexed
- sources.status      -> failed / unreadable rows are digest problems
- drive_files.trashed -> the removal trail (set by tombstoning)

Honesty rules specific to digests:

1. No retroactive churn. A file that existed before the window and only
   got a new representation inside it is "updated", not "new". A first
   sighting inside the window is "new" only if no source row for the
   file predates the window.
2. Nothing double-reports. Each digest's window starts at the previous
   run's generated_at (chained windows); an on-demand digest chains from
   the same anchor so a manual run doesn't re-list what the last run
   already reported.
3. Removals are stated as observations, not verdicts: the projection
   knows a file stopped being served; it cannot always distinguish
   "deleted on Drive" from "access revoked" - the digest says which
   signal fired. Indexing failures (unreadable/failed) take precedence
   over the removal signal: they land in "problems", not "removed".

Schedules are a cron subset: minute, hour, and day-of-week fields
(digests live on daily/weekly rhythms; '*' everywhere = daily at the
given time). dow follows cron's 0=Sunday convention.

Every SQL statement is one single-line fixed string with ? placeholders;
values only ever travel through parameter tuples.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

MAX_LIST = 50  # per-section file list cap: digests are skimmable, not dumps

_FIELD_RE = re.compile(r"^(\*|\*/\d+|\d+(-\d+)?)(,(\*|\*/\d+|\d+(-\d+)?))*$")


@dataclass
class DigestSection:
    """One bucket of the digest, capped for display."""

    kind: str                          # new | updated | removed | problems | skipped
    file_ids: list[str] = field(default_factory=list)
    titles: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass
class Digest:
    generated_at: str
    window_start: str
    window_days: float
    counts: dict = field(default_factory=dict)
    markdown: str = ""
    sections: dict = field(default_factory=dict)  # kind -> DigestSection


@dataclass
class DigestSchedule:
    id: int
    name: str
    cron_minute: str
    cron_hour: str
    cron_dow: str
    window_days: float
    enabled: bool
    last_run_at: str | None
    created_at: str | None


def ensure_digest_tables(conn: sqlite3.Connection) -> None:
    """Idempotent lazy migration (the ensure_hints pattern: installs on
    first use; no schema_version bump - old installs simply had no digests)."""
    conn.execute("CREATE TABLE IF NOT EXISTS digest_schedules (id INTEGER PRIMARY KEY, name TEXT NOT NULL, cron_minute TEXT NOT NULL, cron_hour TEXT NOT NULL, cron_dow TEXT NOT NULL, window_days INTEGER NOT NULL DEFAULT 7, enabled INTEGER NOT NULL DEFAULT 1, last_run_at TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS digest_runs (id INTEGER PRIMARY KEY, schedule_id INTEGER, started_at TEXT, generated_at TEXT, window_start TEXT, window_days REAL, counts_json TEXT NOT NULL DEFAULT '{}', markdown TEXT NOT NULL DEFAULT '')")
    conn.commit()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_days_ago(days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400.0))


# ---- compute -------------------------------------------------------------------


def compute_digest(conn: sqlite3.Connection, window_days: float = 7.0,
                   *, anchor: str | None = None) -> Digest:
    """Compute one digest from the projection. No network, no LLM.

    The window starts at ``anchor`` when given (a schedule chains from
    its last run), else at the previous digest run's generated_at, else
    at now - window_days. The returned markdown is self-contained;
    callers persist it via persist_digest (which chains the next window).
    """
    ensure_digest_tables(conn)
    start = anchor
    if start is None:
        row = conn.execute("SELECT generated_at FROM digest_runs ORDER BY id DESC LIMIT 1", ()).fetchone()
        start = row[0] if row and row[0] else _iso_days_ago(window_days)
    generated = _now_iso()

    new_files: list[str] = []
    updated_files: list[str] = []
    problems: list[str] = []
    skipped: list[str] = []

    rows = conn.execute("SELECT file_id, title, status FROM sources WHERE created_at > ? ORDER BY created_at DESC", (start,)).fetchall()
    for file_id, _title, status in rows:
        prior = conn.execute("SELECT count(*) FROM sources WHERE file_id = ? AND created_at <= ?", (file_id, start)).fetchone()[0]
        if status in ("failed", "unreadable"):
            if file_id not in problems:
                problems.append(file_id)
            continue
        if status == "skipped":
            if file_id not in skipped:
                skipped.append(file_id)
            continue
        if status != "ok":
            continue
        if prior == 0:
            if file_id not in new_files and file_id not in updated_files:
                new_files.append(file_id)
        else:
            if file_id not in updated_files and file_id not in new_files:
                updated_files.append(file_id)

    # Removals: files whose serving stopped inside the window. Access
    # revocations are recorded as 'unreadable' sources - those are
    # problems, so they never double-list here.
    removed: list[str] = []
    for (file_id,) in conn.execute("SELECT file_id FROM drive_files WHERE trashed = 1 AND updated_at > ? ORDER BY updated_at DESC", (start,)).fetchall():
        if file_id not in problems and file_id not in removed:
            removed.append(file_id)

    def _title_map(ids: list[str]) -> dict[str, str]:
        out = {}
        for fid in ids:
            row = conn.execute("SELECT title FROM sources WHERE file_id = ? ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
            if row and row[0]:
                out[fid] = row[0]
                continue
            df = conn.execute("SELECT title FROM drive_files WHERE file_id = ? LIMIT 1", (fid,)).fetchone()
            out[fid] = (df[0] if df and df[0] else None) or fid
        return out

    counts = {
        "new": len(new_files),
        "updated": len(updated_files),
        "removed": len(removed),
        "problems": len(problems),
        "skipped": len(skipped),
    }
    digest = Digest(
        generated_at=generated,
        window_start=start,
        window_days=window_days,
        counts=counts,
        sections={
            "new": DigestSection("new", new_files, _title_map(new_files)),
            "updated": DigestSection("updated", updated_files, _title_map(updated_files)),
            "removed": DigestSection("removed", removed, _title_map(removed),
                                     note="stopped being served - deleted, trashed, or access revoked on Drive"),
            "problems": DigestSection("problems", problems, _title_map(problems),
                                      note="failed or unreadable during indexing"),
            "skipped": DigestSection("skipped", skipped, _title_map(skipped),
                                     note="format on the skip list (not parsed)"),
        },
    )
    digest.markdown = render_markdown(digest)
    return digest


def render_markdown(digest: Digest) -> str:
    """The digest as skimmable markdown (what the UI shows, CLI prints)."""
    c = digest.counts
    lines = [
        "# Drive digest",
        "",
        f"_{digest.window_start} → {digest.generated_at} (~{digest.window_days:g}d window)_",
        "",
        f"**{c['new']} new · {c['updated']} updated · {c['removed']} removed · {c['problems']} problems · {c['skipped']} skipped**",
        "",
    ]
    if sum(c.values()) == 0:
        lines += [
            "Nothing changed in the projection during this window.",
            "No files indexed, no revisions ingested, no removals",
            "observed, nothing failed. A quiet Drive is a valid digest",
            "result - not an error.",
            "",
        ]
        return "\n".join(lines)

    for kind, label in (
        ("new", "New files"),
        ("updated", "Updated files"),
        ("removed", "Removed or trashed files"),
        ("problems", "Indexing problems"),
        ("skipped", "Skipped formats"),
    ):
        section = digest.sections.get(kind)
        if not section or not section.file_ids:
            continue
        lines.append(f"## {label} ({len(section.file_ids)})")
        if section.note:
            lines.append(f"_{section.note}_")
        lines.append("")
        overflow = len(section.file_ids) - MAX_LIST
        for fid in section.file_ids[:MAX_LIST]:
            lines.append(f"- {section.titles.get(fid, fid)}")
        if overflow > 0:
            lines.append(f"- … and {overflow} more")
        lines.append("")
    return "\n".join(lines)


# ---- persist + read -------------------------------------------------------------


def persist_digest(conn: sqlite3.Connection, digest: Digest,
                   schedule_id: int | None = None) -> int:
    """Record a digest run; the ledger that chains the next window."""
    ensure_digest_tables(conn)
    cur = conn.execute("INSERT INTO digest_runs(schedule_id, started_at, generated_at, window_start, window_days, counts_json, markdown) VALUES (?, ?, ?, ?, ?, ?, ?)", (schedule_id, digest.generated_at, digest.generated_at, digest.window_start, digest.window_days, json.dumps(digest.counts), digest.markdown))
    if schedule_id is not None:
        conn.execute("UPDATE digest_schedules SET last_run_at = ? WHERE id = ?", (digest.generated_at, schedule_id))
    conn.commit()
    return cur.lastrowid


def latest_digest(conn: sqlite3.Connection) -> Digest | None:
    """The most recent persisted digest (dashboard display)."""
    ensure_digest_tables(conn)
    row = conn.execute("SELECT generated_at, window_start, window_days, counts_json, markdown FROM digest_runs ORDER BY id DESC LIMIT 1", ()).fetchone()
    if row is None:
        return None
    try:
        counts = json.loads(row[3])
    except (ValueError, TypeError):
        counts = {}
    return Digest(generated_at=row[0], window_start=row[1], window_days=row[2] or 0.0, counts=counts, markdown=row[4] or "")


def recent_digests(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Persisted digest runs, newest first, as display dicts."""
    ensure_digest_tables(conn)
    rows = conn.execute("SELECT id, schedule_id, generated_at, window_start, window_days, counts_json FROM digest_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for rid, sched, gen, wstart, wdays, counts_json in rows:
        try:
            counts = json.loads(counts_json)
        except (ValueError, TypeError):
            counts = {}
        out.append({
            "id": rid, "schedule_id": sched, "generated_at": gen,
            "window_start": wstart, "window_days": wdays, "counts": counts,
        })
    return out


# ---- schedules (cron subset: minute, hour, dow) ----------------------------------


def validate_cron_field(field: str, low: int, high: int) -> str:
    """Accept '*', 'N', 'a-b', '*/N', or comma lists of those; reject the rest."""
    field = field.strip()
    if not _FIELD_RE.match(field):
        raise ValueError(f"invalid cron field: {field!r}")
    for part in field.split(","):
        if part == "*":
            continue
        if part.startswith("*/"):
            step = int(part[2:])
            if step < 1:
                raise ValueError(f"invalid step in cron field: {field!r}")
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if not (low <= lo <= high and low <= hi <= high and lo <= hi):
                raise ValueError(f"range out of bounds in cron field: {field!r}")
            continue
        v = int(part)
        if not (low <= v <= high):
            raise ValueError(f"value out of bounds in cron field: {field!r}")
    return field


def add_schedule(conn: sqlite3.Connection, name: str, minute: str, hour: str,
                 dow: str, window_days: float = 7.0) -> int:
    """Create a digest schedule after validating every cron field."""
    ensure_digest_tables(conn)
    minute = validate_cron_field(minute, 0, 59)
    hour = validate_cron_field(hour, 0, 23)
    dow = validate_cron_field(dow, 0, 6)
    if not (0.25 <= window_days <= 90):
        raise ValueError("window_days must be between 0.25 and 90")
    cur = conn.execute("INSERT INTO digest_schedules(name, cron_minute, cron_hour, cron_dow, window_days, enabled, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)", (name[:100], minute, hour, dow, window_days, _now_iso()))
    conn.commit()
    return cur.lastrowid


def list_schedules(conn: sqlite3.Connection) -> list[DigestSchedule]:
    ensure_digest_tables(conn)
    rows = conn.execute("SELECT id, name, cron_minute, cron_hour, cron_dow, window_days, enabled, last_run_at, created_at FROM digest_schedules ORDER BY id", ()).fetchall()
    return [
        DigestSchedule(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8])
        for r in rows
    ]


def delete_schedule(conn: sqlite3.Connection, schedule_id: int) -> bool:
    ensure_digest_tables(conn)
    cur = conn.execute("DELETE FROM digest_schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    return cur.rowcount > 0


def set_schedule_enabled(conn: sqlite3.Connection, schedule_id: int, enabled: bool) -> bool:
    ensure_digest_tables(conn)
    cur = conn.execute("UPDATE digest_schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, schedule_id))
    conn.commit()
    return cur.rowcount > 0


def _field_matches(field: str, value: int) -> bool:
    """One cron field vs one value ('*' matches everything)."""
    for part in field.split(","):
        if part == "*":
            return True
        if part.startswith("*/"):
            if value % int(part[2:]) == 0:
                return True
        elif "-" in part:
            a, b = part.split("-", 1)
            if int(a) <= value <= int(b):
                return True
        elif int(part) == value:
            return True
    return False


def schedule_matches(schedule: DigestSchedule, now: time.struct_time) -> bool:
    """Does this schedule fire at ``now``? dow uses cron's 0=Sunday."""
    cron_dow = (now.tm_wday + 1) % 7  # python tm_wday: 0=Monday
    return (
        _field_matches(schedule.cron_minute, now.tm_min)
        and _field_matches(schedule.cron_hour, now.tm_hour)
        and _field_matches(schedule.cron_dow, cron_dow)
    )


def _minute_start_iso(now: time.struct_time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:00Z", now)


def run_due(conn: sqlite3.Connection, now: time.struct_time | None = None) -> list[dict]:
    """Compute + persist every due schedule. Idempotent within a minute.

    A schedule is due when the current time matches its cron fields AND
    it has not already run within this minute (the daemon ticks more
    often than the minute). The window chains from last_run_at, or
    window_days back on the first run.
    """
    ensure_digest_tables(conn)
    moment = now or time.gmtime()
    minute_start = _minute_start_iso(moment)
    results = []
    rows = conn.execute("SELECT id, name, cron_minute, cron_hour, cron_dow, window_days, last_run_at FROM digest_schedules WHERE enabled = 1", ()).fetchall()
    for sid, name, minute, hour, dow, window_days, last_run_at in rows:
        sched = DigestSchedule(sid, name, minute, hour, dow, window_days, True, last_run_at, None)
        if not schedule_matches(sched, moment):
            continue
        if last_run_at and last_run_at >= minute_start:
            continue  # already fired this minute
        digest = compute_digest(conn, window_days, anchor=last_run_at)
        run_id = persist_digest(conn, digest, schedule_id=sid)
        results.append({
            "schedule_id": sid, "name": name, "run_id": run_id,
            "counts": digest.counts,
        })
    return results
