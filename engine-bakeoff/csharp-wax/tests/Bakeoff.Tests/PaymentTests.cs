using HiveAR;
using Wax.Commands;
using Wax.Store;
using Xunit;
using static Bakeoff.Tests.Scenario;

namespace Bakeoff.Tests;

// Ported from the Python side's tests/test_payments.py, plus the retry rules
// that the prototype exercised through its API tests.
public sealed class PaymentTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task Waterfall_pays_buckets_in_priority_order()
    {
        var accountPk = await scenario.Place(principal: 100_000, interest: 5_000, fee: 2_000);
        var paymentPk = await scenario.Post([accountPk], 8_000);
        Assert.Equal(Buckets(("interest", 0), ("fee", 0), ("principal", 99_000)), await scenario.Balances(accountPk));
        var applied = (await scenario.LoadStream(accountPk))[^1];
        Assert.Equal("account.payment_applied", applied.Type);
        JsonEqual("""{"balance_buckets": {"interest": 5000, "fee": 2000, "principal": 100000}}""", applied.PriorValue);
        JsonEqual("""{"balance_buckets": {"interest": 0, "fee": 0, "principal": 99000}}""", applied.NewValue);
        Assert.Equal(paymentPk.ToString(), applied.Metadata!["payment_pk"]!.GetValue<string>());
        // One logical operation, two aggregates, one event each.
        Assert.Equal(["payment.posted"], (await scenario.LoadStream(paymentPk)).Select(e => e.Type));
    }

    [Fact]
    public async Task Per_payment_bucket_override_beats_the_waterfall()
    {
        var accountPk = await scenario.Place(principal: 100_000, interest: 5_000);
        await scenario.Post([accountPk], 3_000, bucketAmounts: new() { [accountPk] = new Dictionary<string, long> { ["principal"] = 3_000 } });
        Assert.Equal(Buckets(("principal", 97_000), ("interest", 5_000)), await scenario.Balances(accountPk));
    }

    [Fact]
    public async Task Disputed_account_gets_nothing_the_consumer_did_not_direct()
    {
        var older = await scenario.Place(listed: new DateOnly(2025, 1, 1), principal: 10_000);
        var newer = await scenario.Place(listed: new DateOnly(2026, 1, 1), principal: 10_000);
        await scenario.Run(new OpenDispute { AccountPk = older });
        await scenario.Post([older, newer], 4_000);
        Assert.Equal(Buckets(("principal", 10_000)), await scenario.Balances(older));
        Assert.Equal(Buckets(("principal", 6_000)), await scenario.Balances(newer));
        await scenario.Post([older, newer], 1_000, directed: new() { [older] = 1_000 });
        Assert.Equal(Buckets(("principal", 9_000)), await scenario.Balances(older));
        Assert.Equal(Buckets(("principal", 6_000)), await scenario.Balances(newer));
    }

    [Fact]
    public async Task Oldest_first_and_even_split_allocation()
    {
        var older = await scenario.Place(listed: new DateOnly(2025, 1, 1), principal: 1_000);
        var newer = await scenario.Place(listed: new DateOnly(2026, 1, 1), principal: 10_000);
        await scenario.Post([older, newer], 3_000, method: "oldest_first");
        Assert.Equal(Buckets(("principal", 0)), await scenario.Balances(older));
        Assert.Equal(Buckets(("principal", 8_000)), await scenario.Balances(newer));

        var first = await scenario.Place(listed: new DateOnly(2025, 1, 1), principal: 10_000);
        var second = await scenario.Place(listed: new DateOnly(2026, 1, 1), principal: 10_000);
        await scenario.Post([first, second], 1_001, method: "even_split");
        Assert.Equal(Buckets(("principal", 9_499)), await scenario.Balances(first));
        Assert.Equal(Buckets(("principal", 9_500)), await scenario.Balances(second));
    }

    [Fact]
    public async Task Overpayment_is_held_as_unapplied_not_lost()
    {
        var accountPk = await scenario.Place(principal: 1_000);
        var payment = await scenario.Payment(await scenario.Post([accountPk], 1_500));
        Assert.Equal(Buckets(("principal", 0)), await scenario.Balances(accountPk));
        Assert.Equal(500, payment.Unapplied);
        var applied = payment.Distribution[accountPk.ToString()]!.AsObject().Sum(pair => pair.Value!.GetValue<long>());
        Assert.Equal(payment.Amount, applied + 500);
    }

    [Fact]
    public async Task Reversal_unwinds_the_recorded_distribution()
    {
        var accountPk = await scenario.Place(principal: 100_000, interest: 5_000);
        var before = await scenario.Balances(accountPk);
        var paymentPk = await scenario.Post([accountPk], 7_000);
        await scenario.Run(new ReversePayment { PaymentPk = paymentPk });
        Assert.Equal(before, await scenario.Balances(accountPk));
        Assert.Equal("reversed", (await scenario.Payment(paymentPk)).Status);
        Assert.Empty(await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, accountPk));
        Assert.Empty(await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, paymentPk));
        await Assert.ThrowsAsync<CommandRejectedException>(() => scenario.Run(new ReversePayment { PaymentPk = paymentPk }));
    }

    [Fact]
    public async Task Rejected_command_writes_nothing()
    {
        var accountPk = await scenario.Place(principal: 1_000);
        await Assert.ThrowsAsync<CommandRejectedException>(() =>
            scenario.Post([accountPk], 500, bucketAmounts: new() { [accountPk] = new Dictionary<string, long> { ["principal"] = 5_000 } }));
        Assert.Single(await scenario.LoadStream(accountPk));
        Assert.Equal(Buckets(("principal", 1_000)), await scenario.Balances(accountPk));
    }

    [Fact]
    public async Task Bucket_state_equals_replay_of_the_event_stream()
    {
        // WAX-0001: state tables are rebuildable from the event store.
        var accountPk = await scenario.Place(principal: 100_000, interest: 5_000, fee: 1_000);
        var first = await scenario.Post([accountPk], 5_500);
        await scenario.Post([accountPk], 20_000);
        await scenario.Run(new ReversePayment { PaymentPk = first });
        var replayed = new SortedDictionary<string, long>(StringComparer.Ordinal);
        foreach (var stored in await scenario.LoadStream(accountPk))
        {
            if (stored.NewValue?["balance_buckets"] is System.Text.Json.Nodes.JsonObject buckets)
            {
                foreach (var (code, amount) in buckets)
                {
                    replayed[code] = amount!.GetValue<long>();
                }
            }
        }
        Assert.Equal(replayed, await scenario.Balances(accountPk));
    }

    [Fact]
    public async Task Bucket_override_smaller_than_the_payment_loses_no_money()
    {
        // A bucket override smaller than the payment once lost the remainder.
        var accountPk = await scenario.Place(principal: 100_000, interest: 5_000);
        var payment = await scenario.Payment(await scenario.Post(
            [accountPk], 10_000, bucketAmounts: new() { [accountPk] = new Dictionary<string, long> { ["principal"] = 3_000 } }));
        Assert.Equal(Buckets(("principal", 97_000), ("interest", 5_000)), await scenario.Balances(accountPk));
        Assert.Equal(7_000, payment.Unapplied);

        var older = await scenario.Place(listed: new DateOnly(2025, 1, 1), principal: 50_000);
        var newer = await scenario.Place(listed: new DateOnly(2026, 1, 1), principal: 50_000);
        payment = await scenario.Payment(await scenario.Post(
            [older, newer], 10_000, bucketAmounts: new() { [older] = new Dictionary<string, long> { ["principal"] = 1_000 } }));
        Assert.Equal(Buckets(("principal", 49_000)), await scenario.Balances(older));
        Assert.Equal(Buckets(("principal", 41_000)), await scenario.Balances(newer));
        var applied = payment.Distribution.Sum(account => account.Value!.AsObject().Sum(pair => pair.Value!.GetValue<long>()));
        Assert.Equal(10_000, applied + payment.Unapplied);
        Assert.Equal(10_000, payment.Amount);
    }

    [Fact]
    public async Task Retrying_a_command_that_took_effect_is_answered_as_success()
    {
        var accountPk = await scenario.Place(principal: 10_000);
        var post = new PostPayment { AccountPks = [accountPk], AmountMinorUnits = 1_000, CurrencyCode = "USD" };
        await scenario.Run(post);
        await scenario.Run(post);
        Assert.Equal(Buckets(("principal", 9_000)), await scenario.Balances(accountPk));
        Assert.Single(await scenario.LoadStream(post.PaymentPk));

        var reused = await Assert.ThrowsAsync<CommandRejectedException>(() => scenario.Run(post with { AmountMinorUnits = 2_000 }));
        Assert.Equal("key_reused", reused.Code);
    }
}
