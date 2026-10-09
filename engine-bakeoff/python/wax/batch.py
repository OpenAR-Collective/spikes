"""Batch jobs as WAX-0024 describes them: a coordinator divides the work into
chunks, and workers claim chunks with SELECT ... FOR UPDATE SKIP LOCKED and
run one command per key through the normal command path.

Foundation-written for the engine bake-off (WORKLOADS.md, "Batch
workloads"); not part of Andy Troiano's prototype.

Chunk boundaries are computed in PostgreSQL, in its own uuid order, and a
chunk's keys are read back with BETWEEN in the same order. Sorting keys in
application code instead could disagree with the database's order and lose
or repeat keys at the boundaries.
"""

import uuid

from django.db import connection, transaction

CHUNK_SIZE = 1000


def create_run(job_name, table, key_column, chunk_size=CHUNK_SIZE, run_pk=None):
    """Create a run and its chunks over every key in table. table and
    key_column are trusted identifiers supplied by code, never by input."""
    run_pk = run_pk or uuid.uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.batch_job_run (batch_job_run_pk, job_name) VALUES (%s, %s)", [run_pk, job_name]
        )
        cursor.execute(
            f"""
            WITH numbered AS (
                SELECT {key_column} AS k, (row_number() OVER (ORDER BY {key_column}) - 1) / %s AS n FROM {table}
            )
            INSERT INTO core.batch_job_chunk (batch_job_run_fk, chunk_sequence_number, range_start, range_end, status)
            SELECT %s, first.n, first.k, last.k, 'pending'
            FROM (SELECT DISTINCT ON (n) n, k FROM numbered ORDER BY n, k) first
            JOIN (SELECT DISTINCT ON (n) n, k FROM numbered ORDER BY n, k DESC) last USING (n)
            """,
            [chunk_size, run_pk],
        )
        cursor.execute("SELECT count(*) FROM core.batch_job_chunk WHERE batch_job_run_fk = %s", [run_pk])
        chunks = cursor.fetchone()[0]
    return run_pk, chunks


def claim_chunk(run_pk, instance, lease_seconds=600):
    """Claim the next pending chunk, or one whose claim has lapsed. Returns
    (chunk_pk, range_start, range_end), or None when nothing is left."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT batch_job_chunk_pk, range_start, range_end FROM core.batch_job_chunk"
            " WHERE batch_job_run_fk = %s"
            " AND (status = 'pending' OR (status = 'claimed' AND claim_expires_at < clock_timestamp()))"
            " ORDER BY chunk_sequence_number LIMIT 1 FOR UPDATE SKIP LOCKED",
            [run_pk],
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            "UPDATE core.batch_job_chunk SET status = 'claimed', claimed_by_instance = %s,"
            " claim_expires_at = clock_timestamp() + make_interval(secs => %s) WHERE batch_job_chunk_pk = %s",
            [instance, lease_seconds, row[0]],
        )
    return row


def chunk_keys(table, key_column, range_start, range_end):
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {key_column} FROM {table} WHERE {key_column} BETWEEN %s AND %s ORDER BY {key_column}",
            [range_start, range_end],
        )
        return [row[0] for row in cursor.fetchall()]


def complete_chunk(chunk_pk):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.batch_job_chunk SET status = 'complete', completed_at = clock_timestamp()"
            " WHERE batch_job_chunk_pk = %s",
            [chunk_pk],
        )


def fail_chunk(chunk_pk, detail):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.batch_job_chunk SET status = 'failed', error_detail = %s WHERE batch_job_chunk_pk = %s",
            [str(detail)[:2000], chunk_pk],
        )


def finish_run(run_pk):
    """Record the run's end if every chunk is complete. Returns whether it is."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.batch_job_run SET finished_at = clock_timestamp() WHERE batch_job_run_pk = %s"
            " AND NOT EXISTS (SELECT 1 FROM core.batch_job_chunk WHERE batch_job_run_fk = %s AND status <> 'complete')",
            [run_pk, run_pk],
        )
        return cursor.rowcount == 1
