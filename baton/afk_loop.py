"""AFK background loop: self-implement the top unblocked PRD when the active
project has seen no manual activity for its configured `afk_hours` window.

Kept as its own module (rather than folded into `app.py`/`session_runner.py`)
so `session_runner` never has to import this back -- this module imports
`session_runner` to fire an implement session, and importing it the other way
around would create a cycle.

The idle clock is in-memory only, keyed by project id, and reset via
`record_activity` -- both on a manual PRD-button click and whenever a
self-implement fires. It is intentionally not persisted: a Baton restart
resets everything to a clean slate, same as `session_runner._implement_queues`
and `app._active_project_id`.
"""

import asyncio
import time
from typing import Callable

from baton import db, session_runner

# project_id -> time.monotonic() timestamp of last activity (manual click or
# self-implement fire). Process-lifetime only.
_last_activity: dict[int, float] = {}


def record_activity(project_id: int) -> None:
    _last_activity[project_id] = time.monotonic()


async def check_once(
    project_id: int | None,
    cwd: str | None,
    fetch_prd_list: Callable[[str], list[dict]],
) -> None:
    """One AFK check for whichever project is (or was, at call time) active.

    `fetch_prd_list` is a callable `(cwd) -> list[{"number","title","blocked"}]`
    -- the caller wires this to the live `gh`-backed fetch in production, and
    to a stub in tests.

    No-ops if there's no active project, if idle time hasn't yet reached the
    project's `afk_hours` setting, or if there's no unblocked PRD. Otherwise
    fires `session_runner.start_or_queue_implement` for the top unblocked
    entry and resets this project's idle clock.
    """
    if project_id is None or cwd is None:
        return

    conn = db.get_connection()
    afk_hours = db.get_afk_hours(conn)
    last = _last_activity.setdefault(project_id, time.monotonic())
    elapsed_hours = (time.monotonic() - last) / 3600
    if elapsed_hours < afk_hours:
        return

    prds = fetch_prd_list(cwd)
    unblocked = [p for p in prds if not p["blocked"]]
    if not unblocked:
        return

    top = unblocked[0]
    # Reset before firing so a slow implement job can't cause a second fire
    # mid-flight on the next tick.
    record_activity(project_id)
    await session_runner.start_or_queue_implement(project_id, top["number"], top["title"], cwd)


async def run_forever(
    *,
    get_active_project_id: Callable[[], int | None],
    get_active_project_cwd: Callable[[], str | None],
    fetch_prd_list: Callable[[str], list[dict]],
    interval_seconds: float = 60,
) -> None:
    """The always-on loop, started once from `_lifespan`. Never lets a single
    bad tick (e.g. a transient `gh` failure) escape -- the loop must keep
    ticking indefinitely for the life of the app process."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await check_once(get_active_project_id(), get_active_project_cwd(), fetch_prd_list)
        except Exception:
            pass
