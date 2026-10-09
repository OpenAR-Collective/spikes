"""The Pending Event Register's poller (WAX-0027).

Foundation-written for the engine bake-off (WORKLOADS.md, "Rules"); not part
of Andy Troiano's prototype. The poller looks for due entries and sends one
fire command for each. It takes no lock itself: the fire command claims its
entry's row with SELECT ... FOR UPDATE SKIP LOCKED inside its own
transaction, so an entry that another poller is firing is passed over and no
entry fires twice.
"""

import logging
import time

from django.db import close_old_connections, connection

logger = logging.getLogger("wax.register")


def due_keys(limit):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pending_event_pk FROM core.pending_event"
            " WHERE status = 'pending' AND due_at <= clock_timestamp() ORDER BY due_at LIMIT %s",
            [limit],
        )
        return [row[0] for row in cursor.fetchall()]


def poll_once(fire, limit=500) -> int:
    """Send fire(key) for each due entry. Returns how many were due."""
    keys = due_keys(limit)
    for key in keys:
        fire(key)
    return len(keys)


def loop(fire, poll_seconds, stop=None, limit=500):
    logger.info("Register poller started")
    while stop is None or not stop.is_set():
        close_old_connections()
        try:
            due = poll_once(fire, limit)
        except Exception:
            logger.exception("Register poll failed; retrying")
            due = 0
            time.sleep(1)
        if due < limit:
            time.sleep(poll_seconds)
