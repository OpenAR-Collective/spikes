"""Times one event insert in csharp-wax's database straight after DISCARD ALL
and with the session's plans warm, the check that opens round 2's diagnostic
pass. It copies the seeded template to a throwaway database, builds one
insert from a seeded event (the next sequence number on its aggregate, with
a reason code so the foreign-key check runs), and runs it under EXPLAIN
ANALYZE inside a transaction that is rolled back, in rounds of warm inserts
and of inserts each preceded by DISCARD ALL, after one unrecorded round that
brings the copy's pages into memory. It prints the medians of execution time
and of the two triggers' times, then drops the copy.

    py -3 reset_check.py [accounts] [rounds] [inserts per round]

The defaults are 500,000 accounts, 3 rounds, and 25 inserts per round.
"""

import re
import statistics
import subprocess
import sys

CONTAINER = "hivear-bakeoff-csharp-wax-db-1"
ACCOUNTS = int(sys.argv[1]) if len(sys.argv) > 1 else 500_000
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
PER_ROUND = int(sys.argv[3]) if len(sys.argv) > 3 else 25
TEMPLATE = f"bakeoff_seed_{ACCOUNTS}"
COPY = "reset_check"

BUILD = r"""
SELECT format('INSERT INTO core.event (event_pk, aggregate_sequence_number, event_type, schema_version,'
    ' aggregate_pk_ref, aggregate_type, occurred_at, actor_pk_ref, actor_type, prior_value, new_value,'
    ' reason_code_fk, source, correlation_pk_ref, correlation_type, metadata, prior_event_hash, event_hash)'
    ' VALUES (gen_random_uuid(), %s, %L, %s, %L, %L, now(), %L, %L, %L::jsonb, %L::jsonb, %L, %L, NULL, NULL,'
    ' %L::jsonb, %L, %L)',
    e.aggregate_sequence_number + 1, e.event_type, e.schema_version, e.aggregate_pk_ref, e.aggregate_type,
    e.actor_pk_ref, e.actor_type, e.prior_value, e.new_value,
    coalesce(e.reason_code_fk, (SELECT reference_value_pk FROM core.reference_value ORDER BY 1 LIMIT 1)),
    e.source, e.metadata, e.event_hash, 'reset-check') AS ins,
    coalesce(e.reason_code_fk, (SELECT reference_value_pk FROM core.reference_value ORDER BY 1 LIMIT 1)) IS NOT NULL
        AS has_reason
FROM core.event e
WHERE e.aggregate_sequence_number = (SELECT max(aggregate_sequence_number) FROM core.event x
                                     WHERE x.aggregate_pk_ref = e.aggregate_pk_ref)
ORDER BY e.global_sequence_number DESC LIMIT 1 \gset
"""


def psql(database, sql):
    result = subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-U", "hivear", "-d", database, "-X", "-q",
                             "-v", "ON_ERROR_STOP=1", "-f", "-"], input=sql, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"psql failed:\n{result.stderr}")
    return result.stdout


def session(reset):
    """One psql session: the insert once untimed, so both kinds start from a
    session that has run it, then PER_ROUND timed inserts. :ins is the psql
    variable BUILD sets."""
    one = ("DISCARD ALL;\n" if reset else "") + "BEGIN;\nEXPLAIN (ANALYZE, COSTS OFF) :ins;\nROLLBACK;\n"
    out = psql(COPY, BUILD + "BEGIN;\n:ins;\nROLLBACK;\n" + one * PER_ROUND)
    timings = {"execution": [], "before_insert": [], "foreign_key": []}
    for block in out.split("Execution Time:")[:-1]:
        before = re.search(r"Trigger trg_event_before_insert: time=([\d.]+)", block)
        fk = re.search(r"Trigger for constraint \S+: time=([\d.]+)", block)
        timings["before_insert"].append(float(before.group(1)) if before else None)
        timings["foreign_key"].append(float(fk.group(1)) if fk else None)
    timings["execution"] = [float(x) for x in re.findall(r"Execution Time: ([\d.]+) ms", out)]
    return timings


def main():
    psql("postgres", f"DROP DATABASE IF EXISTS {COPY} WITH (FORCE);\nCREATE DATABASE {COPY} TEMPLATE {TEMPLATE};\n")
    try:
        check = psql(COPY, BUILD + "\\echo has_reason=:has_reason\n")
        print(f"template {TEMPLATE}, {ROUNDS} rounds of {PER_ROUND} inserts each way; {check.strip()}")
        results = {"warm": {"execution": [], "before_insert": [], "foreign_key": []},
                   "after DISCARD ALL": {"execution": [], "before_insert": [], "foreign_key": []}}
        # A session that is not recorded first, so the copy's pages are in
        # memory before anything is timed; then the rounds, alternating which
        # kind goes first, so neither always runs on the colder cache.
        session(False)
        for n in range(ROUNDS):
            kinds = (("warm", False), ("after DISCARD ALL", True))
            for kind, reset in (kinds if n % 2 == 0 else kinds[::-1]):
                for key, values in session(reset).items():
                    results[kind][key] += [v for v in values if v is not None]
        print("Median milliseconds per insert, with the count of inserts timed:")
        print("| Insert | Execution | BEFORE INSERT trigger | Foreign-key check |")
        print("| --- | --- | --- | --- |")
        for kind, timings in results.items():
            cells = [f"{statistics.median(v):.3f} ({len(v)})" if v else "none" for v in timings.values()]
            print(f"| {kind} | " + " | ".join(cells) + " |")
    finally:
        psql("postgres", f"DROP DATABASE IF EXISTS {COPY} WITH (FORCE);\n")


if __name__ == "__main__":
    main()
