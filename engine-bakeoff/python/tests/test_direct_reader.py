"""The python-direct reader against Andy Troiano's reader. Foundation-written.

Commands write a mixed stream through the Django path. Andy's reader builds
the three read models from it; those rows are then deleted, the checkpoints
are wound back, and direct/reader.py builds them again from the same stream. The
rows must match, apart from the payment lines' surrogate keys.
"""

import json
import time
import uuid
from datetime import UTC, date, datetime

import pytest
from django.db import connection

from bakeoff.keys import BATCH
from hivear import commands
from wax import projections, store
from wax.commands import dispatch

WHO = {"actor_pk": BATCH, "actor_type": "batch", "source": "batch"}
NAMES = ["ar.account_summary_read_model.v1", "ar.account_payment_read_model.v2", "ar.payment_read_model.v1"]


def _set_checkpoints(position):
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.projection_checkpoint SET last_global_sequence_number = %s, failed_at = NULL,"
            " failure_kind = NULL, failed_global_sequence_number = NULL, failure_detail = NULL"
            " WHERE projection_name = ANY(%s)",
            [position, NAMES],
        )


def _positions():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT min(last_global_sequence_number), bool_or(failed_at IS NOT NULL) FROM core.projection_checkpoint"
            " WHERE projection_name = ANY(%s)",
            [NAMES],
        )
        return cursor.fetchone()


def _drain(run_once):
    head = store.current_position()
    for _ in range(200):
        run_once()
        low, failed = _positions()
        assert not failed, "a projection stopped"
        if low >= head:
            return
        time.sleep(0.02)
    raise AssertionError("the reader did not reach the head")


def _rows(account_pks, payment_pks):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT account_summary_read_model_pk, client_account_number, status, is_disputed, currency_code, debt_type,"
            " contract_type, list_date, balance_minor_units, balance_buckets::text, last_payment_at,"
            " last_payment_minor_units, last_global_sequence_number FROM ar.account_summary_read_model"
            " WHERE account_summary_read_model_pk = ANY(%s) ORDER BY 1",
            [account_pks],
        )
        summaries = cursor.fetchall()
        cursor.execute(
            "SELECT payment_read_model_pk, amount_minor_units, currency_code, status, received_at, distribution::text,"
            " unapplied_minor_units, last_global_sequence_number FROM ar.payment_read_model"
            " WHERE payment_read_model_pk = ANY(%s) ORDER BY 1",
            [payment_pks],
        )
        payments = cursor.fetchall()
        cursor.execute(
            "SELECT payment_pk, account_pk, applied_minor_units, bucket_amounts::text, payment_amount_minor_units,"
            " payment_unapplied_minor_units, currency_code, received_at, status, last_global_sequence_number"
            " FROM ar.account_payment_read_model_v2 WHERE payment_pk = ANY(%s) ORDER BY 1, 2",
            [payment_pks],
        )
        lines = cursor.fetchall()
    normalized = []
    for table in (summaries, payments, lines):
        normalized.append([
            [json.loads(value) if isinstance(value, str) and value.startswith("{") else value for value in row]
            for row in table
        ])
    return normalized


def _delete(account_pks, payment_pks):
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM ar.account_summary_read_model WHERE account_summary_read_model_pk = ANY(%s)", [account_pks])
        cursor.execute("DELETE FROM ar.payment_read_model WHERE payment_read_model_pk = ANY(%s)", [payment_pks])
        cursor.execute("DELETE FROM ar.account_payment_read_model_v2 WHERE payment_pk = ANY(%s)", [payment_pks])


@pytest.mark.django_db(transaction=True)
def test_the_direct_reader_builds_what_andys_reader_builds(buckets):
    from direct.db import connect
    from direct.reader import Reader

    projections.register_checkpoints()
    start = store.current_position()
    _set_checkpoints(start)

    accounts = [uuid.uuid4() for _ in range(3)]
    for i, pk in enumerate(accounts):
        dispatch(commands.PlaceAccount(
            account_pk=pk, client_account_number=f"READ-{pk.hex[:8]}", debt_type="personal_loan",
            contract_type="third_party_contingency", currency_code="USD", list_date=date(2024, 3, 1 + i),
            itemization_date=date(2024, 1, 31), interest_accrued_through=date(2026, 9, 30),
            opening_balances={"principal": 10_000 + i, "interest": 700, "fee": 1_500},
        ), **WHO)
    dispatch(commands.OpenDispute(account_pk=accounts[2]), **WHO)
    received = datetime(2026, 10, 1, 6, tzinfo=UTC)
    payments = [uuid.uuid4() for _ in range(4)]
    dispatch(commands.PostPayment(payment_pk=payments[0], account_pks=[accounts[0]], amount_minor_units=2_500,
                                  currency_code="USD", received_at=received), **WHO)
    dispatch(commands.PostPayment(payment_pk=payments[1], account_pks=accounts, amount_minor_units=6_000,
                                  currency_code="USD", allocation_method="even_split", received_at=received), **WHO)
    dispatch(commands.PostPayment(payment_pk=payments[2], account_pks=[accounts[2]], amount_minor_units=900,
                                  currency_code="USD", received_at=received), **WHO)
    dispatch(commands.PostPayment(payment_pk=payments[3], account_pks=[accounts[1]], amount_minor_units=9_999_999,
                                  currency_code="USD", received_at=received), **WHO)
    dispatch(commands.ReversePayment(payment_pk=payments[0]), **WHO)
    dispatch(commands.AccrueInterest(account_pk=accounts[0], accrual_date=date(2026, 10, 31),
                                     annual_rate_basis_points=1_800), **WHO)
    dispatch(commands.ChangeAccountStatus(account_pk=accounts[1], status="paid_in_full", reason_code="paid_in_full"), **WHO)

    projections.reset()
    _drain(projections.run_once)
    expected = _rows(accounts, payments)
    assert len(expected[0]) == 3 and len(expected[1]) == 4 and len(expected[2]) >= 4

    _delete(accounts, payments)
    _set_checkpoints(start)
    settings = connection.settings_dict
    conn = connect(dbname=settings["NAME"], user=settings["USER"], password=settings["PASSWORD"],
                   host=settings["HOST"], port=settings["PORT"])
    try:
        reader = Reader(conn)
        _drain(reader.run_once)
    finally:
        conn.close()
    assert _rows(accounts, payments) == expected
