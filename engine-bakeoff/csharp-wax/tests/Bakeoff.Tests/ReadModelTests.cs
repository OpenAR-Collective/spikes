using HiveAR;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

/// <summary>
/// Wax's projection reader keeps the three read models current, and once
/// it has caught up they agree with the authoritative state. This is the
/// bake-off's read-model gate, run on a small scale.
/// </summary>
public sealed class ReadModelTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task Read_models_agree_with_authoritative_state_once_caught_up()
    {
        var first = await scenario.Place(listed: new DateOnly(2025, 1, 1), principal: 10_000, interest: 500);
        var second = await scenario.Place(listed: new DateOnly(2026, 1, 1), principal: 20_000, fee: 300);
        var third = await scenario.Place(principal: 1_000);
        var kept = await scenario.Post([first, second], 4_000);
        var reversed = await scenario.Post([second], 1_200);
        var heldBack = await scenario.Post([third], 1_500);
        await scenario.Run(new ReversePayment { PaymentPk = reversed });
        await scenario.Run(new OpenDispute { AccountPk = third });

        await db.Engine.CatchUpAsync();

        foreach (var accountPk in new[] { first, second, third })
        {
            var buckets = await scenario.Balances(accountPk);
            var summaryTotal = await scenario.Scalar<long>(
                "SELECT balance_minor_units FROM ar.account_summary_read_model WHERE account_summary_read_model_pk = $1", accountPk);
            Assert.Equal(buckets.Values.Sum(), summaryTotal);
            var summaryBuckets = await scenario.Scalar<string>(
                "SELECT balance_buckets::text FROM ar.account_summary_read_model WHERE account_summary_read_model_pk = $1", accountPk);
            Scenario.JsonEqual(System.Text.Json.JsonSerializer.Serialize(buckets), System.Text.Json.Nodes.JsonNode.Parse(summaryBuckets));
        }
        Assert.True(await scenario.Scalar<bool>(
            "SELECT is_disputed FROM ar.account_summary_read_model WHERE account_summary_read_model_pk = $1", third));
        Assert.Equal("reversed", await scenario.Scalar<string>(
            "SELECT status FROM ar.payment_read_model WHERE payment_read_model_pk = $1", reversed));
        Assert.Equal(3L, await scenario.Scalar<long>("SELECT count(*) FROM ar.payment_read_model"));
        // A line for every account each payment was taken against.
        Assert.Equal(2L, await scenario.Scalar<long>("SELECT count(*) FROM ar.account_payment_read_model_v2 WHERE payment_pk = $1", kept));
        Assert.Equal(500L, await scenario.Scalar<long>(
            "SELECT payment_unapplied_minor_units FROM ar.account_payment_read_model_v2 WHERE payment_pk = $1", heldBack));
        var report = await ChainVerifier.VerifyAllAsync(db.Engine.DataSource);
        Assert.Empty(report.Problems);
    }
}
