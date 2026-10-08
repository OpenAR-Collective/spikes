"""The reader in wax.projections: no skipped event, per-aggregate order, one
failing projection does not stop the others, and the cases the review of the
first version found."""

import uuid

import pytest
from django.db import connection, transaction
from psycopg.types.json import Jsonb

from tests.conftest import ACTOR
from wax import projections

INSERT = (
    "INSERT INTO core.event (event_pk, aggregate_sequence_number, event_type, schema_version,"
    " aggregate_pk_ref, aggregate_type, occurred_at, actor_pk_ref, actor_type, new_value,"
    " prior_event_hash, event_hash) VALUES (%s, %s, %s, 1, %s, 'reader_test', now(),"
    " %s, 'system', %s, %s, %s)"
)
AT_PRESENT = (
    "UPDATE core.projection_checkpoint SET last_global_sequence_number = "
    "(SELECT CASE WHEN is_called THEN last_value ELSE 0 END FROM core.seq_event_global_sequence) "
    "WHERE projection_name = %s"
)


def write(conn, aggregate_pk, sequence, label, event_type="reader.test"):
    with conn.cursor() as cursor:
        cursor.execute(
            INSERT,
            [uuid.uuid4(), sequence, event_type, aggregate_pk, ACTOR, Jsonb({"label": label}),
             None if sequence == 1 else "x", f"hash-{label}"],
        )


def register(seen, event_types=("reader.test",), ignores=(), name=None):
    """A throwaway projection that records the labels it is handed. A dict,
    because a real handler's writes roll back with a failed batch and an
    in-memory list would not."""
    item = projections.projection(
        name or f"test.capture_{uuid.uuid4().hex[:8]}", aggregate_types=["reader_test"], ignores=ignores
    )
    item.on(*event_types)(lambda event: seen.__setitem__(event.new_value["label"], True))
    projections.register_checkpoints()
    # Start from the present: earlier tests' events are not this test's business.
    with connection.cursor() as cursor:
        cursor.execute(AT_PRESENT, [item.name])
    return item


@pytest.fixture
def capture():
    seen, made = {}, []

    def make(**kwargs):
        made.append(register(kwargs.pop("seen", seen), **kwargs))
        return made[-1]

    make.seen = seen
    make()
    yield make
    for item in made:
        projections._registry.pop(item.name, None)


def drain():
    while projections.run_once():
        pass


def row(item):
    return next(r for r in projections.status() if r["projection"] == item.name)


@pytest.fixture
def second():
    other = connection.copy()
    yield other
    other.close()


@pytest.mark.django_db(transaction=True)
def test_an_uncommitted_lower_sequence_is_never_skipped(capture, second):
    slow, fast = uuid.uuid4(), uuid.uuid4()
    second.set_autocommit(False)
    write(second, slow, 1, "slow")          # takes the lower sequence number, stays open
    write(connection, fast, 1, "fast")      # higher number, already committed

    drain()
    assert list(capture.seen) == [], "reading past the open transaction would lose its event"

    second.commit()
    drain()
    assert list(capture.seen) == ["slow", "fast"]


@pytest.mark.django_db(transaction=True)
def test_one_aggregate_is_delivered_in_its_own_order(capture, second):
    """The transaction that started first writes the aggregate's later event."""
    aggregate, elsewhere = uuid.uuid4(), uuid.uuid4()
    second.set_autocommit(False)
    write(second, elsewhere, 1, "unrelated")   # second now holds the older transaction id
    write(connection, aggregate, 1, "first")   # commits first
    write(second, aggregate, 2, "second")      # written after, by the older transaction
    second.commit()

    drain()
    seen = list(capture.seen)
    assert seen.index("first") < seen.index("second")


@pytest.mark.django_db(transaction=True)
def test_a_burned_sequence_number_does_not_leave_the_projection_behind(capture, second):
    """A rolled-back insert uses up a number no event will ever carry."""
    item = projections._registry[next(n for n in projections._registry if n.startswith("test.capture_"))]
    write(connection, uuid.uuid4(), 1, "kept")
    second.set_autocommit(False)
    write(second, uuid.uuid4(), 1, "discarded")
    second.rollback()

    drain()
    projections.run_once()
    assert list(capture.seen) == ["kept"]
    assert row(item)["position"] == projections.sequence_position()


@pytest.mark.django_db(transaction=True)
def test_a_failing_projection_stops_alone_and_resumes(capture):
    broken_seen, poisoned = {}, {"on": True}
    broken = projections.projection(f"test.broken_{uuid.uuid4().hex[:8]}", aggregate_types=["reader_test"])

    @broken.on("reader.test")
    def _handler(event):
        if poisoned["on"] and event.new_value["label"] == "bad":
            raise ValueError("cannot handle this one\x00")
        broken_seen[event.new_value["label"]] = True

    projections.register_checkpoints()
    with connection.cursor() as cursor:
        cursor.execute(AT_PRESENT, [broken.name])
    try:
        aggregate = uuid.uuid4()
        for sequence, label in enumerate(["good", "bad", "after"], start=1):
            write(connection, aggregate, sequence, label)
        drain()

        assert list(capture.seen) == ["good", "bad", "after"]
        assert list(broken_seen) == ["good"], "it must stop at the event, not skip it"
        state = row(broken)
        assert state["failure_kind"] == "handler_error" and "cannot handle" in state["failure_detail"]

        poisoned["on"] = False
        projections.retry(broken.name)
        drain()
        assert list(broken_seen) == ["good", "bad", "after"]
    finally:
        del projections._registry[broken.name]


@pytest.mark.django_db(transaction=True)
def test_an_unknown_event_type_stops_the_projection_until_a_release_that_knows_it(capture):
    """Skipping an event that changed state would leave the read model wrong
    with no sign. An older release stops; a newer one picks the event up."""
    name = f"test.versioned_{uuid.uuid4().hex[:8]}"
    old_seen, new_seen = {}, {}
    older = capture(seen=old_seen, name=name)
    aggregate = uuid.uuid4()
    write(connection, aggregate, 1, "known")
    write(connection, aggregate, 2, "adjusted", event_type="reader.adjusted")
    write(connection, aggregate, 3, "later")

    drain()
    assert list(old_seen) == ["known"]
    assert row(older)["failure_kind"] == "unknown_event_type"

    # The next release handles the new type. Its definition differs, so it
    # may not touch the old version's rows until it is adopted as the definition.
    del projections._registry[older.name]
    newer = projections.projection(name, aggregate_types=["reader_test"])
    newer.on("reader.test", "reader.adjusted")(lambda event: new_seen.__setitem__(event.new_value["label"], True))
    try:
        drain()
        assert new_seen == {} and row(newer)["definition_matches"] is False

        projections.accept_definition(newer.name)
        drain()
        assert list(new_seen) == ["adjusted", "later"]
        assert row(newer)["failed_at"] is None
    finally:
        projections._registry.pop(newer.name, None)


@pytest.mark.django_db(transaction=True)
def test_an_ignored_event_type_is_passed_over_on_purpose():
    seen = {}
    item = register(seen, ignores=["reader.noise"])
    try:
        aggregate = uuid.uuid4()
        write(connection, aggregate, 1, "noise", event_type="reader.noise")
        write(connection, aggregate, 2, "signal")
        drain()
        assert list(seen) == ["signal"] and row(item)["failed_at"] is None
    finally:
        del projections._registry[item.name]


@pytest.mark.django_db(transaction=True)
def test_the_mark_is_forgotten_when_the_database_goes_back_in_time():
    """After a restore or a failover the sequence can sit below a mark this
    process already trusted. Reading on the old mark would skip events."""
    mark = projections.HighWaterMark()
    position = mark.advance()
    assert position == projections.sequence_position()

    mark.value, mark._last_sampled = position + 1000, position + 1000   # as if the store had been ahead
    assert mark.advance() <= projections.sequence_position()


@pytest.mark.django_db(transaction=True)
def test_the_reader_refuses_to_run_inside_another_transaction():
    with transaction.atomic(), pytest.raises(RuntimeError, match="inside another transaction"):
        projections.run_once()
