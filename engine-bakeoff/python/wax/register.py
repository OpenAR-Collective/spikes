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


def due_keys(limit, partition=0, partitions=1):
    """Due entries, oldest first. With several pollers, each takes the
    entries whose key hashes to its partition, so they do not compete."""
    sql = "SELECT pending_event_pk FROM core.pending_event WHERE status = 'pending' AND due_at <= clock_timestamp()"
    params = []
    if partitions > 1:
        sql += " AND abs(hashtext(pending_event_pk::text)) %% %s = %s"
        params += [partitions, partition]
    sql += " ORDER BY due_at LIMIT %s"
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [row[0] for row in cursor.fetchall()]


def poll_once(fire, limit=500, partition=0, partitions=1) -> int:
    """Send fire(key) for each due entry. Returns how many were due."""
    keys = due_keys(limit, partition, partitions)
    for key in keys:
        fire(key)
    return len(keys)


def loop(fire, poll_seconds, stop=None, limit=500, partition=0, partitions=1):
    logger.info("Register poller %s/%s started", partition, partitions)
    while stop is None or not stop.is_set():
        close_old_connections()
        try:
            due = poll_once(fire, limit, partition, partitions)
        except Exception:
            logger.exception("Register poll failed; retrying")
            due = 0
            time.sleep(1)
        if due < limit:
            time.sleep(poll_seconds)
