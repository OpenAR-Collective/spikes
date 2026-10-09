using Wax.Store;
using Xunit;
using static Bakeoff.Tests.Scenario;

namespace Bakeoff.Tests;

/// <summary>
/// Same-aggregate writes serialize on the state row's lock (WAX-0013), so
/// concurrent commands against one account neither lose money nor fork its
/// hash chain, and accounts locked in a fixed order cannot deadlock.
/// </summary>
public sealed class ConcurrencyTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task Concurrent_payments_to_one_account_serialize_on_its_row_lock()
    {
        var accountPk = await scenario.Place(principal: 10_000);
        await Task.WhenAll(Enumerable.Range(0, 25).Select(_ => Task.Run(() => scenario.Post([accountPk], 100))));
        Assert.Equal(Buckets(("principal", 7_500)), await scenario.Balances(accountPk));
        var events = await scenario.LoadStream(accountPk);
        Assert.Equal(Enumerable.Range(1, 26).Select(version => (long)version), events.Select(e => e.Version));
        Assert.Empty(await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, accountPk));
    }

    [Fact]
    public async Task Payments_over_the_same_accounts_in_any_order_do_not_deadlock()
    {
        var first = await scenario.Place(listed: new DateOnly(2025, 1, 1), principal: 100_000);
        var second = await scenario.Place(listed: new DateOnly(2026, 1, 1), principal: 100_000);
        await Task.WhenAll(Enumerable.Range(0, 30).Select(index => Task.Run(() =>
            scenario.Post(index % 2 == 0 ? [first, second] : [second, first], 1_000, method: "even_split"))));
        var total = (await scenario.Balances(first)).Values.Sum() + (await scenario.Balances(second)).Values.Sum();
        Assert.Equal(200_000 - 30 * 1_000, total);
        Assert.Empty((await ChainVerifier.VerifyAllAsync(db.Engine.DataSource)).Problems);
    }
}
