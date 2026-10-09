using HiveAR.Projections;
using JasperFx.Events.Projections;
using Marten;
using Npgsql;
using Wax.Commands;
using Wax.Store;

namespace HiveAR;

public sealed class HiveArModule : IWaxModule
{
    public string Name => "hivear";

    public IReadOnlyList<SchemaScript> SchemaScripts { get; } =
        EmbeddedScripts.From(typeof(HiveArModule).Assembly, "Schema", "hivear");

    public void ConfigureStore(StoreOptions options)
    {
        options.Projections.Add(new AccountSummaryProjection(), ProjectionLifecycle.Async);
        options.Projections.Add(new PaymentReadModelProjection(), ProjectionLifecycle.Async);
        options.Projections.Add(new AccountPaymentLineProjection(), ProjectionLifecycle.Async);
    }

    public void RegisterHandlers(CommandDispatcher dispatcher)
    {
        dispatcher.Register(new PlaceAccountHandler());
        dispatcher.Register(new OpenDisputeHandler());
        dispatcher.Register(new PostPaymentHandler());
        dispatcher.Register(new ReversePaymentHandler());
        dispatcher.Register(new AccrueInterestHandler());
        dispatcher.Register(new ChangeAccountStatusHandler());
        dispatcher.Register(new AssignToQueueHandler());
        dispatcher.Register(new CreateTaskHandler());
        dispatcher.Register(new ScheduleFutureEventHandler());
        dispatcher.Register(new FirePendingEventHandler());
    }

    /// <summary>
    /// A sample lending bucket registry. HIVE-0005 has the framework ship no
    /// buckets of its own; these stand in for a debt-type module, as the
    /// prototype's seed_demo command does.
    /// </summary>
    public static async Task SeedDemoAsync(NpgsqlDataSource dataSource, CancellationToken cancellation = default)
    {
        (string Code, string Label, int Priority, bool BearsInterest)[] buckets =
        [
            ("interest", "Interest", 10, true),
            ("fee", "Fees", 20, false),
            ("principal", "Principal", 30, false),
        ];
        foreach (var (code, label, priority, bearsInterest) in buckets)
        {
            await using var command = dataSource.CreateCommand(
                "INSERT INTO ar.balance_bucket_definition (balance_bucket_definition_pk, code, label, waterfall_priority, " +
                "bears_interest, origin) VALUES (gen_random_uuid(), $1, $2, $3, $4, 'client_placed') " +
                "ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label, waterfall_priority = EXCLUDED.waterfall_priority, " +
                "bears_interest = EXCLUDED.bears_interest");
            command.Parameters.AddWithValue(code);
            command.Parameters.AddWithValue(label);
            command.Parameters.AddWithValue(priority);
            command.Parameters.AddWithValue(bearsInterest);
            await command.ExecuteNonQueryAsync(cancellation);
        }
    }
}
