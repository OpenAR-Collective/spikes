using System.Globalization;
using System.Text.Json.Nodes;
using JasperFx.Events;
using Marten;
using Marten.Events.Projections;

namespace HiveAR.Projections;

// The three read models of the prototype's hivear/projections.py, maintained
// by Marten's asynchronous projection daemon instead of the prototype's own
// reader. Each writes plain SQL into the same ar tables the Python side uses,
// and Marten commits each batch of read model writes with its progress.

/// <summary>One row per account: its status and balances, for screens.</summary>
public sealed class AccountSummaryProjection : EventProjection
{
    public AccountSummaryProjection()
    {
        Name = "ar_account_summary_read_model_v1";
        IncludeType<AccountPlaced>();
        IncludeType<DisputeOpened>();
        IncludeType<AccountPaymentApplied>();
        IncludeType<AccountPaymentReversed>();
    }

    public override ValueTask ApplyAsync(IDocumentOperations operations, IEvent e, CancellationToken cancellation)
    {
        switch (e.Data)
        {
            case AccountPlaced placed:
            {
                var value = placed.NewValue!;
                var buckets = value["balance_buckets"]!.AsObject();
                operations.QueueSqlCommand(
                    "INSERT INTO ar.account_summary_read_model (account_summary_read_model_pk, client_account_number, status, " +
                    "is_disputed, currency_code, debt_type, contract_type, list_date, balance_buckets, balance_minor_units, " +
                    "last_global_sequence_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?) " +
                    "ON CONFLICT (account_summary_read_model_pk) DO UPDATE SET client_account_number = EXCLUDED.client_account_number, " +
                    "status = EXCLUDED.status, is_disputed = EXCLUDED.is_disputed, currency_code = EXCLUDED.currency_code, " +
                    "debt_type = EXCLUDED.debt_type, contract_type = EXCLUDED.contract_type, list_date = EXCLUDED.list_date, " +
                    "balance_buckets = EXCLUDED.balance_buckets, balance_minor_units = EXCLUDED.balance_minor_units, " +
                    "last_global_sequence_number = EXCLUDED.last_global_sequence_number",
                    e.StreamId,
                    Text(value, "client_account_number"),
                    Text(value, "status"),
                    value["is_disputed"]!.GetValue<bool>(),
                    Text(value, "currency_code"),
                    Text(value, "debt_type"),
                    Text(value, "contract_type"),
                    DateOnly.Parse(Text(value, "list_date"), CultureInfo.InvariantCulture),
                    buckets.ToJsonString(),
                    buckets.Sum(pair => pair.Value!.GetValue<long>()),
                    e.Sequence);
                break;
            }
            case DisputeOpened:
                operations.QueueSqlCommand(
                    "UPDATE ar.account_summary_read_model SET is_disputed = true, last_global_sequence_number = ? " +
                    "WHERE account_summary_read_model_pk = ?",
                    e.Sequence, e.StreamId);
                break;
            case AccountPaymentApplied applied:
            {
                var moved = applied.NewValue!["balance_buckets"]!.ToJsonString();
                var metadata = applied.Metadata!;
                operations.QueueSqlCommand(
                    "UPDATE ar.account_summary_read_model SET balance_buckets = balance_buckets || ?::jsonb, " +
                    "balance_minor_units = (SELECT COALESCE(sum(value::bigint), 0) FROM jsonb_each_text(balance_buckets || ?::jsonb)), " +
                    "last_global_sequence_number = ?, last_payment_at = ?, last_payment_minor_units = ? " +
                    "WHERE account_summary_read_model_pk = ?",
                    moved, moved, e.Sequence,
                    DateTimeOffset.Parse(Text(metadata, "received_at"), CultureInfo.InvariantCulture).ToUniversalTime(),
                    metadata["bucket_amounts"]!.AsObject().Sum(pair => pair.Value!.GetValue<long>()),
                    e.StreamId);
                break;
            }
            case AccountPaymentReversed reversed:
            {
                var moved = reversed.NewValue!["balance_buckets"]!.ToJsonString();
                operations.QueueSqlCommand(
                    "UPDATE ar.account_summary_read_model SET balance_buckets = balance_buckets || ?::jsonb, " +
                    "balance_minor_units = (SELECT COALESCE(sum(value::bigint), 0) FROM jsonb_each_text(balance_buckets || ?::jsonb)), " +
                    "last_global_sequence_number = ? WHERE account_summary_read_model_pk = ?",
                    moved, moved, e.Sequence, e.StreamId);
                break;
            }
        }
        return ValueTask.CompletedTask;
    }

    internal static string Text(JsonObject value, string key) => value[key]!.GetValue<string>();
}

/// <summary>A payment as a whole: what was received, where it went, and what
/// was left unapplied.</summary>
public sealed class PaymentReadModelProjection : EventProjection
{
    public PaymentReadModelProjection()
    {
        Name = "ar_payment_read_model_v1";
        IncludeType<PaymentPosted>();
        IncludeType<PaymentReversed>();
    }

    public override ValueTask ApplyAsync(IDocumentOperations operations, IEvent e, CancellationToken cancellation)
    {
        switch (e.Data)
        {
            case PaymentPosted posted:
            {
                var value = posted.NewValue!;
                operations.QueueSqlCommand(
                    "INSERT INTO ar.payment_read_model (payment_read_model_pk, amount_minor_units, currency_code, status, " +
                    "received_at, distribution, unapplied_minor_units, last_global_sequence_number) " +
                    "VALUES (?, ?, ?, ?, ?, ?::jsonb, ?, ?) ON CONFLICT (payment_read_model_pk) DO UPDATE SET " +
                    "amount_minor_units = EXCLUDED.amount_minor_units, currency_code = EXCLUDED.currency_code, " +
                    "status = EXCLUDED.status, received_at = EXCLUDED.received_at, distribution = EXCLUDED.distribution, " +
                    "unapplied_minor_units = EXCLUDED.unapplied_minor_units, " +
                    "last_global_sequence_number = EXCLUDED.last_global_sequence_number",
                    e.StreamId,
                    value["amount_minor_units"]!.GetValue<long>(),
                    AccountSummaryProjection.Text(value, "currency_code"),
                    AccountSummaryProjection.Text(value, "status"),
                    DateTimeOffset.Parse(AccountSummaryProjection.Text(value, "received_at"), CultureInfo.InvariantCulture).ToUniversalTime(),
                    value["distribution"]!.ToJsonString(),
                    value["unapplied_minor_units"]!.GetValue<long>(),
                    e.Sequence);
                break;
            }
            case PaymentReversed reversed:
                operations.QueueSqlCommand(
                    "UPDATE ar.payment_read_model SET status = ?, last_global_sequence_number = ? WHERE payment_read_model_pk = ?",
                    AccountSummaryProjection.Text(reversed.NewValue!, "status"), e.Sequence, e.StreamId);
                break;
        }
        return ValueTask.CompletedTask;
    }
}

/// <summary>
/// One payment as one account saw it, built from the payment's own events,
/// which name every account the payment was taken against. A payment taken
/// against an account has a line there even when nothing was applied.
/// </summary>
public sealed class AccountPaymentLineProjection : EventProjection
{
    public AccountPaymentLineProjection()
    {
        Name = "ar_account_payment_read_model_v2";
        IncludeType<PaymentPosted>();
        IncludeType<PaymentReversed>();
    }

    public override ValueTask ApplyAsync(IDocumentOperations operations, IEvent e, CancellationToken cancellation)
    {
        switch (e.Data)
        {
            case PaymentPosted posted:
            {
                var value = posted.NewValue!;
                var distribution = value["distribution"]!.AsObject();
                // Events written before the request was recorded name only the
                // accounts money reached.
                var named = (value["request"]?["account_pks"] as JsonArray)?.Select(node => node!.GetValue<string>()).ToList();
                if (named is null || named.Count == 0)
                {
                    named = distribution.Select(pair => pair.Key).ToList();
                }
                var receivedAt = DateTimeOffset.Parse(AccountSummaryProjection.Text(value, "received_at"), CultureInfo.InvariantCulture).ToUniversalTime();
                foreach (var accountPk in named)
                {
                    var buckets = distribution[accountPk] as JsonObject ?? [];
                    var applied = buckets.Sum(pair => pair.Value!.GetValue<long>());
                    operations.QueueSqlCommand(
                        "INSERT INTO ar.account_payment_read_model_v2 (account_payment_line_pk, payment_pk, account_pk, " +
                        "applied_minor_units, bucket_amounts, payment_amount_minor_units, payment_unapplied_minor_units, " +
                        "currency_code, received_at, status, last_global_sequence_number) " +
                        "VALUES (?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?, ?, ?) ON CONFLICT (payment_pk, account_pk) DO UPDATE SET " +
                        "applied_minor_units = EXCLUDED.applied_minor_units, bucket_amounts = EXCLUDED.bucket_amounts, " +
                        "payment_amount_minor_units = EXCLUDED.payment_amount_minor_units, " +
                        "payment_unapplied_minor_units = EXCLUDED.payment_unapplied_minor_units, " +
                        "currency_code = EXCLUDED.currency_code, received_at = EXCLUDED.received_at, status = EXCLUDED.status, " +
                        "last_global_sequence_number = EXCLUDED.last_global_sequence_number",
                        Guid.NewGuid(),
                        e.StreamId,
                        Guid.Parse(accountPk),
                        applied,
                        buckets.ToJsonString(),
                        value["amount_minor_units"]!.GetValue<long>(),
                        value["unapplied_minor_units"]!.GetValue<long>(),
                        AccountSummaryProjection.Text(value, "currency_code"),
                        receivedAt,
                        applied > 0 ? "applied" : "held",
                        e.Sequence);
                }
                break;
            }
            case PaymentReversed:
                operations.QueueSqlCommand(
                    "UPDATE ar.account_payment_read_model_v2 SET status = 'reversed', last_global_sequence_number = ? WHERE payment_pk = ?",
                    e.Sequence, e.StreamId);
                break;
        }
        return ValueTask.CompletedTask;
    }
}
