# Shared

Files both sides of the bake-off use.

- `hash-vectors.json` holds event hash inputs and the hash the Python side's `wax/hashing.py` computes for each. The C# side's tests require an exact match, which shows the two languages hash the same bytes for the same event. The vectors cover non-ASCII text, characters outside the Basic Multilingual Plane, control characters, key order, and time zone offsets.
- `make_hash_vectors.py` regenerates the vectors from the Python side's code: `python shared/make_hash_vectors.py`.
- `derived-uuids.json` holds names and the UUID each derives to, under the rule in [WORKLOADS.md](../WORKLOADS.md). Both sides check their key derivation against it, so they seed the same accounts. `make_derived_uuids.py` regenerates it.
