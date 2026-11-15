"""Times one event insert in csharp-wax's database straight after DISCARD ALL,
after each of the commands DISCARD ALL stands for, and with the session's
plans warm: the check that opens round 2's diagnostic pass and, with the
split, the pass that tests its fix. It copies the seeded template to a
throwaway database, builds one insert from a seeded event (the next sequence
number on its aggregate, with a reason code so the foreign-key check runs),
and runs it under EXPLAIN ANALYZE inside a transaction that is rolled back,
in rounds of warm inserts and of inserts each preceded by a reset, after one
unrecorded round that brings the copy's pages into memory. It prints the
medians of execution time and of the two triggers' times, then times each
reset command itself with psql's timing, which includes the round trip from
psql inside the container, and drops the copy.

PostgreSQL documents DISCARD ALL as CLOSE ALL; SET SESSION AUTHORIZATION
DEFAULT; RESET ALL; DEALLOCATE ALL; UNLISTEN *; SELECT
pg_advisory_unlock_all(); DISCARD PLANS; DISCARD TEMP; DISCARD SEQUENCES.
The split also times the same list without DEALLOCATE ALL and DISCARD PLANS,
a lighter reset that keeps a session's plans.

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


# The two kinds step 0 compares, then the split: each command DISCARD ALL
# stands for, alone, and the lighter reset without the two that drop plans.
STEP0 = {"warm": "", "after DISCARD ALL": "DISCARD ALL;"}
PARTS = ["CLOSE ALL;", "SET SESSION AUTHORIZATION DEFAULT;", "RESET ALL;", "DEALLOCATE ALL;", "UNLISTEN *;",
         "SELECT pg_advisory_unlock_all();", "DISCARD PLANS;", "DISCARD TEMP;", "DISCARD SEQUENCES;"]
LIGHT = " ".join(part for part in PARTS if part not in ("DEALLOCATE ALL;", "DISCARD PLANS;"))
LIGHT_NAME = "the list without DEALLOCATE ALL and DISCARD PLANS"
SPLIT = {**{f"after {part.rstrip(';')}": part for part in PARTS}, f"after {LIGHT_NAME}": LIGHT}


def session(reset):
    """One psql session: the insert once untimed, so every kind starts from a
    session that has run it, then PER_ROUND timed inserts, each after the
    reset commands given. :ins is the psql variable BUILD sets."""
    one = (reset + "\n" if reset else "") + "BEGIN;\nEXPLAIN (ANALYZE, COSTS OFF) :ins;\nROLLBACK;\n"
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
        # A session that is not recorded first, so the copy's pages are in
        # memory before anything is timed.
        session("")
        for title, kinds in (("Step 0", STEP0), ("The split", SPLIT)):
            results = {kind: {"execution": [], "before_insert": [], "foreign_key": []} for kind in kinds}
            # The rounds, reversing the order of the kinds each round, so no
            # kind always runs on the colder cache.
            for n in range(ROUNDS):
                order = list(kinds.items())
                for kind, reset in (order if n % 2 == 0 else order[::-1]):
                    for key, values in session(reset).items():
                        results[kind][key] += [v for v in values if v is not None]
            print()
            print(f"{title}: median milliseconds per insert, with the count of inserts timed:")
            print("| Insert | Execution | BEFORE INSERT trigger | Foreign-key check |")
            print("| --- | --- | --- | --- |")
            for kind, timings in results.items():
                cells = [f"{statistics.median(v):.3f} ({len(v)})" if v else "none" for v in timings.values()]
                print(f"| {kind} | " + " | ".join(cells) + " |")
        print()
        print(f"Each reset command's own time: median milliseconds from psql's timing over {ROUNDS * PER_ROUND} calls, "
              "with the round trip from psql inside the container:")
        print("| Command | Median ms |")
        print("| --- | --- |")
        calls = ROUNDS * PER_ROUND
        for command in ["DISCARD ALL;", *PARTS, LIGHT]:
            out = psql(COPY, "\\timing on\n" + (command + "\n") * calls)
            times = [float(x) for x in re.findall(r"Time: ([\d.]+) ms", out)]
            # psql times each statement; the lighter reset is several, so each
            # call's time is the sum of its parts.
            size = command.count(";")
            each = [sum(times[i:i + size]) for i in range(0, len(times), size)]
            if len(each) != calls:
                raise SystemExit(f"expected {calls} timings for {command}, read {len(each)}")
            print(f"| {LIGHT_NAME if command == LIGHT else command.rstrip(';')} | {statistics.median(each):.3f} |")
    finally:
        psql("postgres", f"DROP DATABASE IF EXISTS {COPY} WITH (FORCE);\n")


if __name__ == "__main__":
    main()
