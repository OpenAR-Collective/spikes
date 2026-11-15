"""The two reader fixes from the engine bake-off's round 2 diagnostic pass, in
Andy Troiano's reader (wax/projections.py) and the python-direct reader
(direct/reader.py). Foundation-written.

The high-water mark reads the sequence under its lock: a plain SELECT of
last_value can see, for a moment, a number 32 past any handed out, and a mark
built on it can pass open transactions' events. The race is too rare to
provoke in a test (a few times in tens of millions of reads under a heavy
nextval load), so these tests check the reads.

A claim never moves a checkpoint back: a pass looks at the checkpoint without
a lock, then claims it, and another reader on a later mark can move it in
between. Each reader has a hook that runs in that window.
"""

import time
import uuid

import pytest
from django.db import connection
from psycopg.types.json import Jsonb

from direct import reader as direct_reader
from tests.conftest import ACTOR
from wax import projections

INSERT = (
    "INSERT INTO core.event (event_pk, aggregate_sequence_number, event_type, schema_version,"
    " aggregate_pk_ref, aggregate_type, occurred_at, actor_pk_ref, actor_type, new_value,"
    " prior_event_hash, event_hash) VALUES (%s, 1, 'reader.test', 1, %s, 'reader_test', now(),"
    " %s, 'system', %s, NULL, %s)"
)


def _write_event():
    label = uuid.uuid4().hex[:8]
    with connection.cursor() as cursor:
        cursor.execute(INSERT, [uuid.uuid4(), uuid.uuid4(), ACTOR, Jsonb({"label": label}), f"hash-{label}"])


def _at_present(name):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s WHERE projection_name = %s",
            [projections.sequence_position(), name],
        )


def _checkpoint(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name = %s", [name])
        return cursor.fetchone()[0]


def _mover(name):
    """A hook that, the first time a pass reaches the claim, moves the
    checkpoint 10 past the pass's mark, as a reader on a later mark would."""
    moved = {}

    def hook(projection_name, safe):
        if not moved and projection_name == name:
            moved["to"] = safe + 10
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s WHERE projection_name = %s",
                    [moved["to"], name],
                )

    return hook, moved


def _until_moved(run_once, moved):
    for _ in range(300):
        run_once()
        if moved:
            return
        time.sleep(0.05)
    raise AssertionError("the reader never reached a claim")


def test_both_marks_read_the_sequence_under_its_lock():
    for sql in (projections._POSITION, direct_reader.POSITION_SQL):
        assert sql.startswith("SELECT COALESCE(pg_sequence_last_value(")
        assert "last_value ELSE" not in sql


@pytest.mark.django_db(transaction=True)
def test_the_marks_read_the_position_the_store_reports():
    _write_event()
    with connection.cursor() as cursor:
        cursor.execute("SELECT max(global_sequence_number) FROM core.event")
        highest = cursor.fetchone()[0]
    assert projections.sequence_position() == highest


@pytest.mark.django_db(transaction=True)
def test_a_claim_never_moves_the_checkpoint_back_in_andys_reader():
    item = projections.projection(f"test.claim_{uuid.uuid4().hex[:8]}", aggregate_types=["reader_test"])
    item.on("reader.test")(lambda event: None)
    try:
        projections.register_checkpoints()
        _at_present(item.name)
        projections.before_claim, moved = _mover(item.name)
        _write_event()
        projections.reset()
        _until_moved(projections.run_once, moved)
        assert _checkpoint(item.name) == moved["to"]
    finally:
        projections.before_claim = None
        projections._registry.pop(item.name, None)


@pytest.mark.django_db(transaction=True)
def test_a_claim_never_moves_the_checkpoint_back_in_the_direct_reader():
    from direct.db import connect

    item = direct_reader.Projection(f"test.claim_{uuid.uuid4().hex[:8]}", ["reader_test"])
    item.on("reader.test")(lambda cur, event: None)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.projection_checkpoint (projection_name, definition_digest) VALUES (%s, 'test')",
            [item.name],
        )
    _at_present(item.name)
    settings = connection.settings_dict
    conn = connect(dbname=settings["NAME"], user=settings["USER"], password=settings["PASSWORD"],
                   host=settings["HOST"], port=settings["PORT"])
    try:
        reader = direct_reader.Reader(conn, projections=[item])
        reader.before_claim, moved = _mover(item.name)
        _write_event()
        _until_moved(reader.run_once, moved)
        assert _checkpoint(item.name) == moved["to"]
    finally:
        conn.close()
