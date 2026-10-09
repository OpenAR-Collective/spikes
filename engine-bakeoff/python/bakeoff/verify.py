"""verify-chain: recompute every hash chain in one ordered pass.
Foundation-written.

The prototype's verify_chain command checks one aggregate at a time, a query
each, which is slow at the bake-off's sizes. This streams core.event once in
(aggregate, sequence) order through a server-side cursor and applies the same
three checks as the prototype's wax.store.verify_stream: no gap in an
aggregate's sequence, each prior_event_hash equals its predecessor's hash,
and each event's hash equals the hash recomputed from its content.
"""

import json
import time

from django.db import connection

from wax.hashing import compute_event_hash

QUERY = (
    "SELECT aggregate_pk_ref, aggregate_sequence_number, aggregate_type, event_type, schema_version,"
    " prior_value, new_value, occurred_at, actor_pk_ref, actor_type, reason_code_fk, prior_event_hash, event_hash"
    " FROM core.event ORDER BY aggregate_pk_ref, aggregate_sequence_number"
)


def _json(value):
    # Django loads jsonb as text on its connections.
    return json.loads(value) if isinstance(value, str) else value


def verify_chain(report_limit=20) -> dict:
    started = time.perf_counter()
    connection.ensure_connection()
    raw = connection.connection
    problems, aggregates, events = [], 0, 0
    current, expected, chain_prior = None, 0, None
    with raw.transaction(), raw.cursor(name="bakeoff_verify_chain") as cursor:
        cursor.itersize = 20_000
        cursor.execute(QUERY)
        for (aggregate_pk, sequence, aggregate_type, event_type, schema_version, prior_value, new_value,
             occurred_at, actor_pk, actor_type, reason_pk, prior_hash, event_hash) in cursor:
            if aggregate_pk != current:
                current, expected, chain_prior = aggregate_pk, 0, None
                aggregates += 1
            expected += 1
            if sequence != expected:
                problems.append(f"{aggregate_pk}: gap before sequence {sequence}")
                expected = sequence
            if prior_hash != chain_prior:
                problems.append(f"{aggregate_pk}: sequence {sequence}: prior_event_hash does not match predecessor")
            recomputed = compute_event_hash(
                aggregate_type=aggregate_type,
                aggregate_pk_ref=aggregate_pk,
                event_type=event_type,
                schema_version=schema_version,
                prior_value=_json(prior_value),
                new_value=_json(new_value),
                occurred_at=occurred_at,
                actor_pk_ref=actor_pk,
                actor_type=actor_type,
                reason_code_fk=reason_pk,
                prior_event_hash=prior_hash,
            )
            if recomputed != event_hash:
                problems.append(f"{aggregate_pk}: sequence {sequence}: event_hash does not match content")
            chain_prior = event_hash
            events += 1
    return {
        "workload": "verify-chain",
        "aggregates": aggregates,
        "events": events,
        "problems": len(problems),
        "ok": not problems,
        "first_problems": problems[:report_limit],
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
