# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/wax/hashing.py), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: adds the WAX_HASH=off diagnostic switch and its placeholder hash
# (Foundation-written, for the engine bake-off). The hash computation is unchanged.
"""Event hash computation (WAX-0013 Event Chain Integrity and Tamper-Evidence).

Stands in for the Wax Security Module, which owns this in the design
(WAX-0015). The hash covers the fields WAX-0013 lists plus the prior hash.
"""

import hashlib
import json
import os
from datetime import UTC

# A bake-off diagnostic, not a mode Wax has: with WAX_HASH=off, writers skip
# reading the previous hash and computing a new one, and store this
# placeholder instead, so a run shows what hashing costs. verify-chain fails
# on such events, and the harness says so.
HASHING = os.environ.get("WAX_HASH", "on") != "off"
PLACEHOLDER_HASH = "0" * 64


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
