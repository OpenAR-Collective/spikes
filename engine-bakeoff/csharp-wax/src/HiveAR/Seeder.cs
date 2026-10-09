using System.Diagnostics;
using System.Text.Json.Nodes;
using Npgsql;
using NpgsqlTypes;
using Wax.Events;
using Wax.Hashing;
using Wax.Projections;
using Wax.Rules;
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
/// writes them with COPY rather than through the command path; core.event's own
/// insert trigger still numbers each event and stamps its recorded time. Then it
/// moves every projection and the rule runner to the head, so nothing
/// reprocesses the seed. Foundation-written.
/// </summary>
public static class Seeder
{
    public static readonly Guid SeederActor = DerivedUuid.From("bakeoff:seeder");
    public static readonly DateTimeOffset OccurredAt = new(2026, 9, 30, 0, 0, 0, TimeSpan.Zero);

    public static async Task<TimeSpan> SeedAsync(
        NpgsqlDataSource dataSource, long accounts, IReadOnlyList<Projection> projections, CancellationToken cancellation = default)
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
        // The trigger numbers the COPY's rows in order from the sequence's next
        // value; nothing else writes while the seed loads, and that is checked below.
        var firstSequence = await Scalar<long>(connection, EventStore.PositionSql, cancellation) + 1;
        var eventType = WaxEventTypes.NameOf<AccountPlaced>();

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

            using (var events = Import(connection,
                "COPY core.event (event_pk, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, aggregate_type, " +
                "occurred_at, actor_pk_ref, actor_type, new_value, reason_code_fk, source, event_hash) FROM STDIN (FORMAT BINARY)"))
            {
                for (long i = 0; i < accounts; i++)
                {
                    var seed = SeedAccount.For(i);
                    var placed = Placed(seed);
                    var hash = EventHasher.Compute(new EventHashInput(
                        "account", seed.Pk, eventType, 1, null, placed, OccurredAt, SeederActor, "batch", reason, null));
                    events.StartRow();
                    events.Write(DerivedUuid.From($"bakeoff:seed-event:{i}"), NpgsqlDbType.Uuid);
                    events.Write(1, NpgsqlDbType.Integer);
                    events.Write(eventType, NpgsqlDbType.Text);
                    events.Write((short)1, NpgsqlDbType.Smallint);
                    events.Write(seed.Pk, NpgsqlDbType.Uuid);
                    events.Write("account", NpgsqlDbType.Text);
                    events.Write(OccurredAt, NpgsqlDbType.TimestampTz);
                    events.Write(SeederActor, NpgsqlDbType.Uuid);
                    events.Write("batch", NpgsqlDbType.Text);
                    events.Write(placed.ToJsonString(), NpgsqlDbType.Jsonb);
                    events.Write(reason, NpgsqlDbType.Uuid);
                    events.Write("seed", NpgsqlDbType.Text);
                    events.Write(hash, NpgsqlDbType.Text);
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
        if (await Scalar<long>(connection, EventStore.PositionSql, cancellation) != head)
        {
            throw new InvalidOperationException("Something else wrote events while the seed loaded; the summary rows' positions are wrong");
        }
        var positions = projections.Select(projection => (projection.Name, projection.Digest()))
            .Append((RuleRunner.BasePosition, "rules"));
        foreach (var (name, digest) in positions)
        {
            await using var position = new NpgsqlCommand(
                "INSERT INTO core.projection_checkpoint (projection_name, definition_digest, last_global_sequence_number) " +
                "VALUES ($1, $2, $3) ON CONFLICT (projection_name) DO UPDATE SET " +
                "last_global_sequence_number = EXCLUDED.last_global_sequence_number, updated_at = now()", connection);
            position.Parameters.AddWithValue(name);
            position.Parameters.AddWithValue(digest);
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
