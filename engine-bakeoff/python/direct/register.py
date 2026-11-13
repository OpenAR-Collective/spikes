"""The Pending Event Register's poller for the python-direct engine (WAX-0027).
Foundation-written.

It is wax/register.py's poller on a psycopg connection, with no Django. It
looks for due entries, oldest first, and sends one fire command for each
through the direct command path. It takes no lock itself: the fire command
claims its entry's row with SELECT ... FOR UPDATE SKIP LOCKED inside its own
transaction, so an entry that another poller is firing is passed over and no
entry fires twice. Partitions take the entries whose key hashes to them.
"""

import logging
import time

logger = logging.getLogger("direct.register")


def due_keys(conn, limit, partition=0, partitions=1):
    """Due entries, oldest first. With several pollers, each takes the
    entries whose key hashes to its partition, so they do not compete."""
    sql = "SELECT pending_event_pk FROM core.pending_event WHERE status = 'pending' AND due_at <= clock_timestamp()"
    params = []
    if partitions > 1:
        # Masked rather than abs(), which overflows for a hash of -2**31.
        sql += " AND (hashtext(pending_event_pk::text) & 2147483647) %% %s = %s"
        params += [partitions, partition]
    sql += " ORDER BY due_at LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def poll_once(conn, fire, limit=500, partition=0, partitions=1) -> int:
    """Send fire(key) for each due entry. Returns how many were due."""
    keys = due_keys(conn, limit, partition, partitions)
    for key in keys:
        fire(key)
    return len(keys)


def firing(session, actor_pk):
    """fire(key) for the engine: one FirePendingEvent through the direct
    command path, with a refusal logged rather than raised."""
    from direct.commands import CommandRejected, dispatch
    from hivear.commands import FirePendingEvent

    def fire(key):
        try:
            dispatch(session, FirePendingEvent(pending_event_pk=key), actor_pk=actor_pk, actor_type="system",
                     source="register")
        except CommandRejected as exc:
            logger.warning("fire %s refused: %s", key, exc)

    return fire


def run_forever(actor_pk, partition, partitions, poll_seconds, limit=500):
    """One register poller partition of the python-direct engine."""
    from direct import handlers  # noqa: F401  (registers the handlers)
    from direct.commands import Session
    from direct.db import connect

    session = fire = None
    while True:
        try:
            if session is None:
                session = Session(connect())
                fire = firing(session, actor_pk)
                logger.info("Register poller %s/%s started", partition, partitions)
            due = poll_once(session.conn, fire, limit, partition, partitions)
        except Exception:
            logger.exception("Register poll failed; reconnecting")
            try:
                session.conn.close()
            except Exception:
                pass
            session = fire = None
            due = 0
            time.sleep(1)
        if due < limit:
            time.sleep(poll_seconds)
