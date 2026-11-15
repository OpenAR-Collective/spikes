#!/bin/sh
# Checks whether a plain SELECT last_value FROM a sequence can return a number
# that nextval has not handed out yet: the value 32 ahead that nextval writes
# into the tuple while it logs the sequence to WAL, before it sets the real
# value back. Round 2's diagnostic pass ran it to check the cause of run 2's
# account-summary gate failure.
#
# A throwaway container from the bake-off database's own image, nothing
# mounted: 16 pgbench clients loop nextval on a CACHE 1 sequence while one
# session loops "plain read, then pg_sequence_last_value()". The second read
# comes later and the sequence only rises, so a plain read above it is a
# number not yet handed out. Prints the count and the largest gap.
#
# A second block, under the same load, checks both methods against the same
# test: take a reading, then call nextval. Every number handed out before the
# reading is below that nextval, so a reading at or above it was not yet
# handed out when read. The two methods alternate in one loop; the plain
# read's hits are the positive control, and the locked read should have none.
# A plain count of zero means the run found nothing, which neither proves nor
# disproves the mechanism.
#
#     sh seq_race.sh [seconds]
#
# It takes the image from the csharp-wax side's database container, so prepare
# that side first. Its output in round 2's diagnostic pass is
# round2-diag.seq-race.txt.

# One instance at a time: concurrent copies share the container name and
# remove each other's containers.
LOCK="$(dirname "$0")/seq_race.lock"
mkdir "$LOCK" 2>/dev/null || { echo "SEQ RACE NOT RUN: another copy holds $LOCK"; exit 1; }
SECONDS_TO_RUN=${1:-90}
NAME=seqrace-pg
trap 'docker rm -f "$NAME" >/dev/null 2>&1; rmdir "$LOCK" 2>/dev/null' EXIT
IMAGE=$(docker inspect -f '{{.Config.Image}}' hivear-bakeoff-csharp-wax-db-1) || exit 1
docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" -e POSTGRES_USER=race -e POSTGRES_PASSWORD=local-evaluation-only "$IMAGE" >/dev/null || exit 1
n=0
# Probe over TCP: the image first runs a setup server on the Unix socket only,
# then restarts, so a socket probe can pass before the real server is up.
until docker exec "$NAME" pg_isready -h 127.0.0.1 -U race -q 2>/dev/null && docker exec "$NAME" psql -U race -d race -Atqc "SELECT 1" >/dev/null 2>&1; do
    n=$((n + 1)); [ $n -gt 60 ] && { echo "SEQ RACE: database not ready"; docker rm -f "$NAME" >/dev/null; exit 1; }; sleep 2
done
echo "image $IMAGE, $(docker exec "$NAME" psql -U race -d race -Atqc 'SHOW server_version')"
docker exec "$NAME" psql -U race -d race -Atqc "CREATE SEQUENCE s CACHE 1; SELECT nextval('s');" >/dev/null
# MSYS_NO_PATHCONV: Git Bash would otherwise rewrite /tmp/... into a Windows
# path before docker sees it, and pgbench would exit at once, unseen.
MSYS_NO_PATHCONV=1 docker exec "$NAME" sh -c "echo \"SELECT nextval('s');\" > /tmp/nextval.sql"
MSYS_NO_PATHCONV=1 docker exec -d "$NAME" pgbench -U race -n -c 16 -j 4 -T $((2 * SECONDS_TO_RUN + 15)) -f /tmp/nextval.sql race
sleep 3
a=$(docker exec "$NAME" psql -U race -d race -Atqc "SELECT pg_sequence_last_value('s'::regclass)")
sleep 1
b=$(docker exec "$NAME" psql -U race -d race -Atqc "SELECT pg_sequence_last_value('s'::regclass)")
[ "${b:-0}" -gt "${a:-0}" ] || { echo "SEQ RACE NOT RUN: pgbench is not advancing the sequence ($a, then $b)"; exit 1; }
echo "load: the sequence went from $a to $b in about a second"
docker exec -i "$NAME" psql -U race -d race -Atq -v ON_ERROR_STOP=1 <<SQL
DO \$\$
DECLARE
    plain bigint; locked bigint; reads bigint := 0; ahead bigint := 0; largest bigint := 0;
    stop timestamptz := clock_timestamp() + interval '$SECONDS_TO_RUN seconds';
BEGIN
    WHILE clock_timestamp() < stop LOOP
        SELECT last_value INTO plain FROM s;
        locked := pg_sequence_last_value('s'::regclass);
        reads := reads + 1;
        IF plain > locked THEN
            ahead := ahead + 1;
            largest := greatest(largest, plain - locked);
        END IF;
    END LOOP;
    RAISE NOTICE 'reads %, plain read ahead of the locked read %, largest gap %, final %', reads, ahead, largest,
        pg_sequence_last_value('s'::regclass);
END
\$\$;
DO \$\$
DECLARE
    x bigint; n bigint; plain_reads bigint := 0; plain_ahead bigint := 0; plain_largest bigint := 0;
    locked_reads bigint := 0; locked_ahead bigint := 0; locked_largest bigint := 0;
    stop timestamptz := clock_timestamp() + interval '$SECONDS_TO_RUN seconds';
BEGIN
    WHILE clock_timestamp() < stop LOOP
        SELECT last_value INTO x FROM s;
        n := nextval('s');
        plain_reads := plain_reads + 1;
        IF x >= n THEN plain_ahead := plain_ahead + 1; plain_largest := greatest(plain_largest, x - n + 1); END IF;
        x := pg_sequence_last_value('s'::regclass);
        n := nextval('s');
        locked_reads := locked_reads + 1;
        IF x >= n THEN locked_ahead := locked_ahead + 1; locked_largest := greatest(locked_largest, x - n + 1); END IF;
    END LOOP;
    RAISE NOTICE 'against a following nextval: plain reads %, not yet handed out % (largest %); locked reads %, not yet handed out % (largest %)',
        plain_reads, plain_ahead, plain_largest, locked_reads, locked_ahead, locked_largest;
    IF plain_ahead = 0 THEN
        RAISE NOTICE 'the plain read was never caught ahead in this run, so it found nothing: that neither proves nor disproves the mechanism';
    END IF;
END
\$\$;
SQL
docker rm -f "$NAME" >/dev/null
echo "SEQ RACE DONE"
