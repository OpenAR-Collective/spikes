"""Write derived-uuids.json: names and the UUID derived from each, as
WORKLOADS.md defines it, so both sides derive the same keys. Run with any
Python 3 from any directory:

    python engine-bakeoff/shared/make_derived_uuids.py
"""

import hashlib
import json
import pathlib
import uuid

HERE = pathlib.Path(__file__).resolve().parent


def derived_uuid(name):
    return uuid.UUID(bytes=hashlib.sha256(name.encode()).digest()[:16], version=4)


NAMES = [
    "bakeoff:account:0",
    "bakeoff:account:1",
    "bakeoff:account:499999",
    "bakeoff:seeder",
    "bakeoff:payment:3f2b8c1e-5d4a-4e6f-9a1b-2c3d4e5f6a7b:00000000-0000-4000-8000-000000000001",
    "pending:9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a",
    "",
]

vectors = [{"name": name, "uuid": str(derived_uuid(name))} for name in NAMES]
(HERE / "derived-uuids.json").write_text(json.dumps(vectors, indent=2) + "\n", encoding="ascii", newline="\n")
print(f"{len(vectors)} vectors written")
