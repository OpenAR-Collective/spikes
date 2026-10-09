using System.Text.Json.Nodes;
using Npgsql;
using NpgsqlTypes;
using Wax.Commands;

namespace HiveAR;

public sealed class AccountState : AggregateRoot
{
    public const string LockSql =
        "SELECT account_pk, aggregate_sequence_number, client_account_number, debt_type, contract_type, " +
        "currency_code, status, is_disputed, list_date, itemization_date " +
        "FROM ar.account WHERE account_pk = ANY($1) ORDER BY account_pk FOR UPDATE";

    public override string AggregateType => "account";
    public required string ClientAccountNumber { get; init; }
    public required string DebtType { get; init; }
    public required string ContractType { get; init; }
    public required string CurrencyCode { get; init; }
    public string Status { get; set; } = "active";
    public bool IsDisputed { get; set; }
    public required DateOnly ListDate { get; init; }
    public required DateOnly ItemizationDate { get; init; }

    /// <summary>Row-lock account state rows in a fixed order (WAX-0013).</summary>
    public static NpgsqlBatchCommand LockCommand(Guid[] accountPks) => new(LockSql)
    {
        Parameters = { UuidArray(accountPks) },
    };

    public static AccountState Read(NpgsqlDataReader reader) => new()
    {
        Pk = reader.GetGuid(0),
        AggregateSequenceNumber = reader.GetInt32(1),
        ClientAccountNumber = reader.GetString(2),
        DebtType = reader.GetString(3),
        ContractType = reader.GetString(4),
        CurrencyCode = reader.GetString(5),
        Status = reader.GetString(6),
        IsDisputed = reader.GetBoolean(7),
        ListDate = reader.GetFieldValue<DateOnly>(8),
        ItemizationDate = reader.GetFieldValue<DateOnly>(9),
    };

    protected override void QueueInsert(CommandContext context) => context.QueueSql(
        "INSERT INTO ar.account (aggregate_sequence_number, account_pk, client_account_number, debt_type, " +
        "contract_type, currency_code, status, is_disputed, list_date, itemization_date) " +
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        AggregateSequenceNumber, Pk, ClientAccountNumber, DebtType, ContractType, CurrencyCode, Status, IsDisputed,
        ListDate, ItemizationDate);

    protected override void QueueUpdate(CommandContext context) => context.QueueSql(
        "UPDATE ar.account SET aggregate_sequence_number = ?, status = ?, is_disputed = ? WHERE account_pk = ?",
        AggregateSequenceNumber, Status, IsDisputed, Pk);

    public static NpgsqlParameter UuidArray(Guid[] values) =>
        new() { Value = values, NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Uuid };
}

/// <summary>
/// A payment is its own aggregate; each account it touches receives a fan-out
/// event (WAX-0001). Distribution records exactly where the money landed, so
/// a reversal can unwind it (HIVE-0007).
/// </summary>
public sealed class PaymentState : AggregateRoot
{
    public const string LockSql =
        "SELECT payment_pk, aggregate_sequence_number, amount_minor_units, currency_code, status, received_at, " +
        "distribution::text, unapplied_minor_units FROM ar.payment WHERE payment_pk = $1 FOR UPDATE";

    public override string AggregateType => "payment";
    public required long AmountMinorUnits { get; init; }
    public required string CurrencyCode { get; init; }
    public string Status { get; set; } = "posted";
    public required DateTimeOffset ReceivedAt { get; init; }
    public required JsonObject Distribution { get; init; }
    public long UnappliedMinorUnits { get; init; }

    public static PaymentState Read(NpgsqlDataReader reader) => new()
    {
        Pk = reader.GetGuid(0),
        AggregateSequenceNumber = reader.GetInt32(1),
        AmountMinorUnits = reader.GetInt64(2),
        CurrencyCode = reader.GetString(3),
        Status = reader.GetString(4),
        ReceivedAt = reader.GetFieldValue<DateTimeOffset>(5),
        Distribution = JsonNode.Parse(reader.GetString(6))!.AsObject(),
        UnappliedMinorUnits = reader.GetInt64(7),
    };

    protected override void QueueInsert(CommandContext context) => context.QueueSql(
        "INSERT INTO ar.payment (aggregate_sequence_number, payment_pk, amount_minor_units, currency_code, status, " +
        "received_at, distribution, unapplied_minor_units) VALUES (?, ?, ?, ?, ?, ?, ?::jsonb, ?)",
        AggregateSequenceNumber, Pk, AmountMinorUnits, CurrencyCode, Status, ReceivedAt,
        Distribution.ToJsonString(), UnappliedMinorUnits);

    protected override void QueueUpdate(CommandContext context) => context.QueueSql(
        "UPDATE ar.payment SET aggregate_sequence_number = ?, status = ? WHERE payment_pk = ?",
        AggregateSequenceNumber, Status, Pk);
}

/// <summary>One bucket instance on one account, as the handler sees it.</summary>
public sealed class BucketRow
{
    public const string ForAccountsSql =
        "SELECT b.account_balance_bucket_pk, b.account_fk, d.code, d.waterfall_priority, b.balance_minor_units " +
        "FROM ar.account_balance_bucket b " +
        "JOIN ar.balance_bucket_definition d ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk " +
        "WHERE b.account_fk = ANY($1)";

    public required Guid Pk { get; init; }
    public required string Code { get; init; }
    public required int Priority { get; init; }
    public required long Balance { get; set; }

    public static NpgsqlBatchCommand ForAccountsCommand(Guid[] accountPks) => new(ForAccountsSql)
    {
        Parameters = { AccountState.UuidArray(accountPks) },
    };

    /// <summary>Bucket rows by account, then by bucket code.</summary>
    public static async Task<Dictionary<Guid, Dictionary<string, BucketRow>>> ReadAsync(
        NpgsqlDataReader reader, IEnumerable<Guid> accountPks, CancellationToken cancellation)
    {
        var byAccount = accountPks.ToDictionary(pk => pk, _ => new Dictionary<string, BucketRow>());
        while (await reader.ReadAsync(cancellation))
        {
            var row = new BucketRow
            {
                Pk = reader.GetGuid(0),
                Code = reader.GetString(2),
                Priority = reader.GetInt32(3),
                Balance = reader.GetInt64(4),
            };
            byAccount[reader.GetGuid(1)][row.Code] = row;
        }
        return byAccount;
    }
}
