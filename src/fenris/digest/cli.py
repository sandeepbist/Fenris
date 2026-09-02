"""digest_app: Typer sub-app the CLI registers (P2.4).

    fenris digest now [-w DAYS]     compute + print one digest
    fenris digest list [N]          recent persisted digest runs
    fenris digest schedule ls       list schedules
    fenris digest schedule add NAME --at 08:00 [--daily|--dow N|-w DAYS]
    fenris digest schedule rm ID
    fenris digest schedule on/off ID
    fenris digest run-due           the scheduler's tick, stateless (external cron)

`--at` is HH:MM; `--daily` fires every day at that time, `--dow N` fires
weekly (cron 0=Sunday), default daily. External cron users call
`run-due` on their own cadence and get identical idempotence.
"""

from __future__ import annotations

import typer

from fenris.config import load_config
from fenris.db import connect

app = typer.Typer(help="Scheduled Drive digests: what changed, pushed to you.", no_args_is_help=True)
schedule_app = typer.Typer(help="Manage digest schedules.", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")

_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _open():
    cfg = load_config()
    return connect(cfg.db_path)


@app.command()
def now(
    days: float = typer.Option(7.0, "--days", "-w", help="Window size in days (default 7)."),
) -> None:
    """Compute and print one digest right now (also persists it)."""
    from fenris.digest import compute_digest, persist_digest

    conn = _open()
    try:
        digest = compute_digest(conn, days)
        persist_digest(conn, digest)
        typer.echo(digest.markdown)
    finally:
        conn.close()


@app.command()
def list(
    limit: int = typer.Option(10, "--limit", "-n", help="How many runs to show."),
) -> None:
    """Show recent persisted digest runs (one summary line each)."""
    from fenris.digest import recent_digests

    conn = _open()
    try:
        runs = recent_digests(conn, limit)
        if not runs:
            typer.echo("no digests yet - run `fenris digest now`")
            return
        for r in runs:
            c = r["counts"]
            typer.echo(
                f"[{r['id']:>3}] {r['generated_at']}"
                f"  new {c.get('new', 0)} · upd {c.get('updated', 0)}"
                f" · rm {c.get('removed', 0)} · prob {c.get('problems', 0)}"
                f" · skip {c.get('skipped', 0)}"
            )
    finally:
        conn.close()


@schedule_app.command("ls")
def schedule_ls() -> None:
    """List digest schedules."""
    from fenris.digest import list_schedules

    conn = _open()
    try:
        rows = list_schedules(conn)
        if not rows:
            typer.echo("no schedules - add one: fenris digest schedule add NAME --at 08:00")
            return
        for s in rows:
            state = "on " if s.enabled else "off"
            last = s.last_run_at or "never"
            typer.echo(
                f"[{s.id}] {state} {s.name!r} at {s.cron_hour}:{s.cron_minute:>2}"
                f" dow={s.cron_dow} window={s.window_days:g}d last={last}"
            )
    finally:
        conn.close()


@schedule_app.command("add")
def schedule_add(
    name: str = typer.Argument(..., help="Schedule name (shows in digests and logs)."),
    at: str = typer.Option(..., "--at", help="Fire time as HH:MM (24h)."),
    daily: bool = typer.Option(True, "--daily/--weekly", help="Fire daily (default) or weekly."),
    dow: str = typer.Option("mon", "--dow", help="Weekly day: sun,mon,tue,wed,thu,fri,sat."),
    days: float = typer.Option(7.0, "--days", "-w", help="Digest window in days (default 7)."),
) -> None:
    """Add a schedule: `--at 08:00 --weekly --dow mon` = Monday 8:00."""
    from fenris.digest import add_schedule

    hh, mm = _parse_hhmm(at)
    dow_field = "*" if daily else _dow_field(dow)
    conn = _open()
    try:
        sid = add_schedule(conn, name, mm, hh, dow_field, days)
    except ValueError as e:
        typer.echo(f"invalid schedule: {e}")
        raise typer.Exit(code=2)
    finally:
        conn.close()
    when = "daily" if daily else f"weekly {dow_field}"
    typer.echo(f"schedule {sid} added: {name!r} {when} at {at} (window {days:g}d)")


@schedule_app.command("rm")
def schedule_rm(id: int) -> None:
    """Remove a schedule by id."""
    from fenris.digest import delete_schedule

    conn = _open()
    try:
        if delete_schedule(conn, id):
            typer.echo(f"schedule {id} removed")
        else:
            typer.echo(f"no schedule {id}")
            raise typer.Exit(code=1)
    finally:
        conn.close()


@schedule_app.command("on")
def schedule_on(id: int) -> None:
    """Enable a schedule."""
    _set_enabled(id, True)


@schedule_app.command("off")
def schedule_off(id: int) -> None:
    """Disable a schedule."""
    _set_enabled(id, False)


def _set_enabled(id: int, enabled: bool) -> None:
    from fenris.digest import set_schedule_enabled

    conn = _open()
    try:
        if set_schedule_enabled(conn, id, enabled):
            typer.echo(f"schedule {id} {'enabled' if enabled else 'disabled'}")
        else:
            typer.echo(f"no schedule {id}")
            raise typer.Exit(code=1)
    finally:
        conn.close()


def _parse_hhmm(at: str) -> tuple[str, str]:
    try:
        hh, mm = at.split(":", 1)
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
    except ValueError:
        typer.echo(f"invalid --at {at!r}: use HH:MM (24h)")
        raise typer.Exit(code=2)
    return hh, mm


def _dow_field(dow: str) -> str:
    d = dow.strip().lower()[:3]
    if d not in _DOW_NAMES:
        typer.echo(f"invalid --dow {dow!r}: use one of {', '.join(_DOW_NAMES)}")
        raise typer.Exit(code=2)
    return str(_DOW_NAMES.index(d))


@app.command("run-due")
def run_due_cmd() -> None:
    """The scheduler's tick, stateless: run everything due right now.

    Safe to call as often as you like (idempotent within a minute). This
    is the external-cron entry point - identical logic to the built-in
    scheduler thread.
    """
    from fenris.digest import run_due

    conn = _open()
    try:
        ran = run_due(conn)
        if not ran:
            typer.echo("nothing due")
            return
        for r in ran:
            c = r["counts"]
            typer.echo(
                f"digest: {r['name']}"
                f" (new {c.get('new', 0)} · upd {c.get('updated', 0)}"
                f" · rm {c.get('removed', 0)} · prob {c.get('problems', 0)}"
                f" · skip {c.get('skipped', 0)})"
            )
    finally:
        conn.close()
