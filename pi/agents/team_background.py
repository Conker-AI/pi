"""A team role step that runs on its own thread, for callers that cannot hold an HTTP call."""

import logging
import threading

from pi.agents import team_execution as teams

log = logging.getLogger(__name__)

# Role steps started with `background: true` that may run at once in this Pi.
MAX_BACKGROUND_STEPS = 2
_slots = threading.BoundedSemaphore(MAX_BACKGROUND_STEPS)


def execute_in_background(store, runtime, identity, body):
    """Record a role step, run it on its own thread and return the run at once (state running).

    Everything that can refuse a step (the cap, the run state, the limits) runs before the
    thread starts, so a refusal never leaves a thread behind. The caller reads the run until it
    is no longer running; a step that fails ends the run `blocked`, a restart `interrupted`."""
    if not _slots.acquire(blocking=False):
        teams._error("team_busy", "Other team steps are still running; wait for one to finish.")
    try:
        started = teams.begin(store, identity, body)
        if started is None:
            _slots.release()
            return teams.get(store, identity)
    except BaseException:
        _slots.release()
        raise

    def work():
        try:
            teams.finish_step(store, runtime, identity, started)
        except Exception as exc:
            # The step and run are already marked blocked; the type is enough for the log.
            log.error("Team step %s failed: %s", started.step_id, type(exc).__name__)
        finally:
            _slots.release()

    try:
        threading.Thread(target=work, name="pi-team-step", daemon=True).start()
    except BaseException:
        _slots.release()
        raise
    return teams.get(store, identity)
