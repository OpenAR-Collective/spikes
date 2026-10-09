"""Write hash-vectors.json: event hash inputs and the hash the Python side's
wax/hashing.py computes for each, so the C# side can prove it hashes the same
bytes. Run with Python 3.11 or later from any directory:

    python engine-bakeoff/shared/make_hash_vectors.py
"""

import importlib.util
import json
import pathlib
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hashing", HERE.parent / "python" / "wax" / "hashing.py")
hashing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hashing)

ACCOUNT = "3f2b8c1e-5d4a-4e6f-9a1b-2c3d4e5f6a7b"
ACTOR = "00000000-0000-4000-8000-000000000001"
REASON = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"

CASES = [
    {
        "name": "first event of an account",
        "aggregate_type": "account",
        "aggregate_pk_ref": ACCOUNT,
        "event_type": "account.placed",
        "schema_version": 1,
        "prior_value": None,
        "new_value": {
            "client_account_number": "A-1",
            "status": "active",
            "is_disputed": False,
            "list_date": "2026-01-15",
            "balance_buckets": {"principal": 100000, "interest": 5000},
        },
        "occurred_at": "2026-10-08T14:03:05.123456+00:00",
        "actor_pk_ref": ACTOR,
        "actor_type": "user",
        "reason_code_fk": REASON,
        "prior_event_hash": None,
    },
    {
        "name": "a later event, an offset time, and whole seconds",
        "aggregate_type": "account",
        "aggregate_pk_ref": ACCOUNT,
        "event_type": "account.payment_applied",
        "schema_version": 1,
        "prior_value": {"balance_buckets": {"interest": 5000, "fee": 2000, "principal": 100000}},
        "new_value": {"balance_buckets": {"interest": 0, "fee": 0, "principal": 99000}},
        "occurred_at": "2026-10-08T09:03:05-05:00",
        "actor_pk_ref": ACTOR,
        "actor_type": "batch",
        "reason_code_fk": None,
        "prior_event_hash": "0" * 64,
    },
    {
        "name": "text that needs escaping",
        "aggregate_type": "account",
        "aggregate_pk_ref": ACCOUNT,
        "event_type": "account.noted",
        "schema_version": 2,
        "prior_value": {"note": "plain"},
        "new_value": {
            "note": "Zo" + chr(0xEB) + " said \"hi\" \\ then\ttabbed\nand left / " + chr(0x7F) + " " + chr(0x01),
            "cjk": chr(0x65E5) + chr(0x672C),
            "astral": "\U0001f600",
            "empty": "",
        },
        "occurred_at": "1999-12-31T23:59:59.000001+00:00",
        "actor_pk_ref": ACTOR,
        "actor_type": "system",
        "reason_code_fk": REASON,
        "prior_event_hash": "a" * 64,
    },
    {
        "name": "nesting, ordering, and the edges of numbers",
        "aggregate_type": "payment",
        "aggregate_pk_ref": ACCOUNT,
        "event_type": "payment.posted",
        "schema_version": 1,
        "prior_value": None,
        "new_value": {
            "b": [3, 1, 2, [], {}, None, True, False],
            "a": {"z": 1, "Z": 2, "_": 3, "aa": 4, "a": 5, chr(0xE9): 6, chr(0x1F600): 7, chr(0xFF5E): 8},
            "A": 1000000000000000,
            "negative": -42,
            "zero": 0,
        },
        "occurred_at": "2026-02-28T23:30:00.500000-08:00",
        "actor_pk_ref": ACTOR,
        "actor_type": "integration",
        "reason_code_fk": None,
        "prior_event_hash": None,
    },
]

vectors = []
for case in CASES:
    arguments = {key: value for key, value in case.items() if key != "name"}
    arguments["occurred_at"] = datetime.fromisoformat(case["occurred_at"])
    vectors.append({**case, "expected_hash": hashing.compute_event_hash(**arguments)})

(HERE / "hash-vectors.json").write_text(json.dumps(vectors, indent=2, ensure_ascii=True) + "\n", encoding="ascii", newline="\n")
print(f"{len(vectors)} vectors written")
