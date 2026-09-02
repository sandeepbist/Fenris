"""The digest scheduler daemon: one thread, ticking, running due schedules.

The API process is the always-on host (the desktop shell will make it
literally always-on; `fenris api` today is close enough - the thread
lives inside the server process and dies with it). External cron users
get the same behavior stateless via `fenris digest run-due`.

Design points:

- Daemon thread + stop Event: never blocks shutdown.
- One fresh sqlite connection per tick: WAL means readers never block,
  and a connection held across minutes would defeat that.
- A failed tick is logged and retried on the next tick - the scheduler
  must outlive any single transient failure (missing tables, locked DB).
- The tick granularity is sub-minute, so every cron minute fires exactly
  once (run_due is idempotent within a minute by last_run_at).
"""

from __future__ import annotations

import logging
import sqlite3
import threading

logger = logging.getLogger("fenris.scheduler")

TICK_SECONDS = 30


def start_digest_scheduler(
    stop_event: threading.Event, tick_seconds: float = TICK_SECONDS
) -> threading.Thread:
    """Start the daemon; returns the thread (caller never joins it)."""
    thread = threading.Thread(
        target=_loop, args=(stop_event, tick_seconds), daemon=True,
        name="fenris-digest-scheduler",
    )
    thread.start()
    return thread


def _loop(stop_event: threading.Event, tick_seconds: float) -> None:
    while not stop_event.is_set():
        try:
            _tick()
        except Exception as e:  # noqa: BLE001 - the daemon never dies
            logger.warning("digest scheduler tick failed: %s: %s", type(e).__name__, e)
        stop_event.wait(tick_seconds)


def _tick() -> None:
    from fenris.config import load_config
    from fenris.db import connect
    from fenris.digest import run_due

    cfg = load_config()
    conn = connect(cfg.db_path)
    try:
        ran = run_due(conn)
        for r in ran:
            logger.info(
                "digest ran: %s (schedule %s): %s",
                r["name"], r["schedule_id"], r["counts"],
            )
    except sqlite3.Error as e:
        logger.warning("digest tick DB error: %s", e)
    finally:
        conn.close()
