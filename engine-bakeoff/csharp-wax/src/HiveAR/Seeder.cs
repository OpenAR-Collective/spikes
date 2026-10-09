using System.Diagnostics;
using System.Text.Json.Nodes;
using Marten;
using Npgsql;
using NpgsqlTypes;
using Wax.Events;
using Wax.Hashing;
using Wax.Store;

namespace HiveAR;

/// <summary>One account of the bake-off's seed, as WORKLOADS.md defines it.</summary>
public sealed record SeedAccount(
    Guid Pk,
    string ClientAccountNumber,
    DateOnly ListDate,
    DateOnly ItemizationDate,
    long Principal,
    long Interest,
    long Fee)
{
    public static readonly DateOnly AccruedThrough = new(2026, 9, 30);

    public static SeedAccount For(long i) => new(
        DerivedUuid.From($"bakeoff:account:{i}"),
        $"BK-{i:D9}",
        new DateOnly(2020, 1, 1).AddDays((int)(i % 2190)),
        new DateOnly(2020, 1, 1).AddDays((int)(i % 2190) - 30),
        Principal: 50_000 + i * 7_919 % 950_000,
        Interest: 100 + i * 104_729 % 20_000,
        Fee: 500 + i % 3 * 1_000);

    public IReadOnlyDictionary<string, long> Balances => new Dictionary<string, long>
    {
        ["principal"] = Principal,
        ["interest"] = Interest,
        ["fee"] = Fee,
    };
}

/// <summary>
/// The bulk loader for the bake-off's seed. Not measured. For each account it
/// writes exactly what PlaceAccount would have: the state row, the bucket rows,
/// one account.placed event with a valid hash, and the account summary row. It
/// writes them with COPY rather than through the command path, then moves every
/// projection and the rule runner to the head, so nothing reprocesses the seed.
/// Foundation-written.
/// </summary>
public static class Seeder
{
    public static readonly Guid SeederActor = DerivedUuid.From("bakeoff:seeder");
    public static readonly DateTimeOffset OccurredAt = new(2026, 9, 30, 0, 0, 0, TimeSpan.Zero);

    public static async Task<TimeSpan> SeedAsync(
        NpgsqlDataSource dataSource, IDocumentStore store, long accounts, IEnumerable<string> positions, CancellationToken cancellation = default)
    {
        var clock = Stopwatch.StartNew();
        await using var connection = await dataSource.OpenConnectionAsync(cancellation);
        if (await Scalar<long>(connection, "SELECT count(*) FROM ar.account", cancellation) != 0)
        {
            throw new InvalidOperationException("The seed loads into an empty database");
        }
        var reason = await Scalar<Guid>(connection,
            "SELECT value.reference_value_pk FROM core.reference_value value JOIN core.reference_type type " +
            "ON type.reference_type_pk = value.reference_type_fk WHERE type.code = 'reason_code' AND value.code = 'account_placed'",
            cancellation);
        var definitions = new Dictionary<string, Guid>();
        await using (var command = new NpgsqlCommand("SELECT code, balance_bucket_definition_pk FROM ar.balance_bucket_definition", connection))
        await using (var reader = await command.ExecuteReaderAsync(cancellation))
        {
            while (await reader.ReadAsync(cancellation))
            {
                definitions[reader.GetString(0)] = reader.GetGuid(1);
            }
        }
        var firstSequence = await Scalar<long>(connection, "SELECT COALESCE(max(seq_id), 0) FROM core.mt_events", cancellation) + 1;
        var eventType = WaxEventTypes.NameOf<AccountPlaced>();
        var serializer = store.Advanced.Serializer;
        var dotnetType = $"{typeof(AccountPlaced).FullName}, {typeof(AccountPlaced).Assembly.GetName().Name}";

        await Task.Run(() =>
        {
            using (var account = Import(connection, 
                "COPY ar.account (aggregate_sequence_number, account_pk, client_account_number, debt_type, contract_type, " +
                "currency_code, status, is_disputed, list_date, itemization_date, interest_accrued_through) FROM STDIN (FORMAT BINARY)"))
            {
                for (long i = 0; i < accounts; i++)
                {
                    var seed = SeedAccount.For(i);
                    account.StartRow();
                    account.Write(1, NpgsqlDbType.Integer);
                    account.Write(seed.Pk, NpgsqlDbType.Uuid);
                    account.Write(seed.ClientAccountNumber, NpgsqlDbType.Text);
                    account.Write("personal_loan", NpgsqlDbType.Text);
                    account.Write("third_party_contingency", NpgsqlDbType.Text);
                    account.Write("USD", NpgsqlDbType.Varchar);
                    account.Write("active", NpgsqlDbType.Text);
                    account.Write(false, NpgsqlDbType.Boolean);
                    account.Write(seed.ListDate, NpgsqlDbType.Date);
                    account.Write(seed.ItemizationDate, NpgsqlDbType.Date);
                    account.Write(SeedAccount.AccruedThrough, NpgsqlDbType.Date);
                }
                account.Complete();
            }

            using (var buckets = Import(connection, 
                "COPY ar.account_balance_bucket (account_balance_bucket_pk, balance_minor_units, account_fk, balance_bucket_definition_fk) " +
                "FROM STDIN (FORMAT BINARY)"))
            {
                for (long i = 0; i < accounts; i++)
                {
                    var seed = SeedAccount.For(i);
                    foreach (var (code, amount) in seed.Balances)
                    {
                        buckets.StartRow();
                        buckets.Write(Guid.NewGuid(), NpgsqlDbType.Uuid);
                        buckets.Write(amount, NpgsqlDbType.Bigint);
                        buckets.Write(seed.Pk, NpgsqlDbType.Uuid);
                        buckets.Write(definitions[code], NpgsqlDbType.Uuid);
                    }
                }
                buckets.Complete();
            }

            using (var streams = Import(connection, 
                "COPY core.mt_streams (id, version, timestamp, created, tenant_id, compacted_version, is_archived) FROM STDIN (FORMAT BINARY)"))
            {
                for (long i = 0; i < accounts; i++)
                {
                    streams.StartRow();
                    streams.Write(SeedAccount.For(i).Pk, NpgsqlDbType.Uuid);
                    streams.Write(1L, NpgsqlDbType.Bigint);
                    streams.Write(OccurredAt, NpgsqlDbType.TimestampTz);
                    streams.Write(OccurredAt, NpgsqlDbType.TimestampTz);
                    streams.Write("*DEFAULT*", NpgsqlDbType.Varchar);
                    streams.Write(0L, NpgsqlDbType.Bigint);
                    streams.Write(false, NpgsqlDbType.Boolean);
                }
                streams.Complete();
            }

            using (var events = Import(connection, 
                "COPY core.mt_events (seq_id, id, stream_id, version, data, type, timestamp, tenant_id, mt_dotnet_type, headers, is_archived) " +
                "FROM STDIN (FORMAT BINARY)"))
            {
                for (long i = 0; i < accounts; i++)
                {
                    var seed = SeedAccount.For(i);
                    var placed = Placed(seed);
                    var body = new AccountPlaced
                    {
                        AggregateType = "account",
                        NewValue = placed,
                        OccurredAt = OccurredAt,
                        ActorPkRef = SeederActor,
                        ActorType = "batch",
                        ReasonCodeFk = reason,
                        Source = "seed",
                    };
                    var hash = EventHasher.Compute(new EventHashInput(
                        "account", seed.Pk, eventType, 1, null, placed, OccurredAt, SeederActor, "batch", reason, null));
                    events.StartRow();
                    events.Write(firstSequence + i, NpgsqlDbType.Bigint);
                    events.Write(DerivedUuid.From($"bakeoff:seed-event:{i}"), NpgsqlDbType.Uuid);
                    events.Write(seed.Pk, NpgsqlDbType.Uuid);
                    events.Write(1L, NpgsqlDbType.Bigint);
                    events.Write(serializer.ToJson(body), NpgsqlDbType.Jsonb);
                    events.Write(eventType, NpgsqlDbType.Varchar);
                    events.Write(OccurredAt, NpgsqlDbType.TimestampTz);
                    events.Write("*DEFAULT*", NpgsqlDbType.Varchar);
                    events.Write(dotnetType, NpgsqlDbType.Varchar);
                    events.Write(new JsonObject { [StreamHeads.EventHashHeader] = hash }.ToJsonString(), NpgsqlDbType.Jsonb);
                    events.Write(false, NpgsqlDbType.Boolean);
                }
                events.Complete();
            }

            using (var summary = Import(connection, 
                "COPY ar.account_summary_read_model (account_summary_read_model_pk, client_account_number, status, is_disputed, " +
                "currency_code, balance_minor_units, balance_buckets, last_global_sequence_number, contract_type, debt_type, list_date) " +
                "FROM STDIN (FORMAT BINARY)"))
            {
                for (long i = 0; i < accounts; i++)
                {
                    var seed = SeedAccount.For(i);
                    summary.StartRow();
                    summary.Write(seed.Pk, NpgsqlDbType.Uuid);
                    summary.Write(seed.ClientAccountNumber, NpgsqlDbType.Text);
                    summary.Write("active", NpgsqlDbType.Text);
                    summary.Write(false, NpgsqlDbType.Boolean);
                    summary.Write("USD", NpgsqlDbType.Varchar);
                    summary.Write(seed.Principal + seed.Interest + seed.Fee, NpgsqlDbType.Bigint);
                    summary.Write(Json(seed.Balances).ToJsonString(), NpgsqlDbType.Jsonb);
                    summary.Write(firstSequence + i, NpgsqlDbType.Bigint);
                    summary.Write("third_party_contingency", NpgsqlDbType.Text);
                    summary.Write("personal_loan", NpgsqlDbType.Text);
                    summary.Write(seed.ListDate, NpgsqlDbType.Date);
                }
                summary.Complete();
            }
        }, cancellation);

        var head = firstSequence + accounts - 1;
        await Execute(connection, $"SELECT setval('core.mt_events_sequence', {head})", cancellation);
        foreach (var name in positions.Append("HighWaterMark"))
        {
            await using var position = new NpgsqlCommand(
                "INSERT INTO core.mt_event_progression (name, last_seq_id) VALUES ($1, $2) " +
                "ON CONFLICT (name) DO UPDATE SET last_seq_id = EXCLUDED.last_seq_id, last_updated = now()", connection);
            position.Parameters.AddWithValue(name);
            position.Parameters.AddWithValue(head);
            await position.ExecuteNonQueryAsync(cancellation);
        }
        await Execute(connection, "ANALYZE", cancellation);
        return clock.Elapsed;
    }

    // A load of millions of rows, and the deferred foreign-key checks at its
    // end, run far past Npgsql's default 30-second timeout.
    static readonly TimeSpan LongTimeout = TimeSpan.FromHours(2);

    static NpgsqlBinaryImporter Import(NpgsqlConnection connection, string copy)
    {
        var importer = connection.BeginBinaryImport(copy);
        importer.Timeout = LongTimeout;
        return importer;
    }

    /// <summary>The account.placed new_value PlaceAccount writes for a seed account.</summary>
    public static JsonObject Placed(SeedAccount seed) => new()
    {
        ["client_account_number"] = seed.ClientAccountNumber,
        ["debt_type"] = "personal_loan",
        ["contract_type"] = "third_party_contingency",
        ["currency_code"] = "USD",
        ["status"] = "active",
        ["is_disputed"] = false,
        ["list_date"] = IsoFormat.Date(seed.ListDate),
        ["itemization_date"] = IsoFormat.Date(seed.ItemizationDate),
        ["balance_buckets"] = Json(seed.Balances),
        ["interest_accrued_through"] = IsoFormat.Date(SeedAccount.AccruedThrough),
    };

    static JsonObject Json(IReadOnlyDictionary<string, long> balances) =>
        new(balances.Select(pair => KeyValuePair.Create(pair.Key, (JsonNode?)pair.Value)));

    static async Task<T> Scalar<T>(NpgsqlConnection connection, string sql, CancellationToken cancellation)
    {
        await using var command = new NpgsqlCommand(sql, connection) { CommandTimeout = (int)LongTimeout.TotalSeconds };
        return (T)(await command.ExecuteScalarAsync(cancellation))!;
    }

    static async Task Execute(NpgsqlConnection connection, string sql, CancellationToken cancellation)
    {
        await using var command = new NpgsqlCommand(sql, connection) { CommandTimeout = (int)LongTimeout.TotalSeconds };
        await command.ExecuteNonQueryAsync(cancellation);
    }
}
