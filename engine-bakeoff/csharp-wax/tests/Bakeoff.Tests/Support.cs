using System.Text.Json.Nodes;
using HiveAR;
using Npgsql;
using Wax;
using Wax.Commands;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

/// <summary>
/// A fresh database per test class, migrated and seeded with the sample
/// bucket registry the Python side's tests use: interest, then fees, then
/// principal. Connects as the schema owner, as the Python tools service does.
/// </summary>
public sealed class DatabaseFixture : IAsyncLifetime
{
    readonly string databaseName = $"hivear_test_{Guid.NewGuid():N}"[..28];

    public WaxEngine Engine { get; private set; } = null!;

    public string ConnectionString { get; private set; } = "";

    public async Task InitializeAsync()
    {
        await using (var admin = NpgsqlDataSource.Create(WaxSettings.ConnectionStringFromEnvironment()))
        await using (var create = admin.CreateCommand($"CREATE DATABASE {databaseName}"))
        {
            await create.ExecuteNonQueryAsync();
        }
        ConnectionString = WaxSettings.ConnectionStringFromEnvironment(databaseName);
        Engine = await WaxEngine.CreateAsync(ConnectionString, [new HiveArModule()], migrate: true);
        await HiveArModule.SeedDemoAsync(Engine.DataSource);
    }

    public async Task DisposeAsync()
    {
        await Engine.DisposeAsync();
        await using var admin = NpgsqlDataSource.Create(WaxSettings.ConnectionStringFromEnvironment());
        await using var drop = admin.CreateCommand($"DROP DATABASE IF EXISTS {databaseName} WITH (FORCE)");
        await drop.ExecuteNonQueryAsync();
    }
}

/// <summary>An event as a test reads it back: its two sequence numbers, its type,
/// the rest of its columns as one JSON object, and its two hashes.</summary>
public sealed record LoadedEvent(long Version, long Sequence, string Type, JsonObject Data, string? PriorEventHash, string EventHash)
{
    public JsonObject? PriorValue => Data["prior_value"] as JsonObject;
    public JsonObject? NewValue => Data["new_value"] as JsonObject;
    public JsonObject? Metadata => Data["metadata"] as JsonObject;
}

public static class EngineExtensions
{
    /// <summary>Run the projection reader until every read model has reached the
    /// head as it stood when called, and require that none stopped on a failure.</summary>
    public static async Task CatchUpAsync(this WaxEngine engine, int seconds = 60)
    {
        var reader = engine.CreateReader();
        await reader.CatchUpAsync(TimeSpan.FromSeconds(seconds));
        var stopped = (await reader.StatusAsync()).Where(row => row.FailedAt is not null).ToList();
        Assert.True(stopped.Count == 0, string.Join("; ", stopped.Select(row => $"{row.Projection}: {row.FailureKind} {row.FailureDetail}")));
    }
}

/// <summary>The Python side's conftest helpers: place, post, balances.</summary>
public sealed class Scenario(DatabaseFixture db)
{
    public static readonly Guid Actor = Guid.Parse("00000000-0000-4000-8000-000000000001");

    public Task Run(ICommand command) => db.Engine.Commands.DispatchAsync(command, Actor, source: "test");

    public async Task<Guid> Place(DateOnly? listed = null, long? principal = null, long? interest = null, long? fee = null)
    {
        var balances = new Dictionary<string, long>();
        if (principal is null && interest is null && fee is null)
        {
            balances["principal"] = 100_000;
            balances["interest"] = 5_000;
        }
        if (principal is { } p)
        {
            balances["principal"] = p;
        }
        if (interest is { } i)
        {
            balances["interest"] = i;
        }
        if (fee is { } f)
        {
            balances["fee"] = f;
        }
        var command = new PlaceAccount
        {
            ClientAccountNumber = $"A-{Guid.NewGuid():N}"[..10],
            DebtType = "personal_loan",
            ContractType = "third_party_contingency",
            CurrencyCode = "USD",
            ListDate = listed ?? new DateOnly(2026, 1, 15),
            ItemizationDate = new DateOnly(2025, 12, 31),
            OpeningBalances = balances,
        };
        await Run(command);
        return command.AccountPk;
    }

    public async Task<Guid> Post(
        Guid[] accountPks,
        long amount,
        string method = "oldest_first",
        Dictionary<Guid, long>? directed = null,
        Dictionary<Guid, IReadOnlyDictionary<string, long>>? bucketAmounts = null)
    {
        var command = new PostPayment
        {
            AccountPks = accountPks,
            AmountMinorUnits = amount,
            CurrencyCode = "USD",
            AllocationMethod = method,
            DirectedAllocations = directed ?? [],
            BucketAmounts = bucketAmounts ?? [],
        };
        await Run(command);
        return command.PaymentPk;
    }

    public async Task<SortedDictionary<string, long>> Balances(Guid accountPk)
    {
        var balances = new SortedDictionary<string, long>(StringComparer.Ordinal);
        await using var command = db.Engine.DataSource.CreateCommand(
            "SELECT d.code, b.balance_minor_units FROM ar.account_balance_bucket b " +
            "JOIN ar.balance_bucket_definition d ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk " +
            "WHERE b.account_fk = $1");
        command.Parameters.AddWithValue(accountPk);
        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            balances[reader.GetString(0)] = reader.GetInt64(1);
        }
        return balances;
    }

    public static SortedDictionary<string, long> Buckets(params (string Code, long Amount)[] buckets) =>
        new(buckets.ToDictionary(bucket => bucket.Code, bucket => bucket.Amount), StringComparer.Ordinal);

    public async Task<List<LoadedEvent>> LoadStream(Guid streamId)
    {
        var events = new List<LoadedEvent>();
        await using var command = db.Engine.DataSource.CreateCommand(
            "SELECT aggregate_sequence_number, global_sequence_number, event_type, jsonb_build_object(" +
            "'aggregate_type', aggregate_type, 'schema_version', schema_version, 'prior_value', prior_value, " +
            "'new_value', new_value, 'occurred_at', occurred_at, 'actor_pk_ref', actor_pk_ref, 'actor_type', actor_type, " +
            "'reason_code_fk', reason_code_fk, 'source', source, 'correlation_pk_ref', correlation_pk_ref, " +
            "'correlation_type', correlation_type, 'metadata', metadata), prior_event_hash, event_hash " +
            "FROM core.event WHERE aggregate_pk_ref = $1 ORDER BY aggregate_sequence_number");
        command.Parameters.AddWithValue(streamId);
        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            events.Add(new LoadedEvent(
                reader.GetInt32(0),
                reader.GetInt64(1),
                reader.GetString(2),
                JsonNode.Parse(reader.GetString(3))!.AsObject(),
                reader.IsDBNull(4) ? null : reader.GetString(4),
                reader.GetString(5)));
        }
        return events;
    }

    public async Task<T> Scalar<T>(string sql, params object[] values)
    {
        await using var command = db.Engine.DataSource.CreateCommand(sql);
        foreach (var value in values)
        {
            command.Parameters.AddWithValue(value);
        }
        return (T)(await command.ExecuteScalarAsync())!;
    }

    public async Task Execute(string sql, params object[] values)
    {
        await using var command = db.Engine.DataSource.CreateCommand(sql);
        foreach (var value in values)
        {
            command.Parameters.AddWithValue(value);
        }
        await command.ExecuteNonQueryAsync();
    }

    public async Task<(string Status, long Amount, long Unapplied, JsonObject Distribution)> Payment(Guid paymentPk)
    {
        await using var command = db.Engine.DataSource.CreateCommand(
            "SELECT status, amount_minor_units, unapplied_minor_units, distribution::text FROM ar.payment WHERE payment_pk = $1");
        command.Parameters.AddWithValue(paymentPk);
        await using var reader = await command.ExecuteReaderAsync();
        Assert.True(await reader.ReadAsync());
        return (reader.GetString(0), reader.GetInt64(1), reader.GetInt64(2), JsonNode.Parse(reader.GetString(3))!.AsObject());
    }

    public static void JsonEqual(string expected, JsonNode? actual) =>
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse(expected), actual), $"expected {expected}, got {actual?.ToJsonString()}");
}
