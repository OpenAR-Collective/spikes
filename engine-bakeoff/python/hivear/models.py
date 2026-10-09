# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/hivear/models.py), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: Account gains interest_accrued_through and queue_code, and Task is
# added, for the engine bake-off (Foundation-written, WORKLOADS.md).
"""ar schema (WAX-0004). Names follow WAX-0005: singular tables, <table>_pk
UUID keys, <root>_fk foreign keys, no abbreviations. Money is an integer
count of minor currency units.
"""

import uuid

from django.db import models

from wax.models import AggregateRoot, GuardedModel


class BalanceBucketDefinition(models.Model):
    """Agency-level bucket registry (HIVE-0005).

    Seeded by debt-type modules in the design; the framework ships none.
    waterfall_priority is the module default order from HIVE-0007, lowest first.
    """

    balance_bucket_definition_pk = models.UUIDField(primary_key=True, default=uuid.uuid4)
    code = models.TextField(unique=True)
    label = models.TextField()
    waterfall_priority = models.PositiveIntegerField()
    bears_interest = models.BooleanField(default=False)
    origin = models.TextField(
        choices=[("client_placed", "Client placed"), ("agency_assessed", "Agency assessed")],
        default="client_placed",
    )

    class Meta:
        db_table = 'ar"."balance_bucket_definition'


class Account(AggregateRoot):
    aggregate_type = "account"

    account_pk = models.UUIDField(primary_key=True)
    client_account_number = models.TextField()
    debt_type = models.TextField()
    contract_type = models.TextField(
        choices=[("third_party_contingency", "Third-party contingency"), ("purchase", "Purchase")]
    )
    currency_code = models.CharField(max_length=3)
    status = models.TextField(default="active")
    is_disputed = models.BooleanField(default=False)
    list_date = models.DateField()
    itemization_date = models.DateField()
    # Foundation-written for the engine bake-off: the last day interest has
    # been accrued for, and the work queue the account is assigned to.
    interest_accrued_through = models.DateField()
    queue_code = models.TextField(null=True)

    class Meta:
        db_table = 'ar"."account'


class AccountBalanceBucket(GuardedModel):
    """A bucket instance: the running sum of postings to one bucket on one
    account (HIVE-0005). There is no balance column on ar.account; the account
    balance is the sum of these rows.
    """

    account_balance_bucket_pk = models.UUIDField(primary_key=True, default=uuid.uuid4)
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, db_column="account_fk", related_name="balance_buckets"
    )
    balance_bucket_definition = models.ForeignKey(
        BalanceBucketDefinition, on_delete=models.PROTECT, db_column="balance_bucket_definition_fk"
    )
    balance_minor_units = models.BigIntegerField()

    class Meta:
        db_table = 'ar"."account_balance_bucket'
        constraints = [
            models.UniqueConstraint(
                fields=["account", "balance_bucket_definition"],
                name="account_balance_bucket_unique_definition",
            ),
            models.CheckConstraint(
                condition=models.Q(balance_minor_units__gte=0),
                name="account_balance_bucket_not_negative",
            ),
        ]


class Task(GuardedModel):
    """A unit of human work raised against an account (WAX-0029), created by
    the CreateTask command. Foundation-written for the engine bake-off."""

    task_pk = models.UUIDField(primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, db_column="account_fk", related_name="tasks")
    task_type = models.TextField()
    status = models.TextField()
    created_at = models.DateTimeField()

    class Meta:
        db_table = 'ar"."task'


class Payment(AggregateRoot):
    """A payment is its own aggregate; each account it touches receives a
    fan-out event (WAX-0001). distribution records exactly where the money
    landed so a reversal can unwind it (HIVE-0007).
    """

    aggregate_type = "payment"

    payment_pk = models.UUIDField(primary_key=True)
    amount_minor_units = models.BigIntegerField()
    currency_code = models.CharField(max_length=3)
    status = models.TextField(default="posted")
    received_at = models.DateTimeField()
    distribution = models.JSONField()
    unapplied_minor_units = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'ar"."payment'


class AccountSummaryReadModel(models.Model):
    """Read model maintained by hivear.projections. Never written by commands."""

    account_summary_read_model_pk = models.UUIDField(primary_key=True)
    client_account_number = models.TextField()
    status = models.TextField()
    is_disputed = models.BooleanField(default=False)
    currency_code = models.CharField(max_length=3)
    debt_type = models.TextField(null=True)
    contract_type = models.TextField(null=True)
    list_date = models.DateField(null=True)
    balance_minor_units = models.BigIntegerField(default=0)
    balance_buckets = models.JSONField(default=dict)
    last_payment_at = models.DateTimeField(null=True)
    last_payment_minor_units = models.BigIntegerField(null=True)
    last_global_sequence_number = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'ar"."account_summary_read_model'


class AccountPaymentLine(models.Model):
    """One payment as one account saw it: what the payment was, how much of
    it landed here, and how much was left unapplied. A payment taken against
    an account has a line there even when nothing was applied, so that money
    held unapplied can always be found again. Maintained by hivear.projections.

    This is version 2 of the read model. Version 1 was built from account
    events and so had no row for a payment that applied nothing.
    """

    account_payment_line_pk = models.UUIDField(primary_key=True, default=uuid.uuid4)
    payment_pk = models.UUIDField()
    account_pk = models.UUIDField()
    applied_minor_units = models.BigIntegerField()
    bucket_amounts = models.JSONField()
    payment_amount_minor_units = models.BigIntegerField()
    payment_unapplied_minor_units = models.BigIntegerField()
    currency_code = models.CharField(max_length=3)
    received_at = models.DateTimeField()
    status = models.TextField()
    last_global_sequence_number = models.BigIntegerField()

    class Meta:
        db_table = 'ar"."account_payment_read_model_v2'
        constraints = [
            models.UniqueConstraint(fields=["payment_pk", "account_pk"], name="account_payment_line_unique_pair"),
        ]
        indexes = [models.Index(fields=["account_pk", "-received_at"], name="idx_account_payment_line_acct")]


class PaymentReadModel(models.Model):
    """A payment as a whole: what was received, where it went, and what was
    left unapplied. Maintained by hivear.projections."""

    payment_read_model_pk = models.UUIDField(primary_key=True)
    amount_minor_units = models.BigIntegerField()
    currency_code = models.CharField(max_length=3)
    status = models.TextField()
    received_at = models.DateTimeField()
    distribution = models.JSONField()
    unapplied_minor_units = models.BigIntegerField()
    last_global_sequence_number = models.BigIntegerField()

    class Meta:
        db_table = 'ar"."payment_read_model'
