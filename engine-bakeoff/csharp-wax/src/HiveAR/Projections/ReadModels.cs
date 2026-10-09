using System.Globalization;
using System.Text.Json.Nodes;
using Wax.Events;
using Wax.Projections;

namespace HiveAR.Projections;

// The three read models of the prototype's hivear/projections.py, under the
// same names and versions, maintained by Wax's own reader, which is ported
// from the prototype's. Each writes plain SQL into the same ar tables the
// Python side uses, and the reader commits each page of read model writes
// with its checkpoint. Each covers one aggregate type and stops on an event
// type of that aggregate that it neither handles nor ignores.

/// <summary>The projections HiveAR registers with the engine.</summary>
public static class ReadModels
{
    public static IReadOnlyList<Projection> All() =>
    [
        AccountSummaryProjection.Create(),
        PaymentReadModelProjection.Create(),
        AccountPaymentLineProjection.Create(),
    ];
}

/// <summary>One row per account: its status and balances, for screens.</summary>
public static class AccountSummaryProjection
{
    public static Projection Create() => new Projection("ar.account_summary_read_model", 1, ["account"])
        .On<AccountPlaced>(Apply)
        .On<DisputeOpened>(Apply)
        .On<AccountPaymentApplied>(Apply)
        .On<AccountPaymentReversed>(Apply)
        // Added for the bake-off's workloads. The other new account events
        // change nothing this read model shows, so they are ignored on purpose.
        .On<AccountInterestAccrued>(Apply)
        .On<AccountStatusChanged>(Apply)
        .Ignore<PendingEventScheduled>()
        .Ignore<AccountQueueAssigned>()
        .Ignore<TaskCreated>()
        .Ignore<AccountDisputeReviewDue>()
        .Ignore<AccountFirstContactDue>();

    static void Apply(StoredEvent e, ProjectionWrites operations)
    {
        switch (e.EventType)
        {
            case "account.placed":
            {
                var value = e.NewValue!;
                var buckets = value["balance_buckets"]!.AsObject();
                operations.Sql(
                    "INSERT INTO ar.account_summary_read_model (account_summary_read_model_pk, client_account_number, status, " +
                    "is_disputed, currency_code, debt_type, contract_type, list_date, balance_buckets, balance_minor_units, " +
                    "last_global_sequence_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?) " +
                    "ON CONFLICT (account_summary_read_model_pk) DO UPDATE SET client_account_number = EXCLUDED.client_account_number, " +
                    "status = EXCLUDED.status, is_disputed = EXCLUDED.is_disputed, currency_code = EXCLUDED.currency_code, " +
                    "debt_type = EXCLUDED.debt_type, contract_type = EXCLUDED.contract_type, list_date = EXCLUDED.list_date, " +
                    "balance_buckets = EXCLUDED.balance_buckets, balance_minor_units = EXCLUDED.balance_minor_units, " +
                    "last_global_sequence_number = EXCLUDED.last_global_sequence_number",
                    e.AggregatePkRef,
                    Text(value, "client_account_number"),
                    Text(value, "status"),
                    value["is_disputed"]!.GetValue<bool>(),
                    Text(value, "currency_code"),
                    Text(value, "debt_type"),
                    Text(value, "contract_type"),
                    DateOnly.Parse(Text(value, "list_date"), CultureInfo.InvariantCulture),
                    buckets.ToJsonString(),
                    buckets.Sum(pair => pair.Value!.GetValue<long>()),
                    e.GlobalSequenceNumber);
                break;
            }
            case "dispute.opened":
                operations.Sql(
                    "UPDATE ar.account_summary_read_model SET is_disputed = true, last_global_sequence_number = ? " +
                    "WHERE account_summary_read_model_pk = ?",
                    e.GlobalSequenceNumber, e.AggregatePkRef);
                break;
            case "account.payment_applied":
            {
                var moved = e.NewValue!["balance_buckets"]!.ToJsonString();
                var metadata = e.Metadata!;
                operations.Sql(
                    "UPDATE ar.account_summary_read_model SET balance_buckets = balance_buckets || ?::jsonb, " +
                    "balance_minor_units = (SELECT COALESCE(sum(value::bigint), 0) FROM jsonb_each_text(balance_buckets || ?::jsonb)), " +
                    "last_global_sequence_number = ?, last_payment_at = ?, last_payment_minor_units = ? " +
                    "WHERE account_summary_read_model_pk = ?",
                    moved, moved, e.GlobalSequenceNumber,
                    DateTimeOffset.Parse(Text(metadata, "received_at"), CultureInfo.InvariantCulture).ToUniversalTime(),
                    metadata["bucket_amounts"]!.AsObject().Sum(pair => pair.Value!.GetValue<long>()),
                    e.AggregatePkRef);
                break;
            }
            case "account.status_changed":
                operations.Sql(
                    "UPDATE ar.account_summary_read_model SET status = ?, last_global_sequence_number = ? " +
                    "WHERE account_summary_read_model_pk = ?",
                    Text(e.NewValue!, "status"), e.GlobalSequenceNumber, e.AggregatePkRef);
                break;
            case "account.payment_reversed" or "account.interest_accrued":
            {
                var moved = e.NewValue!["balance_buckets"]!.ToJsonString();
                operations.Sql(
                    "UPDATE ar.account_summary_read_model SET balance_buckets = balance_buckets || ?::jsonb, " +
                    "balance_minor_units = (SELECT COALESCE(sum(value::bigint), 0) FROM jsonb_each_text(balance_buckets || ?::jsonb)), " +
                    "last_global_sequence_number = ? WHERE account_summary_read_model_pk = ?",
                    moved, moved, e.GlobalSequenceNumber, e.AggregatePkRef);
                break;
            }
        }
    }

    internal static string Text(JsonObject value, string key) => value[key]!.GetValue<string>();
}

/// <summary>A payment as a whole: what was received, where it went, and what
/// was left unapplied.</summary>
public static class PaymentReadModelProjection
{
    public static Projection Create() => new Projection("ar.payment_read_model", 1, ["payment"])
        .On<PaymentPosted>(Apply)
        .On<PaymentReversed>(Apply);

    static void Apply(StoredEvent e, ProjectionWrites operations)
    {
        switch (e.EventType)
        {
            case "payment.posted":
            {
                var value = e.NewValue!;
                operations.Sql(
                    "INSERT INTO ar.payment_read_model (payment_read_model_pk, amount_minor_units, currency_code, status, " +
                    "received_at, distribution, unapplied_minor_units, last_global_sequence_number) " +
                    "VALUES (?, ?, ?, ?, ?, ?::jsonb, ?, ?) ON CONFLICT (payment_read_model_pk) DO UPDATE SET " +
                    "amount_minor_units = EXCLUDED.amount_minor_units, currency_code = EXCLUDED.currency_code, " +
                    "status = EXCLUDED.status, received_at = EXCLUDED.received_at, distribution = EXCLUDED.distribution, " +
                    "unapplied_minor_units = EXCLUDED.unapplied_minor_units, " +
                    "last_global_sequence_number = EXCLUDED.last_global_sequence_number",
                    e.AggregatePkRef,
                    value["amount_minor_units"]!.GetValue<long>(),
                    AccountSummaryProjection.Text(value, "currency_code"),
                    AccountSummaryProjection.Text(value, "status"),
                    DateTimeOffset.Parse(AccountSummaryProjection.Text(value, "received_at"), CultureInfo.InvariantCulture).ToUniversalTime(),
                    value["distribution"]!.ToJsonString(),
                    value["unapplied_minor_units"]!.GetValue<long>(),
                    e.GlobalSequenceNumber);
                break;
            }
            case "payment.reversed":
                operations.Sql(
                    "UPDATE ar.payment_read_model SET status = ?, last_global_sequence_number = ? WHERE payment_read_model_pk = ?",
                    AccountSummaryProjection.Text(e.NewValue!, "status"), e.GlobalSequenceNumber, e.AggregatePkRef);
                break;
        }
    }
}

/// <summary>
/// One payment as one account saw it, built from the payment's own events,
/// which name every account the payment was taken against. A payment taken
/// against an account has a line there even when nothing was applied.
/// </summary>
public static class AccountPaymentLineProjection
{
    public static Projection Create() => new Projection("ar.account_payment_read_model", 2, ["payment"])
        .On<PaymentPosted>(Apply)
        .On<PaymentReversed>(Apply);

    static void Apply(StoredEvent e, ProjectionWrites operations)
    {
        switch (e.EventType)
        {
            case "payment.posted":
            {
                var value = e.NewValue!;
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
                    operations.Sql(
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
                        e.AggregatePkRef,
                        Guid.Parse(accountPk),
                        applied,
                        buckets.ToJsonString(),
                        value["amount_minor_units"]!.GetValue<long>(),
                        value["unapplied_minor_units"]!.GetValue<long>(),
                        AccountSummaryProjection.Text(value, "currency_code"),
                        receivedAt,
                        applied > 0 ? "applied" : "held",
                        e.GlobalSequenceNumber);
                }
                break;
            }
            case "payment.reversed":
                operations.Sql(
                    "UPDATE ar.account_payment_read_model_v2 SET status = 'reversed', last_global_sequence_number = ? WHERE payment_pk = ?",
                    e.GlobalSequenceNumber, e.AggregatePkRef);
                break;
        }
    }
}
