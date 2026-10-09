using System.Text.Json.Nodes;
using HiveAR;
using Wax.Batches;
using Wax.Commands;
using Wax.Hashing;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

/// <summary>
/// The bulk workloads: a chunk per transaction with set-based writes must pass
/// the same gates as the single-command path and store events of the same shape.
/// </summary>
public sealed class BulkTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    const int Accounts = 2_500;
    static readonly DateTimeOffset ReceivedAt = new(2026, 10, 1, 6, 0, 0, TimeSpan.Zero);
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task Bulk_payments_and_placement_pass_their_gates_and_match_the_single_path()
    {
        await Seeder.SeedAsync(db.Engine.DataSource, Accounts, db.Engine.Projections);
        var totalBefore = await scenario.Scalar<decimal>("SELECT sum(balance_minor_units) FROM ar.account_balance_bucket");
        var actor = DerivedUuid.From("bakeoff:batch");
        var bulk = new BulkWriter(db.Engine.DataSource, await ReasonCodes.LoadAsync(db.Engine.DataSource));

        // The payment file, a chunk per transaction.
        var runner = new BatchRunner(db.Engine.DataSource, "ar.account", "account_pk");
        var runPk = await runner.CreateRunAsync("payment_file_bulk", 1_000);
        var outcomes = new List<BulkOutcome>();
        await runner.RunChunksAsync(runPk, workers: 3, "test", async keys =>
        {
            var outcome = await bulk.PostPaymentsAsync(keys, runPk, 2_500, ReceivedAt, actor, CancellationToken.None);
            lock (outcomes)
            {
                outcomes.Add(outcome);
            }
        });
        Assert.Equal(3, outcomes.Count);
        Assert.Equal(Accounts, outcomes.Sum(outcome => outcome.Succeeded));
        Assert.Equal((long)Accounts, await scenario.Scalar<long>("SELECT count(*) FROM ar.payment"));
        Assert.Equal(totalBefore - 2_500m * Accounts, await scenario.Scalar<decimal>("SELECT sum(balance_minor_units) FROM ar.account_balance_bucket"));

        // A chunk run again posts nothing twice.
        var again = await bulk.PostPaymentsAsync([SeedAccount.For(0).Pk, SeedAccount.For(1).Pk], runPk, 2_500, ReceivedAt, actor, CancellationToken.None);
        Assert.Equal(new BulkOutcome(0, 0, 2), again);
        Assert.Equal((long)Accounts, await scenario.Scalar<long>("SELECT count(*) FROM ar.payment"));

        // The bulk events have exactly the shape of the single-command path's.
        // A twin of seed account 0, as it stood before the payment file, paid through the single-command path.
        var twin = SeedAccount.For(0);
        var single = await scenario.Place(principal: twin.Principal, interest: twin.Interest, fee: twin.Fee);
        await db.Engine.Commands.DispatchAsync(new PostPayment
        {
            PaymentPk = DerivedUuid.From($"bakeoff:payment:{runPk}:{single}"),
            AccountPks = [single],
            AmountMinorUnits = 2_500,
            CurrencyCode = "USD",
            ReceivedAt = ReceivedAt,
        }, actor, "batch", "batch");
        var singleApplied = (await scenario.LoadStream(single))[^1];
        var bulkApplied = (await scenario.LoadStream(SeedAccount.For(0).Pk))[^1];
        Assert.Equal(singleApplied.Type, bulkApplied.Type);
        Assert.Equal(Shape(singleApplied.Data), Shape(bulkApplied.Data));
        Scenario.JsonEqual(singleApplied.PriorValue!.ToJsonString(), bulkApplied.PriorValue);
        Scenario.JsonEqual(singleApplied.NewValue!.ToJsonString(), bulkApplied.NewValue);
        Scenario.JsonEqual(singleApplied.Metadata!["bucket_amounts"]!.ToJsonString(), bulkApplied.Metadata!["bucket_amounts"]);
        var singlePosted = (await scenario.LoadStream(DerivedUuid.From($"bakeoff:payment:{runPk}:{single}")))[0];
        var bulkPosted = (await scenario.LoadStream(DerivedUuid.From($"bakeoff:payment:{runPk}:{SeedAccount.For(0).Pk}")))[0];
        Assert.Equal(Shape(singlePosted.Data), Shape(bulkPosted.Data));
        Assert.Equal(singlePosted.NewValue!["request"]!.ToJsonString(),
            bulkPosted.NewValue!["request"]!.ToJsonString().Replace(SeedAccount.For(0).Pk.ToString(), single.ToString()));

        // Placement in bulk.
        var placementRun = Guid.NewGuid();
        var placed = Enumerable.Range(0, 1_500)
            .Select(i => SeedAccount.For(i) with
            {
                Pk = DerivedUuid.From($"bakeoff:placement:{placementRun}:{i}"),
                ClientAccountNumber = $"PL-{i:D9}",
            })
            .ToList();
        Assert.Equal(new BulkOutcome(1_000, 0, 0), await bulk.PlaceAccountsAsync(placed.Take(1_000).ToList(), actor, CancellationToken.None));
        Assert.Equal(new BulkOutcome(500, 0, 500), await bulk.PlaceAccountsAsync(placed.Skip(500).ToList(), actor, CancellationToken.None));
        Assert.Equal(1_500L, await scenario.Scalar<long>("SELECT count(*) FROM ar.account WHERE client_account_number LIKE 'PL-%'"));
        var fifth = placed[5];
        Assert.Equal(Scenario.Buckets(("principal", fifth.Principal), ("interest", fifth.Interest), ("fee", fifth.Fee)), await scenario.Balances(fifth.Pk));
        var placedEvents = await scenario.LoadStream(fifth.Pk);
        Assert.Equal(["account.placed"], placedEvents.Select(e => e.Type));
        Scenario.JsonEqual(Seeder.Placed(fifth).ToJsonString(), placedEvents[0].NewValue);

        // Every chain verifies, and the read models catch up and agree.
        Assert.Empty((await ChainVerifier.VerifyAllAsync(db.Engine.DataSource)).Problems);
        await db.Engine.CatchUpAsync(120);
        Assert.Equal(0L, await scenario.Scalar<long>(
            "SELECT count(*) FROM ar.account a LEFT JOIN ar.account_summary_read_model s ON s.account_summary_read_model_pk = a.account_pk " +
            "WHERE s.balance_minor_units IS DISTINCT FROM (SELECT sum(balance_minor_units) FROM ar.account_balance_bucket WHERE account_fk = a.account_pk) " +
            "OR s.status IS DISTINCT FROM a.status"));
        Assert.Equal((long)Accounts + 1, await scenario.Scalar<long>("SELECT count(*) FROM ar.payment_read_model"));
    }

    /// <summary>Every key path and value type in a JSON value, but none of its values.</summary>
    static string Shape(JsonNode? node) => node switch
    {
        JsonObject obj => "{" + string.Join(",", obj.OrderBy(pair => pair.Key, StringComparer.Ordinal)
            .Select(pair => pair.Key is "occurred_at" or "actor_pk_ref" ? pair.Key : $"{(IsKey(pair.Key) ? "<key>" : pair.Key)}:{Shape(pair.Value)}")) + "}",
        JsonArray array => "[" + string.Join(",", array.Select(Shape)) + "]",
        JsonValue value => value.GetValueKind().ToString(),
        _ => "null",
    };

    static bool IsKey(string key) => Guid.TryParse(key, out _);
}
