"""Pin wax.hashing to the bake-off's shared hash vectors.

engine-bakeoff/shared/hash-vectors.json holds event hash inputs and their
expected hashes. The C# side's tests require the same hashes, so both sides
are held to one canonical form, and a change to either side's hashing fails a
test instead of silently splitting the two.

The file is found at ../../shared/ from this test, which is
engine-bakeoff/shared/ in the source tree and /shared/ in the tools container,
where docker-compose.yml mounts it read-only. HASH_VECTORS overrides the path.
A missing file fails the test rather than skipping it.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from wax.hashing import compute_event_hash

VECTORS = Path(os.environ.get("HASH_VECTORS", Path(__file__).resolve().parents[2] / "shared" / "hash-vectors.json"))


def _vectors():
    if not VECTORS.exists():
        return [pytest.param(None, id="hash-vectors.json not found")]
    return [pytest.param(vector, id=vector["name"]) for vector in json.loads(VECTORS.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("vector", _vectors())
def test_event_hash_matches_shared_vector(vector):
    assert vector is not None, f"{VECTORS} is missing; the hash vectors pin both sides to one canonical form"
    arguments = {key: value for key, value in vector.items() if key not in ("name", "expected_hash")}
    arguments["occurred_at"] = datetime.fromisoformat(vector["occurred_at"])
    assert compute_event_hash(**arguments) == vector["expected_hash"]
