"""Event hash computation (WAX-0013 Event Chain Integrity and Tamper-Evidence).

Stands in for the Wax Security Module, which owns this in the design
(WAX-0015). The hash covers the fields WAX-0013 lists plus the prior hash.
"""

import hashlib
import json
from datetime import UTC


def compute_event_hash(
    *,
    aggregate_type,
    aggregate_pk_ref,
    event_type,
    schema_version,
    prior_value,
    new_value,
    occurred_at,
    actor_pk_ref,
    actor_type,
    reason_code_fk,
    prior_event_hash,
) -> str:
    canonical = json.dumps(
        {
            "aggregate_type": aggregate_type,
            "aggregate_pk_ref": str(aggregate_pk_ref),
            "event_type": event_type,
            "schema_version": int(schema_version),
            "prior_value": prior_value,
            "new_value": new_value,
            "occurred_at": occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "actor_pk_ref": str(actor_pk_ref),
            "actor_type": actor_type,
            "reason_code_fk": str(reason_code_fk) if reason_code_fk else None,
            "prior_event_hash": prior_event_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
