from datetime import datetime

from hivear.models import AccountPaymentLine, AccountSummaryReadModel, PaymentReadModel
from wax.projections import projection

account_summary = projection("ar.account_summary_read_model", version=1, aggregate_types=["account"])


@account_summary.on("account.placed")
def _placed(event):
    new = event.new_value
    AccountSummaryReadModel.objects.update_or_create(
        pk=event.aggregate_pk_ref,
        defaults={
            "client_account_number": new["client_account_number"],
            "status": new["status"],
            "is_disputed": new["is_disputed"],
            "currency_code": new["currency_code"],
            "debt_type": new["debt_type"],
            "contract_type": new["contract_type"],
            "list_date": new["list_date"],
            "balance_buckets": new["balance_buckets"],
            "balance_minor_units": sum(new["balance_buckets"].values()),
            "last_global_sequence_number": event.global_sequence_number,
        },
    )


@account_summary.on("dispute.opened")
def _disputed(event):
    AccountSummaryReadModel.objects.filter(pk=event.aggregate_pk_ref).update(
        is_disputed=True, last_global_sequence_number=event.global_sequence_number
    )


@account_summary.on("account.payment_applied", "account.payment_reversed")
def _balance_moved(event):
    summary = AccountSummaryReadModel.objects.get(pk=event.aggregate_pk_ref)
    summary.balance_buckets.update(event.new_value["balance_buckets"])
    summary.balance_minor_units = sum(summary.balance_buckets.values())
    summary.last_global_sequence_number = event.global_sequence_number
    if event.event_type == "account.payment_applied":
        summary.last_payment_at = datetime.fromisoformat(event.metadata["received_at"])
        summary.last_payment_minor_units = sum(event.metadata["bucket_amounts"].values())
    summary.save()


# Version 2: built from the payment's own events, which name every account
# the payment was taken against. Version 1 read account events and so missed
# a payment that applied nothing (SHARED-0010: a changed projection is a new
# version with its own table, built beside the old one).
account_payment = projection("ar.account_payment_read_model", version=2, aggregate_types=["payment"])


@account_payment.on("payment.posted")
def _payment_lines(event):
    new = event.new_value
    distribution = new["distribution"]
    # Events written before the request was recorded name only the accounts money reached.
    named = (new.get("request") or {}).get("account_pks") or list(distribution)
    for account_pk in named:
        buckets = distribution.get(account_pk, {})
        applied = sum(buckets.values())
        AccountPaymentLine.objects.update_or_create(
            payment_pk=event.aggregate_pk_ref,
            account_pk=account_pk,
            defaults={
                "applied_minor_units": applied,
                "bucket_amounts": buckets,
                "payment_amount_minor_units": new["amount_minor_units"],
                "payment_unapplied_minor_units": new["unapplied_minor_units"],
                "currency_code": new["currency_code"],
                "received_at": datetime.fromisoformat(new["received_at"]),
                "status": "applied" if applied else "held",
                "last_global_sequence_number": event.global_sequence_number,
            },
        )


@account_payment.on("payment.reversed")
def _payment_lines_reversed(event):
    AccountPaymentLine.objects.filter(payment_pk=event.aggregate_pk_ref).update(
        status="reversed", last_global_sequence_number=event.global_sequence_number
    )


payment = projection("ar.payment_read_model", version=1, aggregate_types=["payment"])


@payment.on("payment.posted")
def _payment_posted(event):
    new = event.new_value
    PaymentReadModel.objects.update_or_create(
        pk=event.aggregate_pk_ref,
        defaults={
            "amount_minor_units": new["amount_minor_units"],
            "currency_code": new["currency_code"],
            "status": new["status"],
            "received_at": datetime.fromisoformat(new["received_at"]),
            "distribution": new["distribution"],
            "unapplied_minor_units": new["unapplied_minor_units"],
            "last_global_sequence_number": event.global_sequence_number,
        },
    )


@payment.on("payment.reversed")
def _payment_was_reversed(event):
    PaymentReadModel.objects.filter(pk=event.aggregate_pk_ref).update(
        status=event.new_value["status"], last_global_sequence_number=event.global_sequence_number
    )
