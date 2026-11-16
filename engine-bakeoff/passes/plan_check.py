"""Times csharp-wax's event insert as a prepared statement under each of
PostgreSQL's plan_cache_mode settings: the check that opens round 2's
follow-up tests. The fix pass found the insert cheaper per row with custom
plans forced. The insert's own plan is trivial, and its BEFORE INSERT trigger
runs no parameterized query, but the foreign-key check runs one whose plan
each session caches apart from the statement's, and plan_cache_mode governs
that plan too. This check shows where the difference sits.

It copies the seeded template to a throwaway database and builds the insert
reset_check.py builds (the next sequence number on a seeded event's
aggregate, with a reason code so the foreign-key check runs), with its
values as parameters, as a prepared statement. In one session per mode it
prepares the insert, runs it enough times to settle its plan choice and the
foreign-key check's, then runs it under EXPLAIN ANALYZE inside a transaction
that is rolled back, in rounds that alternate the order of the modes, after
one unrecorded session that brings the copy's pages into memory. It prints
the medians of execution time and of the two triggers' times, and the
prepared statement's generic and custom plan counts, then drops the copy.

    py -3 plan_check.py [accounts] [rounds] [inserts per round]

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
COPY = "plan_check"
MODES = ["auto", "force_custom_plan", "force_generic_plan"]
# Enough uses to pass the five custom plans PostgreSQL tries before it
# considers a generic one, for the statement and the foreign-key check alike.
SETTLE = 8

PREPARE = (
    "PREPARE ins AS INSERT INTO core.event (event_pk, aggregate_sequence_number, event_type, schema_version,"
    " aggregate_pk_ref, aggregate_type, occurred_at, actor_pk_ref, actor_type, prior_value, new_value,"
    " reason_code_fk, source, correlation_pk_ref, correlation_type, metadata, prior_event_hash, event_hash)"
    " VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, now(), $6, $7, $8, $9, $10, $11, NULL, NULL, $12, $13, $14);\n"
)

BUILD = r"""
SELECT format('EXECUTE ins(%s, %L, %s, %L, %L, %L, %L, %L, %L, %L, %L, %L, %L, %L)',
    e.aggregate_sequence_number + 1, e.event_type, e.schema_version, e.aggregate_pk_ref, e.aggregate_type,
    e.actor_pk_ref, e.actor_type, e.prior_value, e.new_value,
    coalesce(e.reason_code_fk, (SELECT reference_value_pk FROM core.reference_value ORDER BY 1 LIMIT 1)),
    e.source, e.metadata, e.event_hash, 'plan-check') AS exe,
    coalesce(e.reason_code_fk, (SELECT reference_value_pk FROM core.reference_value ORDER BY 1 LIMIT 1)) IS NOT NULL
        AS has_reason
FROM core.event e
WHERE e.aggregate_sequence_number = (SELECT max(aggregate_sequence_number) FROM core.event x
                                     WHERE x.aggregate_pk_ref = e.aggregate_pk_ref)
ORDER BY e.global_sequence_number DESC LIMIT 1 \gset
"""

COUNTS = ("SELECT generic_plans, custom_plans FROM pg_prepared_statements WHERE name = 'ins' \\gset\n"
          "\\echo counts :generic_plans :custom_plans\n")


def psql(database, sql):
    result = subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-U", "hivear", "-d", database, "-X", "-q",
                             "-v", "ON_ERROR_STOP=1", "-f", "-"], input=sql, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"psql failed:\n{result.stderr}")
    return result.stdout


def session(mode):
    """One psql session under one mode: the insert prepared, settled with
    SETTLE uses, then PER_ROUND timed uses. :exe is the psql variable BUILD sets."""
    settle = "BEGIN;\n:exe;\nROLLBACK;\n" * SETTLE
    timed = "BEGIN;\nEXPLAIN (ANALYZE, COSTS OFF) :exe;\nROLLBACK;\n" * PER_ROUND
    out = psql(COPY, f"SET plan_cache_mode = {mode};\n" + BUILD + PREPARE + settle + timed + COUNTS)
    timings = {"execution": [], "before_insert": [], "foreign_key": []}
    for block in out.split("Execution Time:")[:-1]:
        before = re.search(r"Trigger trg_event_before_insert: time=([\d.]+)", block)
        fk = re.search(r"Trigger for constraint \S+: time=([\d.]+)", block)
        timings["before_insert"].append(float(before.group(1)) if before else None)
        timings["foreign_key"].append(float(fk.group(1)) if fk else None)
    timings["execution"] = [float(x) for x in re.findall(r"Execution Time: ([\d.]+) ms", out)]
    counts = re.search(r"counts (\d+) (\d+)", out)
    return timings, (int(counts.group(1)), int(counts.group(2))) if counts else None


def main():
    psql("postgres", f"DROP DATABASE IF EXISTS {COPY} WITH (FORCE);\nCREATE DATABASE {COPY} TEMPLATE {TEMPLATE};\n")
    try:
        check = psql(COPY, BUILD + "\\echo has_reason=:has_reason\n")
        print(f"template {TEMPLATE}, {ROUNDS} rounds of {PER_ROUND} timed inserts per mode after {SETTLE} untimed;"
              f" {check.strip()}")
        session("auto")
        results = {mode: {"execution": [], "before_insert": [], "foreign_key": []} for mode in MODES}
        counts = {mode: [] for mode in MODES}
        for n in range(ROUNDS):
            # The order of the modes turns each round, so no mode always runs first.
            for mode in MODES[n % len(MODES):] + MODES[:n % len(MODES)]:
                timings, choice = session(mode)
                for key, values in timings.items():
                    results[mode][key] += [v for v in values if v is not None]
                counts[mode].append(choice)
        print()
        print("Median milliseconds per insert, with the count of inserts timed, and the prepared statement's plan"
              " choices in each session (generic, custom):")
        print("| plan_cache_mode | Execution | BEFORE INSERT trigger | Foreign-key check | Plan choices per session |")
        print("| --- | --- | --- | --- | --- |")
        for mode, timings in results.items():
            cells = [f"{statistics.median(v):.3f} ({len(v)})" if v else "none" for v in timings.values()]
            choices = ", ".join(f"{g} and {c}" if (g, c) != (None, None) else "none"
                                for g, c in (choice or (None, None) for choice in counts[mode]))
            print(f"| {mode} | " + " | ".join(cells) + f" | {choices} |")
    finally:
        psql("postgres", f"DROP DATABASE IF EXISTS {COPY} WITH (FORCE);\n")


if __name__ == "__main__":
    main()
